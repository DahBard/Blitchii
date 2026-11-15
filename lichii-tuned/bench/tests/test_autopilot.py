import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import autopilot  # noqa: E402
import benchplan  # noqa: E402
import fakes  # noqa: E402
import ghrepo  # noqa: E402
import oot_bench  # noqa: E402
import winhost  # noqa: E402
from test_plan import NOPROFILE  # noqa: E402
from test_runner import small_plan  # noqa: E402

REPO = "owner/repo"
WORKFLOW_PATH = ".github/workflows/lichii-tuned.yml"
SHA66 = "bfe83e2de4" + "1" * 30
NOT_FOUND = (1, "", "gh: Not Found (HTTP 404)")
NETWORK_DOWN = (1, "", 'Post "https://api.github.com/graphql": dial tcp: connection refused')


def run_record(number, sha, repo=REPO, head=REPO, event="push", branch="tuned"):
    return {"id": 9000 + number, "run_number": number, "head_sha": sha, "head_branch": branch,
            "conclusion": "success", "event": event, "path": WORKFLOW_PATH,
            "repository": {"full_name": repo}, "head_repository": {"full_name": head}}


def package_files():
    return {"lichii.exe": b"MZ fake", "perf-trace-noprofile.bat": NOPROFILE.encode(),
            "README-tuned.txt": b"fake package\r\n"}


class FakeGh:
    """The GitHub CLI against an in-memory repository: workflow runs and their artifacts, files on
    the build branch, and the git objects of the results branch."""

    def __init__(self):
        self.runs = []
        self.artifacts = {}
        self.packages = {}
        self.tuned = {}
        self.blobs, self.trees, self.commits, self.refs = {}, {}, {}, {}
        self.faults = []          # (regex over the command, (code, out, err)), each used once
        self.calls = []
        self.download_fault = None

    def add_build(self, number, sha, size=200 << 20):
        run = run_record(number, sha)
        self.runs.append(run)
        name = f"lichii-tuned-r{number + 60}-pgo-lto"
        self.artifacts[run["id"]] = [{"name": name, "expired": False, "size_in_bytes": size},
                                     {"name": name + "-symbols", "expired": False, "size_in_bytes": size}]
        self.packages[name] = package_files()
        return run

    # -- the CLI ------------------------------------------------------------------------------
    def __call__(self, cmd, timeout, poll=None):
        self.calls.append(cmd)
        if poll is not None:
            poll()
        line = " ".join(cmd)
        for i, (rx, answer) in enumerate(self.faults):
            if re.search(rx, line):
                del self.faults[i]
                return answer
        if cmd[1] == "run" and cmd[2] == "download":
            return self.download(cmd)
        if cmd[1] != "api":
            return 1, "", f"unknown command {line}"
        method, path = cmd[3], cmd[4]
        raw = "application/vnd.github.raw" in cmd[6]
        body = None
        if "--input" in cmd:
            with open(cmd[cmd.index("--input") + 1], encoding="utf-8") as stream:
                body = json.load(stream)
        try:
            out = self.api(method, path, body, raw)
        except KeyError:
            return NOT_FOUND
        except ValueError as error:
            return 1, "", f"gh: {error} (HTTP 422)"
        return 0, out if raw else json.dumps(out), ""

    def download(self, cmd):
        name, dest = cmd[cmd.index("-n") + 1], cmd[cmd.index("-D") + 1]
        os.makedirs(dest, exist_ok=True)
        files = self.packages[name]
        for i, (rel, data) in enumerate(files.items()):
            if self.download_fault and i == 1:
                return self.download_fault
            with open(os.path.join(dest, rel), "wb") as stream:
                stream.write(data)
        return 0, "", ""

    def api(self, method, path, body, raw):
        path, _, query = path.partition("?")
        p = path.split("/")
        assert p[0] == "repos" and f"{p[1]}/{p[2]}" == REPO, path
        rest = p[3:]
        if rest[:2] == ["actions", "workflows"] and len(rest) == 4 and rest[3] == "runs":
            return {"workflow_runs": list(self.runs)}
        if rest[:2] == ["actions", "workflows"] and len(rest) == 3:
            return {"path": WORKFLOW_PATH}
        if rest[:2] == ["actions", "runs"] and rest[3] == "artifacts":
            return {"artifacts": self.artifacts[int(rest[2])]}
        if rest[0] == "contents":
            target = "/".join(rest[1:])
            assert query == "ref=tuned", query
            if target in self.tuned:
                return self.tuned[target]
            entries = [{"name": k.rsplit("/", 1)[1], "path": k, "type": "file",
                        "sha": hashlib.sha1(v.encode()).hexdigest()} for k, v in sorted(self.tuned.items())
                       if k.rsplit("/", 1)[0] == target]
            if not entries:
                raise KeyError(target)
            return entries
        if rest[:3] == ["git", "ref", "heads"]:
            return {"object": {"sha": self.refs["/".join(rest[3:])]}}
        if rest[:2] == ["git", "blobs"]:
            data = base64.b64decode(body["content"])
            sha = hashlib.sha1(data).hexdigest()
            self.blobs[sha] = data
            return {"sha": sha}
        if rest[:2] == ["git", "commits"] and method == "GET":
            return {"tree": {"sha": self.commits[rest[2]]["tree"]}}
        if rest[:2] == ["git", "trees"]:
            tree = dict(self.trees[body["base_tree"]]) if "base_tree" in body else {}
            for e in body["tree"]:
                if "content" in e:
                    data = e["content"].encode()
                    sha = hashlib.sha1(data).hexdigest()
                    self.blobs[sha] = data
                    tree[e["path"]] = sha
                else:
                    tree[e["path"]] = e["sha"]
            sha = hashlib.sha1(json.dumps(sorted(tree.items())).encode()).hexdigest()
            self.trees[sha] = tree
            return {"sha": sha}
        if rest[:2] == ["git", "commits"]:
            sha = hashlib.sha1(json.dumps([body, len(self.commits)]).encode()).hexdigest()
            self.commits[sha] = body
            return {"sha": sha}
        if rest[:2] == ["git", "refs"] and method == "POST":
            branch = body["ref"][len("refs/heads/"):]
            if branch in self.refs:
                raise ValueError("Reference already exists")
            self.refs[branch] = body["sha"]
            return {}
        if rest[:3] == ["git", "refs", "heads"] and method == "PATCH":
            branch = "/".join(rest[3:])
            if self.commits[body["sha"]]["parents"] != [self.refs[branch]]:
                raise ValueError("Update is not a fast forward")
            self.refs[branch] = body["sha"]
            return {}
        raise KeyError(path)

    def branch_files(self, branch="bench-results"):
        if branch not in self.refs:
            return {}
        tree = self.trees[self.commits[self.refs[branch]]["tree"]]
        return {path: self.blobs[sha] for path, sha in tree.items()}

    def branch_json(self, path, branch="bench-results"):
        return json.loads(self.branch_files(branch)[path])


class AutoHost(fakes.FakeHost):
    """The fake host plus the owner: keyboard/mouse input times, controller activity, and a
    trigger that adds input when the fake world reaches a state."""

    def __init__(self, world):
        super().__init__(world)
        self.inputs = []
        self.pad = []
        self.trigger = None
        self.busy = None
        self.free = 500 << 30
        self.launch_times = []
        self.started = []
        self.awake_calls = []

    def fire(self):
        if self.trigger and self.trigger(self.world):
            self.trigger = None
            self.on_trigger()

    def on_trigger(self):
        self.inputs.append(self.world.t)

    def input_idle_s(self):
        self.fire()
        past = [t for t in self.inputs if t <= self.world.t]
        return self.world.t - max(past) if past else 1e9

    def controller_active(self):
        self.fire()
        return any(s <= self.world.t < e for s, e in self.pad)

    def user_busy(self):
        return self.busy

    def disk_free(self, path):
        return self.free

    def launch(self, cmd, env, cwd):
        self.launch_times.append(self.world.t)
        return super().launch(cmd, env, cwd)

    def start_file(self, path):
        self.started.append(path)

    def keep_awake(self, on):
        self.awake_calls.append(on)


class AutopilotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.old_data = oot_bench.DATA
        oot_bench.DATA = os.path.join(root, "LichiiBench")
        os.makedirs(oot_bench.DATA)
        oot_bench.save_refs(fakes.refs_for_flow())
        self.builds = os.path.join(root, "Forked Lichii Builds")
        os.makedirs(self.builds)
        self.world = fakes.World(os.path.join(root, "lichii", "log"))
        self.host = AutoHost(self.world)
        data_root = os.path.join(root, "lichii")
        self.save_dir = os.path.join(data_root, "nand", "user", "save", "0000000000000000", "1", "0400486024DEC000")
        os.makedirs(self.save_dir)
        with open(os.path.join(self.save_dir, "file1.bin"), "wb") as stream:
            stream.write(b"save")
        self.local = {"build_dir": os.path.join(self.builds, "r65"), "exe": "lichii.exe",
                      "game_file": os.path.join(root, "oot.nsp"), "log_dir": self.world.log_dir,
                      "data_root": data_root, "config_file": os.path.join(data_root, "config", "qt-config.ini"),
                      "hotkeys": oot_bench.DEFAULT_HOTKEYS, "game_title": "Ocarina of Time",
                      "check_base": "perf-trace-noprofile.bat"}
        self.gh = FakeGh()
        self.cfg = dict(autopilot.DEFAULT_CONFIG, repo=REPO, builds_dir=self.builds)
        self.lines = []

    def tearDown(self):
        oot_bench.DATA = self.old_data
        self.tmp.cleanup()

    def pilot(self, **cfg):
        config = dict(self.cfg, **cfg)
        gh = ghrepo.GitHub(REPO, os.path.join(oot_bench.DATA, "autopilot", "tmp"), runner=self.gh, delays=(1, 2, 3))
        return autopilot.Autopilot(self.host, gh, config, self.local, say=self.lines.append,
                                   runner_kwargs={"client_factory": lambda: fakes.FakeClient(self.world),
                                                  "threaded": False})

    def queue(self, name, plan):
        self.gh.tuned[f"lichii-tuned/bench/queue/{name}.json"] = plan if isinstance(plan, str) else json.dumps(plan)

    def state(self):
        return oot_bench.read_json(os.path.join(oot_bench.DATA, "autopilot", "state.json"))

    def log_text(self):
        return "\n".join(self.lines)


class InstallTest(AutopilotCase):
    def test_the_newest_run_is_installed_beside_the_old_builds(self):
        old = os.path.join(self.builds, "r65")
        os.makedirs(old)
        with open(os.path.join(old, "lichii.exe"), "wb") as stream:
            stream.write(b"old build")
        self.gh.add_build(5, "a" * 40)
        self.gh.add_build(6, SHA66)
        ap = self.pilot()
        ap.sync()
        dest = os.path.join(self.builds, "r66")
        self.assertTrue(os.path.isfile(os.path.join(dest, "lichii.exe")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "bench", "oot_bench.py")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "bench.bat")))
        self.assertFalse(os.path.exists(os.path.join(dest, "bench", "tests")))
        self.assertEqual(oot_bench.read_json(os.path.join(dest, "bench-build.json"))["sha"], SHA66)
        with open(os.path.join(old, "lichii.exe"), "rb") as stream:
            self.assertEqual(stream.read(), b"old build")
        self.assertEqual(sorted(self.state()["builds"]), ["66"])
        self.assertEqual([n for n in os.listdir(self.builds) if n.startswith(".")], [])
        # Only the newest: r65 isn't downloaded, and the next poll downloads nothing.
        downloads = [c for c in self.gh.calls if c[1] == "run"]
        self.assertEqual(len(downloads), 1)
        ap.sync()
        self.assertEqual(len([c for c in self.gh.calls if c[1] == "run"]), 1)

    def test_an_existing_folder_is_taken_as_the_build_and_not_overwritten(self):
        dest = os.path.join(self.builds, "r66")
        os.makedirs(dest)
        with open(os.path.join(dest, "lichii.exe"), "wb") as stream:
            stream.write(b"owner's copy")
        self.gh.add_build(6, SHA66)
        self.pilot().sync()
        with open(os.path.join(dest, "lichii.exe"), "rb") as stream:
            self.assertEqual(stream.read(), b"owner's copy")
        self.assertEqual(self.state()["builds"]["66"]["source"], "existing folder")
        self.assertEqual([c for c in self.gh.calls if c[1] == "run"], [])

    def test_a_folder_without_lichii_is_left_alone(self):
        dest = os.path.join(self.builds, "r66")
        os.makedirs(dest)
        self.gh.add_build(6, SHA66)
        self.pilot().sync()
        self.assertEqual(os.listdir(dest), [])
        self.assertEqual(self.state()["builds"], {})

    def test_a_newest_run_without_the_package_falls_back_to_the_one_before(self):
        self.gh.add_build(6, SHA66)
        run = self.gh.add_build(7, "b" * 40)
        self.gh.artifacts[run["id"]] = [{"name": "lichii-tuned-r67-pgo-train", "expired": False, "size_in_bytes": 1}]
        self.pilot().sync()
        self.assertEqual(sorted(self.state()["builds"]), ["66"])

    def test_runs_from_elsewhere_are_never_installed(self):
        self.gh.runs.append(run_record(7, "c" * 40, head="someone/fork"))
        self.gh.runs.append(run_record(8, "d" * 40, event="pull_request"))
        self.gh.runs.append(run_record(9, "e" * 40, branch="other"))
        self.gh.add_build(6, SHA66)
        self.pilot().sync()
        self.assertEqual(sorted(self.state()["builds"]), ["66"])

    def test_low_disk_space_skips_the_download(self):
        self.host.free = 5 << 30
        self.gh.add_build(6, SHA66)
        ap = self.pilot()
        ap.sync()
        self.assertEqual(os.listdir(self.builds), [])
        self.assertIn("delete old builds yourself", self.log_text())

    def test_a_failed_download_leaves_no_folder_and_is_tried_again(self):
        self.gh.add_build(6, SHA66)
        self.gh.download_fault = (1, "", "error downloading: unexpected EOF")
        ap = self.pilot()
        with self.assertRaises(ghrepo.GhError):
            ap.sync()
        self.assertEqual(os.listdir(self.builds), [])
        self.assertEqual(len([c for c in self.gh.calls if c[1] == "run"]), 1)
        self.gh.download_fault = None
        ap.sync()
        self.assertTrue(os.path.isfile(os.path.join(self.builds, "r66", "lichii.exe")))

    def test_a_stale_staging_folder_is_removed_at_start(self):
        stale = os.path.join(self.builds, ".bench-download-r66-x")
        os.makedirs(stale)
        open(os.path.join(stale, autopilot.STAGING_MARK), "w").close()
        other = os.path.join(self.builds, ".bench-download-not-ours")
        os.makedirs(other)
        self.pilot().recover()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(other))


class GitHubTest(AutopilotCase):
    def test_network_errors_are_retried(self):
        gh = ghrepo.GitHub(REPO, self.tmp.name, runner=self.gh, sleep=lambda s: None)
        self.gh.add_build(6, SHA66)
        self.gh.faults = [("workflows", NETWORK_DOWN), ("workflows", (1, "", "gh: Server Error (HTTP 502)"))]
        self.assertEqual(len(gh.successful_runs("lichii-tuned.yml", "tuned")), 1)

    def test_a_client_error_is_not_retried(self):
        gh = ghrepo.GitHub(REPO, self.tmp.name, runner=self.gh, sleep=lambda s: None)
        with self.assertRaises(ghrepo.GhError) as caught:
            gh.read_file("nothing/here.json", "tuned")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(self.gh.calls), 1)

    def test_commits_create_the_branch_then_add_to_it(self):
        gh = ghrepo.GitHub(REPO, self.tmp.name, runner=self.gh, sleep=lambda s: None)
        gh.commit_files("bench-results", {"a.txt": b"one"}, "first", readme="hello")
        path = os.path.join(self.tmp.name, "b.bin")
        with open(path, "wb") as stream:
            stream.write(b"two")
        gh.commit_files("bench-results", {"b.bin": path}, "second")
        files = self.gh.branch_files()
        self.assertEqual(files, {"a.txt": b"one", "README.md": b"hello", "b.bin": b"two"})

    def test_a_moved_branch_is_read_again(self):
        gh = ghrepo.GitHub(REPO, self.tmp.name, runner=self.gh, sleep=lambda s: None)
        gh.commit_files("bench-results", {"a.txt": b"one"}, "first")
        real = self.gh.api

        def racing(method, path, body, raw):
            if method == "PATCH" and not getattr(self, "raced", False):
                self.raced = True
                head = self.gh.refs["bench-results"]
                tree = self.gh.commits[head]["tree"]
                other = {"message": "other", "tree": tree, "parents": [head]}
                sha = "f" * 40
                self.gh.commits[sha] = other
                self.gh.refs["bench-results"] = sha
            return real(method, path, body, raw)
        self.gh.api = racing
        gh.commit_files("bench-results", {"b.txt": b"two"}, "second")
        self.assertEqual(sorted(self.gh.branch_files()), ["a.txt", "b.txt"])
        self.assertEqual(self.gh.commits[self.gh.refs["bench-results"]]["parents"], ["f" * 40])


class QueueTest(AutopilotCase):
    def test_queue_states(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-good", small_plan(build="r66"))
        self.queue("0002-not-json", "{nope")
        self.queue("0003-later", small_plan(build="r70"))
        self.queue("0004-outside", small_plan(build="r66", arms={
            "N-a": {"base": "perf-trace-noprofile.bat", "env": {"EDEN_NX2_FRAME_DUMP": "/etc/elsewhere"}},
            "N-b": {"base": "perf-trace-noprofile.bat", "env": {"EDEN_NX2_FRAME_DUMP": "/etc/elsewhere"}}}))
        self.gh.tuned["lichii-tuned/bench/queue/README.txt"] = "not a plan"
        ap = self.pilot()
        ap.sync()
        plans = self.state()["plans"]
        self.assertEqual(sorted(plans), ["0001-good", "0002-not-json", "0003-later", "0004-outside"])
        self.assertEqual(plans["0002-not-json"]["status"], "refused")
        ap.state["builds"]["66"]["prepare"] = {"status": "ok"}
        work = ap.next_work()
        self.assertEqual(work[:2], ("plan", "0001-good"))
        self.assertEqual(ap.state["plans"]["0003-later"]["status"], "waiting")
        self.assertIn("r70 isn't installed", ap.state["plans"]["0003-later"]["reason"])
        ap.set_plan("0001-good", "done")
        ap.run_plan("0004-outside")
        self.assertEqual(ap.state["plans"]["0004-outside"]["status"], "refused")
        self.assertIn("outside the bench's data and build folders", ap.state["plans"]["0004-outside"]["reason"])
        # A plan whose file leaves the queue is withdrawn.
        del self.gh.tuned["lichii-tuned/bench/queue/0003-later.json"]
        ap.sync_queue()
        self.assertEqual(ap.state["plans"]["0003-later"]["status"], "withdrawn")

    def test_a_plan_withdrawn_while_it_waits_for_the_owner_doesnt_run(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", small_plan(build="r66"))
        ap = self.pilot()
        ap.sync()
        ap.state["builds"]["66"]["prepare"] = {"status": "ok"}
        ap.next_poll = 0.0

        def withdraw_then_ready(context, sitting=False):
            del self.gh.tuned["lichii-tuned/bench/queue/0001-aa.json"]
            ap.sync_queue()
        ap.wait_ready = withdraw_then_ready
        ap.run_plan("0001-aa")
        self.assertEqual(ap.state["plans"]["0001-aa"]["status"], "withdrawn")
        self.assertEqual(self.host.launch_times, [])
        self.assertFalse(os.path.isdir(os.path.join(oot_bench.DATA, "sessions")) and
                         os.listdir(os.path.join(oot_bench.DATA, "sessions")))

    def test_a_plan_for_an_expired_build_is_refused(self):
        run = self.gh.add_build(6, SHA66)
        self.gh.add_build(7, "b" * 40)
        for art in self.gh.artifacts[run["id"]]:
            art["expired"] = True
        self.queue("0001-old", small_plan(build="r66"))
        ap = self.pilot()
        ap.sync()
        self.assertEqual(ap.state["plans"]["0001-old"]["status"], "refused")
        self.assertIn("expired", ap.state["plans"]["0001-old"]["reason"])

    def test_a_plan_naming_an_older_build_installs_it(self):
        self.gh.add_build(6, SHA66)
        self.gh.add_build(7, "b" * 40)
        self.queue("0001-old", small_plan(build="bfe83e2de4"))
        ap = self.pilot()
        ap.sync()
        self.assertEqual(sorted(ap.state["builds"]), ["66", "67"])

    def test_path_rules(self):
        roots = [os.path.join(self.tmp.name, "a")]
        ok = {"arms": {"x": {"base": "perf-trace.bat", "env": {"EDEN_NX2_FRAME_DUMP": os.path.join(roots[0], "d"),
                                                                "EDEN_NX2_TEST_1216": "1"}}}}
        autopilot.check_plan_paths(ok, roots)
        for env in ({"V": os.path.join(roots[0], "..", "x")}, {"V": "relative/dir"}, {"V": "/elsewhere"}):
            with self.assertRaises(benchplan.PlanError):
                autopilot.check_plan_paths({"arms": {"x": {"env": env}}}, roots)
        with self.assertRaises(benchplan.PlanError):
            autopilot.check_plan_paths({"arms": {"x": {"base": "..\\other\\perf.bat"}}}, roots)


class LoopTest(AutopilotCase):
    def plan(self, **over):
        return small_plan(build="r66", **over)

    def test_once_installs_prepares_runs_and_publishes(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        ap = self.pilot()
        self.assertEqual(ap.run(once=True), 0)
        state = self.state()
        self.assertEqual(state["builds"]["66"]["prepare"]["status"], "ok", self.log_text())
        self.assertEqual(state["plans"]["0001-aa"]["status"], "done", self.log_text())
        files = self.gh.branch_files()
        status = json.loads(files["status/plans/0001-aa.json"])
        self.assertEqual(status["status"], "done")
        session = status["session"]
        result = json.loads(files[f"results/{session}/result.json"])
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["build"]["r"], 66)
        z = zipfile.ZipFile(__import__("io").BytesIO(files[f"results/{session}/{result['parts'][0]}"]))
        self.assertIn(f"{session}/summary.txt", z.namelist())
        self.assertIn(f"results/{session}/summary.txt", files)
        self.assertIn("README.md", files)
        beat = json.loads(files["status/autopilot.json"])
        self.assertEqual(beat["state"], "finished")
        self.assertEqual(beat["builds"][0]["prepare"], "ok")
        self.assertEqual(beat["builds"][0]["folder"], "r66")
        prepares = [k for k in files if k.endswith("result.json") and json.loads(files[k])["kind"] == "prepare"]
        self.assertEqual(len(prepares), 1)
        # The plan's build went into the log check: the session's plan names r66's commit.
        self.assertEqual(oot_bench.read_json(os.path.join(oot_bench.DATA, "sessions", session, "plan.json"))["build"],
                         "bfe83e2de4")
        self.assertFalse(self.world.alive)
        self.assertEqual(os.listdir(os.path.join(oot_bench.DATA, "autopilot", "outbox")), [])
        self.assertTrue(os.path.exists(os.path.join(oot_bench.DATA, "autopilot.log")))
        # The second pass has nothing left to do.
        launches = len(self.host.launch_times)
        self.pilot().run(once=True)
        self.assertEqual(len(self.host.launch_times), launches)

    def test_no_launch_while_the_owner_is_active(self):
        self.gh.add_build(6, SHA66)
        start = self.world.t
        self.host.inputs = [start, start + 300]
        self.pilot().run(once=True)
        self.assertGreaterEqual(self.host.launch_times[0], start + 300 + 900)

    def test_owner_input_stops_the_launch_and_the_slot_runs_again(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        # Input while launch 3 (the plan's second) stands at the fountain.
        self.host.trigger = lambda w: w.launches == 3 and w.screen == "fountain" and w.t - w.screen_since > 5
        self.pilot().run(once=True)
        state = self.state()
        self.assertEqual(state["plans"]["0001-aa"]["status"], "done", self.log_text())
        self.assertEqual(state["counters"]["owner_interruptions"], 1)
        when = self.host.inputs[0]
        later = [t for t in self.host.launch_times if t > when]
        self.assertGreaterEqual(later[0], when + 900)
        session = state["plans"]["0001-aa"]["session"]
        runs = oot_bench.read_json(os.path.join(oot_bench.DATA, "sessions", session, "runs.json"))
        self.assertEqual([(r["label"], r["status"]) for r in runs["runs"]],
                         [("01-W-N-a", "ok"), ("03-N-a", "ok"), ("04-N-b", "ok"), ("05-N-b", "ok"), ("06-N-a", "ok")])
        self.assertTrue(any("02-N-a was stopped by the owner's input" in n for n in runs["notes"]))
        interrupted = oot_bench.read_json(os.path.join(oot_bench.DATA, "sessions", session, "runs", "02-N-a", "run.json"))
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertIn("keyboard or mouse", interrupted["reason"])
        self.assertEqual(interrupted["slot"], 2)

    def test_controller_input_counts(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        self.host.trigger = lambda w: w.launches == 2 and w.screen == "menu3"
        self.host.on_trigger = lambda: self.host.pad.append((self.world.t, self.world.t + 2))
        self.pilot().run(once=True)
        state = self.state()
        self.assertEqual(state["plans"]["0001-aa"]["status"], "done", self.log_text())
        self.assertEqual(state["counters"]["owner_interruptions"], 1)
        self.assertIn("owner input (controller)", self.log_text())

    def test_owner_input_during_the_preparation(self):
        self.gh.add_build(6, SHA66)
        self.host.trigger = lambda w: w.launches == 1 and w.screen == "title"
        self.pilot().run(once=True)
        prep = self.state()["builds"]["66"]["prepare"]
        self.assertEqual(prep["status"], "ok")
        self.assertEqual(prep["interrupted"], 1)
        self.assertEqual(prep["attempts"], 1)
        self.assertEqual(len(self.host.launch_times), 2)

    def test_f3_while_waiting_stops_the_autopilot(self):
        self.gh.add_build(6, SHA66)
        self.host.inputs = [self.world.t]
        self.world.f3_at = self.world.t + 120
        self.pilot().run(once=False)
        self.assertEqual(self.host.launch_times, [])
        self.assertEqual(self.gh.branch_json("status/autopilot.json")["state"], "stopped: F3")

    def test_f3_during_a_sitting_aborts_the_plan(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        ap = self.pilot()
        ap.sync()
        ap.state["builds"]["66"]["prepare"] = {"status": "ok"}
        self.host.trigger = lambda w: w.launches == 2 and w.screen == "fountain"
        self.host.on_trigger = lambda: setattr(self.world, "f3_at", self.world.t)
        ap.run(once=False)
        self.assertEqual(ap.state["plans"]["0001-aa"]["status"], "aborted")
        self.assertFalse(self.world.alive)
        self.assertEqual(self.gh.branch_json("status/plans/0001-aa.json")["status"], "aborted")

    def test_the_control_file_stops_after_the_launch_and_requeues(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        control = os.path.join(oot_bench.DATA, "autopilot", "control.json")
        self.host.trigger = lambda w: w.launches == 2 and w.screen == "fountain"
        self.host.on_trigger = lambda: oot_bench.write_json(control, {"stop": True})
        ap = self.pilot()
        ap.run(once=False)
        p = ap.state["plans"]["0001-aa"]
        self.assertEqual(p["status"], "queued")
        self.assertEqual(len(self.host.launch_times), 2)
        self.assertFalse(self.world.alive)
        # The next start clears the stop and runs the plan again from the start.
        self.pilot().run(once=True)
        self.assertEqual(self.state()["plans"]["0001-aa"]["status"], "done", self.log_text())
        self.assertEqual(self.state()["plans"]["0001-aa"]["starts"], 2)

    def test_a_restart_during_a_sitting_requeues_it(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        ap = self.pilot()
        ap.sync()
        ap.state["builds"]["66"]["prepare"] = {"status": "ok"}
        session = oot_bench.new_session(ap.state["plans"]["0001-aa"]["plan"])
        ap.set_plan("0001-aa", "running", session=os.path.basename(session), starts=1, build_r=66)
        ap.save()
        self.pilot().run(once=True)
        p = self.state()["plans"]["0001-aa"]
        self.assertEqual(p["status"], "done", self.log_text())
        self.assertEqual(p["starts"], 2)
        results = [json.loads(v) for k, v in self.gh.branch_files().items() if k.endswith("result.json")]
        self.assertEqual(sorted(r["status"] for r in results if r["kind"] == "sitting"), ["finished", "interrupted"])

    def test_an_unexpected_error_requeues_the_plan_and_the_loop_goes_on(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        real = oot_bench.end_session
        calls = []

        def broken(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk trouble")
            return real(*args, **kwargs)
        oot_bench.end_session = broken
        try:
            self.pilot().run(once=True)
        finally:
            oot_bench.end_session = real
        p = self.state()["plans"]["0001-aa"]
        self.assertEqual((p["status"], p["starts"]), ("done", 2), self.log_text())
        self.assertIn("unexpected error: RuntimeError('disk trouble')", self.log_text())

    def test_network_failure_backs_off_and_recovers(self):
        self.gh.add_build(6, SHA66)
        self.gh.faults = [("contents", NETWORK_DOWN)] * 8
        ap = self.pilot()
        ap.run(once=True)
        self.assertIn("next try in", self.log_text())
        self.assertEqual(ap.state["builds"], {})
        self.gh.faults = []
        self.pilot().run(once=True)
        self.assertEqual(self.state()["builds"]["66"]["prepare"]["status"], "ok")

    def test_a_save_change_is_flagged_and_backed_up(self):
        self.gh.add_build(6, SHA66)
        self.host.trigger = lambda w: w.launches == 1 and w.screen == "fountain"

        def write_save():
            with open(os.path.join(self.save_dir, "file1.bin"), "wb") as stream:
                stream.write(b"changed")
        # Not input: the game writing its save during a bench launch.
        self.host.on_trigger = write_save
        self.pilot().run(once=True)
        self.assertEqual(len(self.state()["save_changes"]), 1)
        self.assertTrue(os.listdir(os.path.join(oot_bench.DATA, "save-backups")))

    def test_a_save_change_during_an_interrupted_launch_is_flagged(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        self.host.trigger = lambda w: w.launches == 3 and w.screen == "fountain" and w.t - w.screen_since > 5

        def write_save_and_touch():
            with open(os.path.join(self.save_dir, "file1.bin"), "wb") as stream:
                stream.write(b"changed")
            self.host.inputs.append(self.world.t)
        self.host.on_trigger = write_save_and_touch
        self.pilot().run(once=True)
        state = self.state()
        self.assertEqual(state["plans"]["0001-aa"]["status"], "done", self.log_text())
        self.assertEqual([c["during"] for c in state["save_changes"]], ["launch 02-N-a"])
        session = state["plans"]["0001-aa"]["session"]
        interrupted = oot_bench.read_json(os.path.join(oot_bench.DATA, "sessions", session, "runs", "02-N-a", "run.json"))
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertIn("Ocarina's save folder changed during this launch", interrupted["flags"])

    def test_a_preparation_that_cant_start_is_not_an_attempt(self):
        self.gh.add_build(6, SHA66)
        ap = self.pilot()
        ap.sync()
        ap.wait_ready = lambda *args, **kwargs: None
        self.host.reboot_pending = lambda: True
        ap.prepare_build("66")
        prep = self.state()["builds"]["66"]["prepare"]
        self.assertIsNone(prep.get("status"))
        self.assertNotIn("attempts", prep)
        self.assertEqual(self.host.launch_times, [])
        self.assertIn("prepare r66 waits", self.log_text())

    def test_a_fatal_preparation_is_published(self):
        self.gh.add_build(6, "a" * 40)  # the log names another build: fatal
        ap = self.pilot()
        ap.sync()
        ap.prepare_build("66")
        ap.publish_outbox()
        prep = self.state()["builds"]["66"]["prepare"]
        self.assertEqual(prep["last"]["status"], "fatal", self.log_text())
        self.assertTrue(prep["last"]["session"])
        files = self.gh.branch_files()
        self.assertIn(f"results/{prep['last']['session']}/result.json", files)

    def test_a_large_zip_is_split(self):
        self.gh.add_build(6, SHA66)
        self.queue("0001-aa", self.plan())
        self.pilot(part_mb=0.002).run(once=True)
        files = self.gh.branch_files()
        session = json.loads(files["status/plans/0001-aa.json"])["session"]
        result = json.loads(files[f"results/{session}/result.json"])
        self.assertGreater(len(result["parts"]), 1)
        whole = b"".join(files[f"results/{session}/{p}"] for p in result["parts"])
        self.assertEqual(hashlib.sha256(whole).hexdigest(), result["zip_sha256"])
        self.assertTrue(all(len(files[f"results/{session}/{p}"]) <= int(0.002 * (1 << 20)) for p in result["parts"]))

    def test_paused_from_the_queue(self):
        self.gh.add_build(6, SHA66)
        self.gh.tuned["lichii-tuned/bench/queue/control.json"] = json.dumps({"pause": True})
        ap = self.pilot()
        ap.sync()
        self.assertEqual(ap.not_ready(), "paused by the queue's control.json")


class CommandsTest(AutopilotCase):
    def test_install_autostart(self):
        oot_bench.write_json(os.path.join(oot_bench.DATA, "local.json"), self.local)
        appdata = os.path.join(self.tmp.name, "AppData")
        answers = iter([REPO, self.builds])
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = appdata
        try:
            entry = autopilot.cmd_install_autostart(
                self.host, ask=lambda q: next(answers), say=self.lines.append,
                gh_factory=lambda repo: ghrepo.GitHub(repo, self.tmp.name, runner=self.gh, sleep=lambda s: None))
        finally:
            if old is None:
                del os.environ["APPDATA"]
            else:
                os.environ["APPDATA"] = old
        with open(entry, encoding="utf-8") as stream:
            text = stream.read()
        self.assertIn("bench.bat autopilot", text)
        app = os.path.join(oot_bench.DATA, "app")
        self.assertIn(app, text)
        self.assertTrue(os.path.isfile(os.path.join(app, "bench", "autopilot.py")))
        self.assertTrue(os.path.isfile(os.path.join(app, "bench.bat")))
        self.assertEqual(self.host.started, [entry])
        cfg = autopilot.load_config()
        self.assertEqual((cfg["repo"], cfg["builds_dir"]), (REPO, self.builds))

    def test_one_instance(self):
        path = os.path.join(self.tmp.name, "lock")
        with open(path, "a+") as a, open(path, "a+") as b:
            self.assertTrue(winhost.lock_exclusive(a))
            self.assertFalse(winhost.lock_exclusive(b))

    def test_autopilot_stop_writes_the_control_file(self):
        class Args:
            command = "autopilot-stop"
        self.assertEqual(autopilot.main(Args(), say=self.lines.append), 0)
        self.assertTrue(oot_bench.read_json(os.path.join(oot_bench.DATA, "autopilot", "control.json"))["stop"])

    def test_split_file(self):
        path = os.path.join(self.tmp.name, "x.zip")
        with open(path, "wb") as stream:
            stream.write(os.urandom(2500))
        out = os.path.join(self.tmp.name, "parts")
        os.makedirs(out)
        parts = autopilot.split_file(path, 1000, out)
        self.assertEqual([n for n, _ in parts], ["x.zip.001", "x.zip.002", "x.zip.003"])
        self.assertEqual(autopilot.split_file(path, 5000, out), [("x.zip", path)])


class OwnerTest(unittest.TestCase):
    def test_a_gap_in_the_controller_polling_counts_as_use(self):
        world = fakes.World(tempfile.gettempdir())
        host = AutoHost(world)
        owner = autopilot.Owner(host)
        self.assertEqual(owner.idle_s(), 1e9)
        world.advance(60)
        self.assertEqual(owner.idle_s(), 1e9)
        world.advance(600)  # not polled for 10 min: a controller could have been in use
        self.assertEqual(owner.idle_s(), 0)
        world.advance(30)
        self.assertEqual(owner.idle_s(), 30)
        # Input during a launch is judged on what was seen, not on the gap.
        self.assertIsNone(owner.input_since(world.t - 1))


class RunCmdTest(unittest.TestCase):
    def test_output_and_exit_code(self):
        code, out, err = ghrepo.run_cmd([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], 30)
        self.assertEqual((code, out.strip()), (3, "hi"))

    def test_a_poll_that_raises_kills_gh(self):
        calls = []

        def poll():
            calls.append(1)
            if len(calls) == 2:
                raise oot_bench.Aborted("F3")
        with self.assertRaises(oot_bench.Aborted):
            ghrepo.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], 60, poll)
        self.assertEqual(len(calls), 2)

    def test_timeout(self):
        code, _, err = ghrepo.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], 1, lambda: None)
        self.assertIsNone(code)
        self.assertIn("timed out", err)


class WinHostLogicTest(unittest.TestCase):
    def test_idle_ticks_wrap(self):
        self.assertEqual(winhost.idle_from_ticks(5000, 2000), 3.0)
        self.assertEqual(winhost.idle_from_ticks(1000, 0xFFFFFFFF - 999), 2.0)

    def test_pad_activity(self):
        self.assertFalse(winhost.pad_active(0, 0, 0, (1200, -3000, 0, 9000)))
        self.assertTrue(winhost.pad_active(0x1000, 0, 0, (0, 0, 0, 0)))
        self.assertTrue(winhost.pad_active(0, 200, 0, (0, 0, 0, 0)))
        self.assertTrue(winhost.pad_active(0, 0, 0, (0, 0, -30000, 0)))


if __name__ == "__main__":
    unittest.main()
