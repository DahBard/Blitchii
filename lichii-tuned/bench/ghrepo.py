"""GitHub for the autopilot, through the GitHub CLI (gh) signed in on the PC.

Reads: the build workflow's successful runs and their artifacts, the queue folder on the build
branch, and downloads an artifact with `gh run download`. Writes: commits to the results branch
through the Git data API (blobs, a tree, a commit, the branch ref), so no clone is kept and
several files land in one commit. Every call is retried with backoff on network and server
errors; a 4xx answer (other than a rate limit) is not.
"""

import base64
import json
import os
import re
import subprocess
import time

HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
RETRY_DELAYS = (5, 20, 60)


class GhError(Exception):
    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def run_cmd(cmd, timeout, poll=None):
    """(exit code, stdout, stderr); exit code None when it timed out. `poll` is called about twice
    a second while gh runs (F3 and the controller during a long download); if it raises, gh is
    killed and the exception passes on."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise GhError("the GitHub CLI (gh) isn't installed or not on PATH")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.5 if poll else max(0.1, deadline - time.monotonic()))
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                proc.kill()
                proc.communicate()
                return None, "", f"timed out after {timeout} s"
            if poll:
                poll()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        raise


def classify(code, out, err):
    text = (err or "").strip() or (out or "").strip()
    m = HTTP_STATUS.search(text)
    status = int(m[1]) if m else None
    if code is None:
        return GhError(text, None, transient=True)
    if status is None:
        # No HTTP status: a network error (proxy, DNS, reset) or gh itself failing.
        auth = re.search(r"auth login|not logged|authentication", text, re.I)
        return GhError(text or f"gh exited with {code}", None, transient=not auth)
    rate = status in (403, 429) and re.search(r"rate limit|secondary", text, re.I)
    return GhError(text, status, transient=status >= 500 or bool(rate))


class GitHub:
    def __init__(self, repo, tmp_dir, runner=run_cmd, sleep=time.sleep, say=None, gh="gh", delays=RETRY_DELAYS):
        self.repo = repo
        self.tmp_dir = tmp_dir
        self.runner = runner
        self.sleep = sleep
        self.say = say or (lambda text: None)
        self.gh_exe = gh
        self.delays = delays
        self.calls = 0
        self.poll = None

    # -- the CLI ------------------------------------------------------------------------------
    def gh(self, args, timeout=180, retry=True):
        last = None
        for attempt in range(len(self.delays) + 1 if retry else 1):
            self.calls += 1
            cmd = [self.gh_exe] + list(args)
            code, out, err = self.runner(cmd, timeout) if self.poll is None else self.runner(cmd, timeout, self.poll)
            if code == 0:
                return out
            last = classify(code, out, err)
            if not last.transient or not retry or attempt == len(self.delays):
                break
            self.say(f"gh {args[0]}: {last} (retrying in {self.delays[attempt]} s)")
            self.sleep(self.delays[attempt])
        raise last

    def api(self, path, method="GET", body=None, raw=False, timeout=180):
        args = ["api", "-X", method, path, "-H",
                "Accept: application/vnd.github.raw" if raw else "Accept: application/vnd.github+json"]
        tmp = None
        if body is not None:
            os.makedirs(self.tmp_dir, exist_ok=True)
            tmp = os.path.join(self.tmp_dir, f"body-{os.getpid()}-{self.calls}.json")
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(body, stream)
            args += ["--input", tmp]
        try:
            out = self.gh(args, timeout)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if raw:
            return out
        return json.loads(out) if out.strip() else None

    # -- builds -------------------------------------------------------------------------------
    def successful_runs(self, workflow, branch, per_page=30):
        """The workflow's successful runs on the branch, newest first, from this repository only
        (pushes and manual runs; a pull request from a fork never counts)."""
        data = self.api(f"repos/{self.repo}/actions/workflows/{workflow}/runs?branch={branch}"
                        f"&status=success&per_page={per_page}")
        out = []
        for run in (data or {}).get("workflow_runs", []):
            repo = ((run.get("repository") or {}).get("full_name") or "").lower()
            head = ((run.get("head_repository") or {}).get("full_name") or "").lower()
            if run.get("conclusion") != "success" or run.get("head_branch") != branch:
                continue
            if repo != self.repo.lower() or head != self.repo.lower():
                continue
            if run.get("event") not in ("push", "workflow_dispatch"):
                continue
            if run.get("path") and not run["path"].endswith("/" + workflow):
                continue
            out.append(run)
        out.sort(key=lambda r: r["run_number"], reverse=True)
        return out

    def artifacts(self, run_id):
        data = self.api(f"repos/{self.repo}/actions/runs/{run_id}/artifacts?per_page=100")
        return (data or {}).get("artifacts", [])

    def download_artifact(self, run_id, name, dest):
        # No retry here: a second download into the same folder could stop on the files the first
        # left. The autopilot tries again at its next poll, into a new folder.
        self.gh(["run", "download", str(run_id), "-R", self.repo, "-n", name, "-D", dest], timeout=3600, retry=False)

    # -- the queue ------------------------------------------------------------------------------
    def list_dir(self, path, ref):
        try:
            data = self.api(f"repos/{self.repo}/contents/{path}?ref={ref}")
        except GhError as error:
            if error.status == 404:
                return []
            raise
        return data if isinstance(data, list) else []

    def read_file(self, path, ref):
        return self.api(f"repos/{self.repo}/contents/{path}?ref={ref}", raw=True)

    # -- the results branch ---------------------------------------------------------------------
    def branch_head(self, branch):
        try:
            data = self.api(f"repos/{self.repo}/git/ref/heads/{branch}")
        except GhError as error:
            if error.status == 404:
                return None
            raise
        return data["object"]["sha"]

    def commit_files(self, branch, files, message, readme=None):
        """One commit with `files` ({path: bytes or a local file's path}) on `branch`, created
        (orphan, with `readme`) if it doesn't exist. Files are read one at a time. A ref that
        moved meanwhile is re-read and the commit made again."""
        blobs = {}
        for path, data in files.items():
            if not isinstance(data, bytes):
                with open(data, "rb") as stream:
                    data = stream.read()
            blob = self.api(f"repos/{self.repo}/git/blobs", "POST",
                            {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
                            timeout=1800)
            blobs[path] = blob["sha"]
        for _ in range(4):
            head = self.branch_head(branch)
            entries = [{"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in blobs.items()]
            body = {"tree": entries}
            if head is None:
                if readme is not None and "README.md" not in blobs:
                    entries.append({"path": "README.md", "mode": "100644", "type": "blob", "content": readme})
            else:
                body["base_tree"] = self.api(f"repos/{self.repo}/git/commits/{head}")["tree"]["sha"]
            tree = self.api(f"repos/{self.repo}/git/trees", "POST", body)["sha"]
            commit = self.api(f"repos/{self.repo}/git/commits", "POST",
                              {"message": message, "tree": tree, "parents": [head] if head else []})["sha"]
            try:
                if head is None:
                    self.api(f"repos/{self.repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": commit})
                else:
                    self.api(f"repos/{self.repo}/git/refs/heads/{branch}", "PATCH", {"sha": commit, "force": False})
                return commit
            except GhError as error:
                if error.status != 422:
                    raise
                self.say(f"{branch} moved while committing; trying again")
        raise GhError(f"{branch} kept moving; the commit was not made", 422, transient=True)
