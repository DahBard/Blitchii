"""Lichii Bench autopilot: new CI builds, their shader preparation and queued plans, unattended.

    bench.bat autopilot [--once] [--idle-min N]   the loop (--once: the work there is now, then exit)
    bench.bat autopilot-stop                      stop after the current launch
    bench.bat install-autostart                   start it at every logon (asks for the repository
                                                  and the builds folder once)
    bench.bat uninstall-autostart

Every few minutes it asks GitHub for the newest successful build workflow run on the build branch
and installs its package into a new folder <builds>\\r<N> (never over an existing folder, never
deleting one), prepares each new build once, and runs the plans queued in the repository's queue
folder in name order, each on the build it names. Each finished sitting's zip and status go to
the results branch, with a heartbeat about every 30 min (QUEUE.md has the layout).

Safety: one instance (a lock file); launches only when the owner has been idle 15 min, the
desktop isn't locked or taken by a full-screen program, Lichii isn't running and the laptop is on
AC; any keyboard, mouse or controller input during a launch closes Lichii, and the launch runs
again once he has been idle 15 min. Hold F3 to stop (the sitting is kept and marked aborted).
Only this repository's own workflow runs are installed. The bench never writes to the saves; a
save that changes during one of its launches is backed up and flagged.
"""

import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import benchplan  # noqa: E402
import oot_bench  # noqa: E402
import winhost  # noqa: E402
from ghrepo import GhError, GitHub  # noqa: E402
from oot_bench import Aborted, Fatal, read_json, write_json  # noqa: E402

VERSION = "autopilot 1"
GIB = 1 << 30
MIB = 1 << 20

DEFAULT_CONFIG = {
    "repo": None,                    # owner/name of the repository whose CI builds Lichii
    "branch": "tuned",
    "workflow": "lichii-tuned.yml",
    "variant": "pgo-lto",
    "build_offset": 60,              # r<N> = run number + 60
    "builds_dir": None,              # the folder with one folder per build
    "folder": "r{r}",
    "queue": "lichii-tuned/bench/queue",
    "results_branch": "bench-results",
    "install_newest": True,
    "poll_s": 300,
    "heartbeat_s": 1800,
    "idle_s": 900,
    "min_free_gb": 10,
    "part_mb": 25,
    "max_zip_mb": 400,
    "max_pause_h": 6,
    "max_plan_starts": 3,
    "max_prepare_attempts": 2,
    "max_interrupts_per_slot": 6,
}

FINAL = frozenset({"done", "stopped", "refused", "aborted", "withdrawn", "gave-up"})
PLAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
STAGING_MARK = ".lichii-bench-download"
STARTUP_NAME = "Lichii Bench autopilot.cmd"

RESULTS_README = """# Lichii Bench results

Written by the bench autopilot on the test PC; see lichii-tuned/bench/QUEUE.md on the build
branch. Nothing else should commit here.

- `status/autopilot.json`: the heartbeat (about every 30 min, and at each change).
- `status/plans/<plan>.json`: each queued plan's status.
- `results/<session>/`: one folder per sitting or preparation: `result.json`, `summary.txt`,
  `runs.json` and the zip (`<session>.zip`, or `<session>.zip.001`, `.002`, ... to concatenate).
"""


class OwnerActive(Aborted):
    """The owner used the keyboard, mouse or a controller during a launch."""


class StopAutopilot(Exception):
    """F3, Ctrl+C or the control file: leave the loop."""


def ap_dir():
    return os.path.join(oot_bench.DATA, "autopilot")


def ap_path(*names):
    return os.path.join(ap_dir(), *names)


def log_path():
    return os.path.join(oot_bench.DATA, "autopilot.log")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(ap_path("config.json"), {}) or {})
    return cfg


def save_config(cfg):
    os.makedirs(ap_dir(), exist_ok=True)
    write_json(ap_path("config.json"), {k: cfg[k] for k in cfg if k in DEFAULT_CONFIG})


def json_bytes(data):
    return (json.dumps(data, indent=1, default=str) + "\n").encode("utf-8")


def split_file(path, part_bytes, out_dir):
    """[(name, path)]: the file itself if it fits, else <name>.001, .002, ... in out_dir."""
    name = os.path.basename(path)
    if os.path.getsize(path) <= part_bytes:
        return [(name, path)]
    parts = []
    with open(path, "rb") as src:
        index = 1
        while True:
            chunk = src.read(part_bytes)
            if not chunk:
                break
            part = os.path.join(out_dir, f"{name}.{index:03d}")
            with open(part, "wb") as dst:
                dst.write(chunk)
            parts.append((os.path.basename(part), part))
            index += 1
    return parts


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def under(path, roots):
    full = os.path.normcase(os.path.abspath(path))
    for root in roots:
        r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
        if full == r or full.startswith(r + os.sep):
            return True
    return False


def check_plan_paths(plan, roots):
    """A queued plan may name launchers in the build folder only, and an env value that is a
    path must lie in the bench's data folder or the build folder."""
    problems = []
    for name, arm in plan.get("arms", {}).items():
        base = arm.get("base")
        if base is not None and (not re.fullmatch(r"[A-Za-z0-9 _.()+-]+\.bat", str(base)) or ".." in str(base)):
            problems.append(f"arm {name}: base {base!r} must be a launcher's file name in the build folder")
        for var, value in arm.get("env", {}).items():
            value = str(value)
            if not re.search(r"[\\/]|^[A-Za-z]:", value):
                continue
            parts = re.split(r"[\\/]", value)
            if not os.path.isabs(value) or ".." in parts or not under(value, roots):
                problems.append(f"arm {name}: {var}={value!r} is a path outside the bench's data and build folders")
    if problems:
        raise benchplan.PlanError("the plan is refused:\n  " + "\n  ".join(problems))


def copy_bench(dest_parent):
    """This bench (bench.bat and the bench folder, without caches, Python or tests) into a build
    folder or the app folder. Existing files are left alone."""
    src_bat = os.path.join(HERE, "bench.bat")
    if not os.path.exists(src_bat):
        src_bat = os.path.join(os.path.dirname(HERE), "bench.bat")
    dest = os.path.join(dest_parent, "bench")
    if not os.path.exists(dest):
        shutil.copytree(HERE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "python", "tests", "queue"))
    bat = os.path.join(dest_parent, "bench.bat")
    if os.path.exists(src_bat) and not os.path.exists(bat):
        shutil.copy2(src_bat, bat)


def save_digest(local):
    """A digest of Ocarina's save folders (names, sizes and contents), or None."""
    root = os.path.join(local.get("data_root", ""), "nand", "user", "save")
    h = hashlib.sha256()
    found = False
    for base, dirs, files in os.walk(root):
        dirs.sort()
        inside = benchplan.OOT in os.path.relpath(base, root).upper().split(os.sep)
        if not inside:
            continue
        found = True
        for name in sorted(files):
            path = os.path.join(base, name)
            h.update(os.path.relpath(path, root).encode("utf-8", "replace"))
            try:
                h.update(file_sha256(path).encode())
            except OSError:
                h.update(b"?")
    return h.hexdigest() if found else None


class Owner:
    """The owner's input: keyboard and mouse from the host's idle time, controllers polled. A
    controller is only seen while it is polled, so after a gap in the polling (a long upload or
    zip) its idle time counts from the end of the gap."""

    GAP_S = 120.0

    def __init__(self, host):
        self.host = host
        self.last_pad = None
        self.last_poll = None
        self.gap_end = None

    def poll(self):
        now = self.host.now()
        if self.last_poll is not None and now - self.last_poll > self.GAP_S:
            self.gap_end = now
        self.last_poll = now
        try:
            if self.host.controller_active():
                self.last_pad = now
        except OSError:
            pass

    def idle_s(self):
        self.poll()
        kb = self.host.input_idle_s()
        kb = float("inf") if kb is None else kb
        seen = [t for t in (self.last_pad, self.gap_end) if t is not None]
        pad = self.host.now() - max(seen) if seen else float("inf")
        return min(kb, pad)

    def input_since(self, t):
        self.poll()
        kb = self.host.input_idle_s()
        if kb is not None and self.host.now() - kb > t + 0.5:
            return "keyboard or mouse"
        if self.last_pad is not None and self.last_pad > t:
            return "controller"
        return None


class Autopilot:
    def __init__(self, host, gh, config, local, say=print, runner_kwargs=None, refs=None):
        self.host = host
        self.gh = gh
        self.cfg = config
        self.local = local
        self.say_out = say
        self.runner_kwargs = runner_kwargs or {}
        self.refs = refs
        self.owner = Owner(host)
        self.clock = oot_bench.Clock(host)
        gh.sleep = self.wait
        gh.say = self.log
        gh.poll = self.gh_poll
        os.makedirs(ap_path("outbox"), exist_ok=True)
        self.state = read_json(ap_path("state.json")) or read_json(ap_path("state.json.bak"), {}) or {}
        self.state.setdefault("builds", {})
        self.state.setdefault("plans", {})
        self.state.setdefault("install_failures", {})
        self.state.setdefault("counters", {"owner_interruptions": 0, "sittings": 0, "prepares": 0})
        self.recent = collections.deque(maxlen=40)
        self.phase = "starting"
        self.remote = {}
        self.dirty = set()
        self.heartbeat_due = True
        self.last_heartbeat = None
        self.next_poll = 0.0
        self.net_wait_until = 0.0
        self.net_failures = 0
        self.synced_once = False
        self.armed_at = None
        self.guard_calls = 0
        self.ready_cache = (-1e18, None)
        self.awake = None
        self.session_dir = None
        self.stop_request = None
        self.requeue = None
        self.interrupted = []
        self.install_problems = {}

    # -- bookkeeping --------------------------------------------------------------------------
    def log(self, text):
        line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
        self.recent.append(line)
        try:
            self.say_out(line)
        except Exception:  # noqa: BLE001 - a closed console must not stop the loop
            pass
        try:
            path = log_path()
            if os.path.exists(path) and os.path.getsize(path) > 5 * MIB:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    def save(self):
        """state.json, flushed to disk, with the previous copy kept: a power cut must not lose
        which plans already ran."""
        path = ap_path("state.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(self.state, stream, indent=1, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.exists(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)

    def set_phase(self, text):
        """The current phase (in the heartbeat); logged when it changes other than in the idle
        countdown, so a long wait doesn't fill the log."""
        def key(t):
            return re.sub(r"idle \d+ of", "idle of", t)
        if key(text) != key(self.phase):
            self.log(text)
        self.phase = text

    def control(self):
        return read_json(ap_path("control.json"), {}) or {}

    def control_stop(self):
        c = self.control()
        if c.get("abort"):
            return "abort"
        if c.get("stop"):
            return "stop"
        return None

    def set_plan(self, pid, status, reason=None, **fields):
        p = self.state["plans"][pid]
        changed = p.get("status") != status or p.get("reason") != reason
        p.update(status=status, reason=reason, **fields)
        if status in FINAL:
            p.setdefault("finished", now_iso())
        if changed:
            self.log(f"plan {pid}: {status}" + (f" ({reason})" if reason else ""))
            self.dirty.add(pid)
            self.heartbeat_due = True
        self.save()

    def build_local(self, build):
        return dict(self.local, build_dir=build["dir"], build=build["sha"][:10])

    def keep_awake(self, on):
        on = bool(on) and self.host.ac_power() is not False
        if on != self.awake:
            self.host.keep_awake(on)
            self.awake = on

    # -- waiting ------------------------------------------------------------------------------
    def gh_poll(self):
        """While a gh call runs: the controller, and F3 held (raises Aborted)."""
        self.owner.poll()
        self.clock.check()

    def wait(self, seconds):
        """Sleeps, tracking the controller; F3 held raises Aborted."""
        end = self.host.now() + seconds
        while True:
            self.owner.poll()
            left = end - self.host.now()
            if left <= 0:
                return
            self.clock.sleep(min(0.5, left))

    def not_ready(self):
        """Why a launch can't start now, or None."""
        if self.control().get("pause"):
            return "paused by control.json"
        if self.remote.get("pause"):
            return "paused by the queue's control.json"
        idle = self.owner.idle_s()
        if idle < self.cfg["idle_s"]:
            return f"you're using the PC (idle {idle / 60:.0f} of {self.cfg['idle_s'] / 60:.0f} min)"
        now = self.host.now()
        if now - self.ready_cache[0] < 10:
            return self.ready_cache[1]
        reason = self.host.user_busy()
        if reason is None and (self.host.running(self.local.get("exe", "lichii.exe")) or
                               self.host.port_open(oot_bench.ADDRESS)):
            reason = "Lichii is running (the bench never closes a Lichii it didn't start)"
        if reason is None and self.host.ac_power() is False:
            reason = "the laptop is on battery"
        if reason is None and self.host.reboot_pending():
            reason = "Windows has a restart pending"
        if reason is None:
            try:
                if self.host.disk_free(oot_bench.DATA) < 2 * GIB:
                    reason = f"less than 2 GB free for {oot_bench.DATA}"
            except OSError:
                pass
        self.ready_cache = (now, reason)
        return reason

    def wait_ready(self, context, sitting=False):
        start = self.host.now()
        waited = False
        while True:
            stop = self.control_stop()
            if stop:
                if sitting:
                    self.stop_request = f"control.json {stop}"
                    self.requeue = "the autopilot was stopped by its control file"
                    raise Aborted(f"autopilot control.json {stop}")
                raise StopAutopilot(f"control.json {stop}")
            reason = self.not_ready()
            if reason is None:
                if waited:
                    self.log(f"{context}: starting")
                self.ready_cache = (-1e18, None)
                return
            if sitting and self.host.now() - start > self.cfg["max_pause_h"] * 3600:
                self.requeue = f"waited more than {self.cfg['max_pause_h']} h between two launches"
                raise Fatal(self.requeue)
            waited = True
            self.set_phase(f"{context}: waiting ({reason})")
            self.tick(poll=not sitting)
            self.wait(1.0)

    def arm(self):
        self.armed_at = self.host.now()
        self.ready_cache = (-1e18, None)

    def disarm(self):
        self.armed_at = None

    def guard(self):
        """Called on every poll of a running launch (about twice a second)."""
        if self.armed_at is None:
            return
        source = self.owner.input_since(self.armed_at)
        if source:
            raise OwnerActive(f"owner input ({source})")
        self.guard_calls += 1
        if self.guard_calls % 10 == 0 and self.control_stop() == "abort":
            self.stop_request = "control.json abort"
            self.requeue = "the autopilot was stopped by its control file"
            raise Aborted("autopilot control.json abort")

    # -- the loop -------------------------------------------------------------------------------
    def run(self, once=False):
        self.recover()
        try:
            self.host.lower_priority()
        except OSError:
            pass
        self.state["started"] = now_iso()
        self.save()
        self.log(f"{VERSION} started for {self.cfg['repo']} ({self.cfg['branch']}); hold F3 to stop")
        result = "finished"
        errors = collections.deque()
        try:
            while True:
                try:
                    if self.step(once):
                        break
                except (StopAutopilot, Aborted, KeyboardInterrupt):
                    raise
                except Exception as error:  # noqa: BLE001 - one bad step must not end an unattended loop
                    self.log(f"unexpected error: {error!r}\n{traceback.format_exc()}")
                    now = self.host.now()
                    errors.append(now)
                    while errors and now - errors[0] > 3600:
                        errors.popleft()
                    if len(errors) >= 5:
                        raise StopAutopilot("5 unexpected errors within an hour (see autopilot.log)")
                    self.disarm()
                    self.session_dir = None
                    self.recover_plans()
                    self.wait(60)
        except StopAutopilot as error:
            result = f"stopped: {error}"
        except Aborted as error:
            result = f"stopped: {error}"
        except KeyboardInterrupt:
            result = "stopped: Ctrl+C"
        finally:
            self.disarm()
            self.gh.poll = None  # F3 may still be held: the last heartbeat goes out regardless
            self.host.keep_awake(False)
            self.set_phase(result)
            self.save()
            try:
                self.publish_outbox()
                self.heartbeat(final=result)
            except GhError as error:
                self.log(f"the last heartbeat wasn't sent: {error}")
        return 0

    def step(self, once):
        """One pass of the loop; True to leave it."""
        stop = self.control_stop()
        if stop:
            raise StopAutopilot(f"control.json {stop}")
        self.tick(poll=True)
        work = self.next_work()
        self.keep_awake(work is not None)
        if work is not None:
            reason = self.not_ready()
            if reason is None:
                self.do(work)
                self.awake = None
                return False
            self.set_phase(f"{work[2]}: waiting ({reason})")
        elif once and self.synced_once:
            return True
        else:
            self.set_phase("idle: nothing to do")
        self.wait(1.0)
        return False

    def tick(self, poll=True):
        now = self.host.now()
        if now < self.net_wait_until:
            if poll:
                self.synced_once = True
            return
        try:
            if poll and now >= self.next_poll:
                try:
                    self.sync()
                finally:
                    self.synced_once = True
                self.next_poll = now + self.cfg["poll_s"]
            self.publish_outbox()
            if self.heartbeat_due or self.last_heartbeat is None or now - self.last_heartbeat >= self.cfg["heartbeat_s"]:
                self.heartbeat()
            if self.net_failures:
                self.log("GitHub answers again")
            self.net_failures = 0
            self.state.pop("network_error", None)
        except GhError as error:
            self.net_failures += 1
            delay = min(self.cfg["poll_s"] * 2 ** (self.net_failures - 1), 3600)
            self.net_wait_until = self.host.now() + delay
            self.state["network_error"] = str(error)[:300]
            self.log(f"GitHub: {str(error)[:200]} (next try in {delay / 60:.0f} min)")

    def do(self, work):
        kind, key, _ = work
        if kind == "prepare":
            self.prepare_build(key)
        else:
            self.run_plan(key)

    def next_work(self):
        builds = self.state["builds"]
        for key in sorted(builds, key=int, reverse=True):
            if builds[key].setdefault("prepare", {}).get("status") is None:
                return ("prepare", key, f"prepare r{key}")
        plans = self.state["plans"]
        work = None
        for pid in sorted(plans, key=lambda k: plans[k].get("order", k)):
            p = plans[pid]
            if p["status"] not in ("queued", "waiting"):
                continue
            build, why = self.resolve_build(p["plan"].get("build"))
            if build is None:
                self.set_plan(pid, "waiting", why)
                continue
            prep = build["prepare"]
            if prep.get("status") == "failed":
                self.set_plan(pid, "stopped", f"r{build['r']}'s preparation failed: {prep.get('reason')}")
                continue
            if prep.get("status") != "ok":
                self.set_plan(pid, "waiting", f"r{build['r']} isn't prepared yet")
                continue
            if work is None:
                work = ("plan", pid, f"run {pid} on r{build['r']}")
            elif p["status"] == "waiting":
                self.set_plan(pid, "queued", None)
        return work

    # -- GitHub: builds and the queue -------------------------------------------------------------
    def sync(self):
        self.sync_queue()
        self.sync_builds()
        self.state["last_sync"] = now_iso()
        self.save()

    def sync_queue(self):
        cfg = self.cfg
        entries = self.gh.list_dir(cfg["queue"], cfg["branch"])
        plans = self.state["plans"]
        names = set()
        remote = {}
        for entry in sorted(entries, key=lambda e: e.get("name", "")):
            name = entry.get("name", "")
            if entry.get("type") != "file" or not name.endswith(".json"):
                continue
            if name == "control.json":
                try:
                    remote = json.loads(self.gh.read_file(entry["path"], cfg["branch"])) or {}
                except ValueError:
                    remote = {}
                continue
            pid = name[:-5]
            if not PLAN_ID.fullmatch(pid):
                continue
            names.add(pid)
            p = plans.get(pid)
            if p and (p["status"] in FINAL or p["status"] == "running" or p.get("blob") == entry.get("sha")):
                continue
            text = self.gh.read_file(entry["path"], cfg["branch"])
            plans[pid] = {"id": pid, "file": entry["path"], "blob": entry.get("sha"), "order": name,
                          "status": "queued", "reason": None, "starts": (p or {}).get("starts", 0),
                          "queued_at": now_iso()}
            try:
                plan = json.loads(text)
                if not isinstance(plan, dict) or not plan.get("build"):
                    raise ValueError("a plan is a JSON object that names its build")
                plans[pid]["plan"] = plan
                self.log(f"plan {pid} queued for build {plan['build']}")
                self.dirty.add(pid)
                self.heartbeat_due = True
            except ValueError as error:
                plans[pid]["plan"] = {}
                self.set_plan(pid, "refused", f"not a plan: {error}")
        if remote.get("pause") != self.remote.get("pause"):
            self.log(f"queue control.json: pause {bool(remote.get('pause'))}")
        self.remote = remote
        for pid, p in plans.items():
            if p["status"] in ("queued", "waiting") and pid not in names:
                self.set_plan(pid, "withdrawn", "its file left the queue")
        self.save()

    def resolve_build(self, spec):
        """(build record, None) or (None, why)."""
        spec = str(spec or "").strip()
        builds = self.state["builds"]
        if spec.lower() == "latest":
            if not builds:
                return None, "no build is installed yet"
            return builds[max(builds, key=int)], None
        m = re.fullmatch(r"[rR](\d+)", spec)
        if m:
            b = builds.get(str(int(m[1])))
            return (b, None) if b else (None, f"build r{int(m[1])} isn't installed yet")
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", spec):
            for key in sorted(builds, key=int, reverse=True):
                if builds[key]["sha"].lower().startswith(spec.lower()):
                    return builds[key], None
            return None, f"build {spec} isn't installed yet"
        return None, f"build {spec!r}: give r<N>, a commit or latest"

    def package_name(self, r):
        return f"lichii-tuned-r{r}-{self.cfg['variant']}"

    def package_artifact(self, run, r):
        for art in self.gh.artifacts(run["id"]):
            if art.get("name") == self.package_name(r) and not art.get("expired"):
                return art
        return None

    def sync_builds(self):
        cfg = self.cfg
        runs = self.gh.successful_runs(cfg["workflow"], cfg["branch"])
        builds = self.state["builds"]
        off = cfg["build_offset"]
        if cfg["install_newest"]:
            for run in runs[:5]:
                r = run["run_number"] + off
                if str(r) in builds:
                    break
                art = None if self.folder_ready(r) else self.package_artifact(run, r)
                if art or self.folder_ready(r):
                    self.install(run, r, art)
                    break
        # Builds that queued plans name.
        for pid in sorted(self.state["plans"]):
            p = self.state["plans"][pid]
            if p["status"] not in ("queued", "waiting"):
                continue
            build, _ = self.resolve_build(p["plan"].get("build"))
            if build is not None:
                continue
            spec = str(p["plan"].get("build", "")).strip()
            m = re.fullmatch(r"[rR](\d+)", spec)
            if m:
                run = next((x for x in runs if x["run_number"] + off == int(m[1])), None)
            elif re.fullmatch(r"[0-9a-fA-F]{7,40}", spec):
                run = next((x for x in runs if x["head_sha"].lower().startswith(spec.lower())), None)
            else:
                continue
            if run is None:
                continue  # its run hasn't succeeded yet (or is older than the last 30)
            r = run["run_number"] + off
            if str(r) in builds:
                continue
            art = None if self.folder_ready(r) else self.package_artifact(run, r)
            if art is None and not self.folder_ready(r):
                self.set_plan(pid, "refused", f"r{r} isn't installed and its {self.package_name(r)} artifact has expired")
                continue
            self.install(run, r, art)

    def build_dir(self, r):
        return os.path.join(self.cfg["builds_dir"], self.cfg["folder"].format(r=r))

    def folder_ready(self, r):
        return os.path.isfile(os.path.join(self.build_dir(r), "lichii.exe"))

    def install(self, run, r, art):
        """The run's package into <builds>\\r<N>: downloaded into a staging folder beside it and
        renamed, so a folder is never half written or overwritten. A folder that already holds
        lichii.exe is taken as that build."""
        fails = self.state["install_failures"]
        if fails.get(str(r), 0) >= 3:
            return
        dest = self.build_dir(r)
        record = {"r": r, "sha": run["head_sha"], "run_id": run["id"], "run_number": run["run_number"],
                  "dir": dest, "artifact": self.package_name(r), "installed_at": now_iso(), "prepare": {}}
        if os.path.exists(dest):
            if not self.folder_ready(r):
                self.install_problems[str(r)] = f"{os.path.basename(dest)} exists without lichii.exe; not installed"
                self.set_phase(f"r{r}: {self.install_problems[str(r)]}")
                fails[str(r)] = 3
                self.save()
                return
            record["source"] = "existing folder"
            copy_bench(dest)
            self.finish_install(record)
            return
        need = int(art.get("size_in_bytes", 0)) * 3 + self.cfg["min_free_gb"] * GIB
        os.makedirs(self.cfg["builds_dir"], exist_ok=True)
        try:
            free = self.host.disk_free(self.cfg["builds_dir"])
        except OSError:
            free = None
        if free is not None and free < need:
            self.install_problems[str(r)] = (f"{free / GIB:.0f} GB free in the builds folder, {need / GIB:.0f} GB "
                                             "needed; delete old builds yourself to make room")
            self.set_phase(f"r{r}: {self.install_problems[str(r)]}")
            return
        staging = os.path.join(self.cfg["builds_dir"], f".bench-download-r{r}-{oot_bench.stamp()}")
        os.makedirs(staging)
        with open(os.path.join(staging, STAGING_MARK), "w") as stream:
            stream.write(f"r{r} run {run['id']}\n")
        self.set_phase(f"downloading r{r} ({int(art.get('size_in_bytes', 0)) / MIB:.0f} MB)")
        try:
            self.gh.download_artifact(run["id"], self.package_name(r), staging)
            if not os.path.isfile(os.path.join(staging, "lichii.exe")):
                raise GhError(f"{self.package_name(r)} has no lichii.exe")
            if os.path.exists(dest):
                raise GhError(f"{os.path.basename(dest)} appeared during the download; left as it is")
            for attempt in range(6):
                try:
                    os.rename(staging, dest)
                    break
                except OSError:
                    # A virus scanner can hold the new files for a moment.
                    if attempt == 5 or os.path.exists(dest):
                        raise
                    self.wait(5)
            try:
                os.remove(os.path.join(dest, STAGING_MARK))
            except OSError:
                pass  # harmless: only folders named .bench-download-* are ever removed
        except (GhError, OSError) as error:
            remove_staging(staging)
            fails[str(r)] = fails.get(str(r), 0) + 1
            self.save()
            self.install_problems[str(r)] = f"the download failed ({str(error)[:200]})"
            self.log(f"r{r}: {self.install_problems[str(r)]}")
            if isinstance(error, GhError) and error.transient:
                raise
            return
        record["source"] = "downloaded"
        copy_bench(dest)
        write_json(os.path.join(dest, "bench-build.json"),
                   {k: record[k] for k in ("r", "sha", "run_id", "run_number", "artifact", "installed_at")})
        self.finish_install(record)

    def finish_install(self, record):
        self.state["builds"][str(record["r"])] = record
        self.state["install_failures"].pop(str(record["r"]), None)
        self.install_problems.pop(str(record["r"]), None)
        self.heartbeat_due = True
        self.save()
        self.log(f"r{record['r']} installed ({record['source']}, commit {record['sha'][:10]})")

    # -- preparing a build ----------------------------------------------------------------------
    def prepare_build(self, key):
        build = self.state["builds"][key]
        prep = build.setdefault("prepare", {})
        self.wait_ready(f"prepare r{key}")
        self.set_phase(f"preparing r{key} (the first launch's shader preparation, up to 75 min)")
        prep["running"] = True
        self.save()
        local = self.build_local(build)
        try:
            oot_bench.preflight(self.host, local)
        except Fatal as error:
            # Not an attempt: the machine isn't ready (Lichii open, a restart pending, ...).
            prep.pop("running", None)
            self.save()
            self.log(f"prepare r{key} waits: {error}")
            self.ready_cache = (self.host.now(), str(error))
            return
        before = save_digest(self.local)
        self.arm()
        record = None
        try:
            record = oot_bench.cmd_prepare(self.host, say=self.log, local=local, guard=self.guard,
                                           runner_kwargs=dict(self.runner_kwargs, refs=self.refs))
        except OwnerActive as error:
            self.state["counters"]["owner_interruptions"] += 1
            prep["interrupted"] = prep.get("interrupted", 0) + 1
            self.log(f"{error} during the preparation of r{key}: Lichii closed; it starts again once you've been "
                     f"idle {self.cfg['idle_s'] / 60:.0f} min")
            return
        except Aborted as error:
            raise StopAutopilot(str(error))
        except (Fatal, benchplan.PlanError) as error:
            record = dict(getattr(error, "record", None) or {}, status="fatal", reason=str(error))
        finally:
            self.disarm()
            prep.pop("running", None)
            self.check_saves(before, f"the preparation of r{key}")
            self.save()
        self.state["counters"]["prepares"] += 1
        prep["attempts"] = prep.get("attempts", 0) + 1
        prep["last"] = {"status": record.get("status"), "reason": record.get("reason"),
                        "session": os.path.basename(record.get("session", "")) or None, "at": now_iso()}
        if record.get("status") == "ok":
            prep.update(status="ok", reason=None)
        elif prep["attempts"] >= self.cfg["max_prepare_attempts"]:
            prep.update(status="failed", reason=record.get("reason"))
        self.heartbeat_due = True
        self.save()
        self.log(f"r{key} prepare: {record.get('status')}" + (f" ({record.get('reason')})" if record.get("reason") else ""))
        if record.get("session"):
            self.publish_session(record["session"], "prepare", None, record.get("status"),
                                 {"build": {"r": build["r"], "sha": build["sha"]}, "reason": record.get("reason")})

    # -- running a plan ---------------------------------------------------------------------------
    def run_plan(self, pid):
        p = self.state["plans"][pid]
        build, why = self.resolve_build(p["plan"].get("build"))
        if build is None:
            self.set_plan(pid, "waiting", why)
            return
        plan = dict(p["plan"])
        plan["build"] = build["sha"][:10]
        plan["autopilot"] = {"plan_id": pid, "build_r": build["r"], "build_sha": build["sha"],
                             "start": p.get("starts", 0) + 1}
        try:
            check_plan_paths(plan, [oot_bench.DATA, build["dir"]])
            plan = benchplan.load_plan(plan, build["dir"])
        except (benchplan.PlanError, OSError) as error:
            self.set_plan(pid, "refused", str(error))
            return
        local = self.build_local(build)
        self.wait_ready(f"run {pid} on r{build['r']}")
        if self.state["plans"].get(pid) is not p or p["status"] not in ("queued", "waiting"):
            return  # edited or withdrawn while it waited: the next pass takes the new state
        session_dir = oot_bench.new_session(plan)
        self.session_dir = session_dir
        p["starts"] = p.get("starts", 0) + 1
        self.set_plan(pid, "running", None, session=os.path.basename(session_dir), build_r=build["r"],
                      started=now_iso())
        self.set_phase(f"running {pid} on r{build['r']} ({len(benchplan.schedule(plan))} launches, about "
                       f"{benchplan.estimate_minutes(plan):.0f} min)")
        oot_bench.copy_configs(self.local, session_dir)
        runner = oot_bench.Runner(self.host, local, plan, session_dir, say=self.log, guard=self.guard, refs=self.refs,
                                  **self.runner_kwargs)
        self.stop_request = self.requeue = None
        self.interrupted = []
        self.state["counters"]["sittings"] += 1
        self.host.keep_awake(True)
        try:
            try:
                results, notes, stopped = oot_bench.run_schedule(plan, self.slot_runner(runner, plan), say=runner.log)
            except KeyboardInterrupt:
                results, notes, stopped = [], [], "aborted: Ctrl+C"
            notes = list(notes) + [f"launch {label} was stopped by the owner's input and its slot run again"
                                   for label in self.interrupted]
            zip_path = oot_bench.end_session(self.host, local, plan, session_dir, results, notes, stopped, runner,
                                             self.log, quiet=True)
        finally:
            self.session_dir = None
        stop_autopilot = None
        if self.requeue:
            again = p["starts"] < self.cfg["max_plan_starts"]
            status, reason = ("queued" if again else "gave-up"), self.requeue
            if self.stop_request:
                stop_autopilot = self.stop_request
        elif stopped is None:
            status, reason = "done", None
        elif stopped.startswith("aborted"):
            status, reason = "aborted", stopped
            stop_autopilot = stopped
        else:
            status, reason = "stopped", stopped
        self.set_plan(pid, status, reason, result=f"results/{os.path.basename(session_dir)}/")
        self.publish_session(session_dir, "sitting", pid, stopped or "finished",
                             {"build": {"r": build["r"], "sha": build["sha"]}, "plan_status": status,
                              "interrupted_launches": self.interrupted, "zip_local": os.path.basename(zip_path)})
        if stop_autopilot:
            raise StopAutopilot(stop_autopilot)

    def slot_runner(self, runner, plan):
        total = len(benchplan.schedule(plan))

        def run_one(slot, attempt):
            interrupts = 0
            self.tick(poll=False)
            while True:
                self.wait_ready(f"slot {slot['slot']} of {total}", sitting=True)
                before = save_digest(self.local)
                self.arm()
                record = None
                checked = False
                try:
                    record = runner.run_one(slot, attempt)
                    return record
                except OwnerActive as error:
                    self.state["counters"]["owner_interruptions"] += 1
                    record = getattr(error, "record", None)
                    if record and record.get("status") == "ok":
                        self.log(f"{error} during the cooldown after {record['label']}: the run is kept")
                        return record
                    self.disarm()
                    self.check_saves(before, f"launch {(record or {}).get('label')}", record)
                    checked = True
                    if record:
                        record.update(status="interrupted", reason=str(error))
                        write_json(os.path.join(runner.session_dir, "runs", record["label"], "run.json"), record)
                        self.interrupted.append(record["label"])
                    interrupts += 1
                    self.log(f"{error}: Lichii closed; slot {slot['slot']} runs again once you've been idle "
                             f"{self.cfg['idle_s'] / 60:.0f} min")
                    if interrupts >= self.cfg["max_interrupts_per_slot"]:
                        raise Fatal(f"input stopped slot {slot['slot']} {interrupts} times (is a program sending "
                                    "input, or a controller drifting?)")
                except (Fatal, Aborted) as error:
                    record = getattr(error, "record", None)
                    raise
                finally:
                    self.disarm()
                    if not checked:
                        # A record that goes back to the schedule is written again by finish().
                        self.check_saves(before, f"launch {(record or {}).get('label', slot['slot'])}", record)
        return run_one

    def check_saves(self, before, what, record=None):
        after = save_digest(self.local)
        if before == after:
            return
        self.log(f"WARNING: Ocarina's save folder changed during {what}; a copy is kept in save-backups")
        try:
            oot_bench.backup_saves(self.local, self.log)
        except OSError as error:
            self.log(f"the save backup failed: {error}")
        self.state.setdefault("save_changes", []).append({"during": what, "at": now_iso()})
        self.heartbeat_due = True
        if record is not None:
            record.setdefault("flags", []).append("Ocarina's save folder changed during this launch")

    # -- results and the heartbeat ----------------------------------------------------------------
    def outbox_new(self, title):
        root = ap_path("outbox")
        os.makedirs(root, exist_ok=True)
        seq = 1 + max([int(n.split("-", 1)[0]) for n in os.listdir(root) if n.split("-", 1)[0].isdigit()] or [0])
        path = os.path.join(root, f"{seq:06d}-{oot_bench.slug(title)}")
        os.makedirs(path)
        return path

    def outbox_commit(self, item, files, message):
        """files: {repo path: local path or bytes}; bytes are written into the item first. The
        manifest comes last, so an item without one was never complete."""
        manifest = {}
        for n, (repo_path, data) in enumerate(files.items()):
            if isinstance(data, bytes):
                local = os.path.join(item, f"file-{n:03d}")
                with open(local, "wb") as stream:
                    stream.write(data)
                data = local
            manifest[repo_path] = data
        write_json(os.path.join(item, "manifest.json"), {"message": message, "files": manifest})

    def publish_outbox(self):
        root = ap_path("outbox")
        for name in sorted(os.listdir(root)):
            item = os.path.join(root, name)
            manifest = read_json(os.path.join(item, "manifest.json"))
            if not manifest:
                continue
            files = {p: path for p, path in manifest["files"].items() if os.path.exists(path)}
            try:
                self.gh.commit_files(self.cfg["results_branch"], files, manifest["message"], readme=RESULTS_README)
            except GhError as error:
                if error.transient:
                    raise
                self.log(f"results {name} not accepted ({str(error)[:200]}); kept in outbox-failed")
                # The plan statuses it carried go out with the next heartbeat instead.
                for repo_path in manifest["files"]:
                    m = re.fullmatch(r"status/plans/(.+)\.json", repo_path)
                    if m and m[1] in self.state["plans"]:
                        self.dirty.add(m[1])
                os.makedirs(ap_path("outbox-failed"), exist_ok=True)
                shutil.move(item, ap_path("outbox-failed", name))
                continue
            shutil.rmtree(item, ignore_errors=True)
            self.log(f"results sent: {manifest['message']}")

    def publish_session(self, session_dir, kind, pid, status, extra):
        """The session's zip (split into parts if large), summary, runs.json and result.json,
        and the plan's status, as one commit through the outbox."""
        name = os.path.basename(session_dir)
        results_dir = os.path.join(oot_bench.DATA, "results")
        zip_path = os.path.join(results_dir, name + ".zip")
        if not os.path.exists(zip_path):
            zip_path = oot_bench.make_zip(session_dir, results_dir)
        limit = self.cfg["max_zip_mb"] * MIB
        left_out = None
        if os.path.getsize(zip_path) > limit:
            zip_path = oot_bench.make_zip(session_dir, results_dir, exclude=r"\.dmp$", name=name + "-slim")
            left_out = "dumps"
        if os.path.getsize(zip_path) > limit:
            zip_path = oot_bench.make_zip(session_dir, results_dir, exclude=r"(\.dmp|eden_log\.txt)$",
                                          name=name + "-nologs")
            left_out = "dumps and logs (kept on the PC)"
        item = self.outbox_new(f"{kind}-{name}")
        parts = split_file(zip_path, max(1, int(self.cfg["part_mb"] * MIB)), item)
        base = f"results/{name}/"
        files = {base + part: path for part, path in parts}
        for extra_name in ("summary.txt", "runs.json"):
            path = os.path.join(session_dir, extra_name)
            if os.path.exists(path):
                files[base + extra_name] = path
        result = dict(extra, kind=kind, plan=pid, session=name, status=status, finished=now_iso(),
                      zip=os.path.basename(zip_path), parts=[part for part, _ in parts],
                      zip_bytes=os.path.getsize(zip_path), zip_sha256=file_sha256(zip_path), left_out=left_out,
                      autopilot=VERSION)
        files[base + "result.json"] = json_bytes(result)
        if pid:
            files[f"status/plans/{pid}.json"] = json_bytes(self.plan_status(pid))
            self.dirty.discard(pid)
        self.outbox_commit(item, files, f"bench: {kind} {name} ({status})")

    def plan_status(self, pid):
        p = self.state["plans"][pid]
        return {k: p.get(k) for k in ("id", "file", "status", "reason", "build_r", "session", "result", "starts",
                                       "queued_at", "started", "finished")}

    def heartbeat(self, final=None):
        builds = [{"r": b["r"], "commit": b["sha"][:10], "folder": os.path.basename(b["dir"]),
                   "source": b.get("source"), "prepare": b.get("prepare", {}).get("status"),
                   "prepare_last": b.get("prepare", {}).get("last")} for _, b in
                  sorted(self.state["builds"].items(), key=lambda kv: int(kv[0]))]
        idle = self.owner.idle_s()
        data = {"updated": now_iso(), "autopilot": VERSION, "state": final or "running", "phase": self.phase,
                "started": self.state.get("started"), "last_sync": self.state.get("last_sync"),
                "network_error": self.state.get("network_error"),
                "owner_idle_min": None if idle == float("inf") else round(idle / 60, 1),
                "counters": self.state["counters"], "save_changes": self.state.get("save_changes", [])[-10:],
                "builds": builds, "plans": {pid: {k: p.get(k) for k in ("status", "reason", "build_r", "session")}
                                            for pid, p in self.state["plans"].items()},
                "outbox": len([n for n in os.listdir(ap_path("outbox"))]),
                "install_problems": {f"r{r}": text for r, text in sorted(self.install_problems.items())},
                "remote_pause": bool(self.remote.get("pause")),
                "recent": [line[:400] for line in list(self.recent)[-30:]]}
        if self.session_dir:
            data["session"] = read_json(os.path.join(self.session_dir, "status.json"))
        files = {"status/autopilot.json": json_bytes(data)}
        for pid in sorted(self.dirty):
            files[f"status/plans/{pid}.json"] = json_bytes(self.plan_status(pid))
        self.gh.commit_files(self.cfg["results_branch"], files, f"autopilot: {self.phase[:70]}", readme=RESULTS_README)
        self.dirty.clear()
        self.heartbeat_due = False
        self.last_heartbeat = self.host.now()

    # -- restart ----------------------------------------------------------------------------------
    def recover(self):
        """After a reboot or crash: our staging folders and unfinished outbox items go, a sitting
        that was running is zipped as interrupted and its plan queued again."""
        builds_dir = self.cfg["builds_dir"]
        if builds_dir and os.path.isdir(builds_dir):
            for name in os.listdir(builds_dir):
                path = os.path.join(builds_dir, name)
                if name.startswith(".bench-download-") and os.path.isfile(os.path.join(path, STAGING_MARK)):
                    remove_staging(path)
        root = ap_path("outbox")
        for name in os.listdir(root):
            if not os.path.exists(os.path.join(root, name, "manifest.json")):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        for build in self.state["builds"].values():
            build.setdefault("prepare", {}).pop("running", None)
        self.state["install_failures"] = {}
        self.recover_plans()
        c = self.control()
        if c.get("stop") or c.get("abort"):
            write_json(ap_path("control.json"), {"stop": False, "abort": False, "pause": bool(c.get("pause"))})
        self.save()

    def recover_plans(self):
        for pid, p in self.state["plans"].items():
            if p.get("status") != "running":
                continue
            again = p.get("starts", 0) < self.cfg["max_plan_starts"]
            reason = "the autopilot stopped during the sitting (restart or crash)"
            session_dir = os.path.join(oot_bench.DATA, "sessions", p.get("session") or "")
            self.set_plan(pid, "queued" if again else "gave-up", reason)
            if p.get("session") and os.path.isdir(session_dir):
                self.publish_session(session_dir, "sitting", pid, "interrupted", {"build_r": p.get("build_r"),
                                                                                 "plan_status": p["status"]})
        self.save()


def remove_staging(path):
    """Removes one of our own download folders (it carries the marker file); nothing else."""
    if os.path.basename(path).startswith(".bench-download-") and os.path.isfile(os.path.join(path, STAGING_MARK)):
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# Commands


def ensure_config(ask=None, say=print, again=False, local=None):
    cfg = load_config()
    if cfg["repo"] and cfg["builds_dir"] and not again:
        return cfg
    if ask is None:
        raise Fatal("the autopilot isn't set up: run bench.bat install-autostart once")
    repo = ask(f"GitHub repository that builds Lichii (owner/name) [{cfg['repo'] or ''}]: ").strip() or cfg["repo"]
    if not repo or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Fatal(f"{repo!r} isn't owner/name")
    guess = cfg["builds_dir"] or (os.path.dirname(local["build_dir"].rstrip("\\/")) if local else "")
    builds = ask(f"Folder with one folder per build [{guess}]: ").strip().strip('"') or guess
    if not builds or not os.path.isdir(builds):
        raise Fatal(f"no folder {builds!r}")
    cfg.update(repo=repo, builds_dir=builds)
    save_config(cfg)
    say(f"Wrote {ap_path('config.json')}")
    return cfg


def startup_entry():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        STARTUP_NAME)


def install_app():
    """The bench the autopilot runs from: DATA\\app (bench.bat and bench\\), replaced each time."""
    app = os.path.join(oot_bench.DATA, "app")
    if under(HERE, [app]):
        return app
    shutil.rmtree(os.path.join(app, "bench"), ignore_errors=True)
    if os.path.exists(os.path.join(app, "bench.bat")):
        os.remove(os.path.join(app, "bench.bat"))
    os.makedirs(app, exist_ok=True)
    copy_bench(app)
    return app


def cmd_install_autostart(host, ask=input, say=print, gh_factory=None):
    local = oot_bench.load_local()
    cfg = ensure_config(ask, say, again=True, local=local)
    gh = (gh_factory or (lambda repo: GitHub(repo, ap_path("tmp"))))(cfg["repo"])
    try:
        gh.api(f"repos/{cfg['repo']}/actions/workflows/{cfg['workflow']}")
    except GhError as error:
        raise Fatal(f"GitHub CLI can't see {cfg['workflow']} in {cfg['repo']}: {error} (gh auth status?)")
    app = install_app()
    entry = startup_entry()
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    with open(entry, "w", encoding="utf-8", newline="\r\n") as stream:
        stream.write("@echo off\n"
                     "rem Lichii Bench autopilot (bench.bat install-autostart); delete this file to stop it starting at logon.\n"
                     f'cd /d "{app}"\n'
                     'start "Lichii Bench autopilot" /min cmd /c bench.bat autopilot\n')
    say(f"The autopilot starts at every logon ({entry}); its log is {log_path()}.")
    say("Starting it now in a minimized console. Hold F3 to stop it; bench.bat uninstall-autostart removes the entry.")
    host.start_file(entry)
    return entry


def main(args, host=None, say=print, ask=None):
    command = args.command
    if command == "autopilot-stop":
        os.makedirs(ap_dir(), exist_ok=True)
        c = read_json(ap_path("control.json"), {}) or {}
        c["stop"] = True
        write_json(ap_path("control.json"), c)
        say("The autopilot stops after the current launch (Lichii is closed as usual).")
        return 0
    if command == "uninstall-autostart":
        try:
            os.remove(startup_entry())
            say("Removed the Startup entry; a running autopilot keeps going until F3 or autopilot-stop.")
        except FileNotFoundError:
            say("There was no Startup entry.")
        return 0
    try:
        host = host or oot_bench.make_host()
        if command == "install-autostart":
            cmd_install_autostart(host, ask or input, say)
            return 0
        os.makedirs(ap_dir(), exist_ok=True)
        lock = open(ap_path("lock"), "a+")
        if not winhost.lock_exclusive(lock):
            say("The autopilot is already running (see its minimized console or autopilot.log).")
            lock.close()
            return 0
        try:
            local = oot_bench.load_local()
            if not oot_bench.load_refs():
                raise Fatal("no menu screen records: run bench.bat check once first")
            cfg = ensure_config(ask or (input if sys.stdin and sys.stdin.isatty() else None), say, local=local)
            if getattr(args, "idle_min", None) is not None:
                cfg["idle_s"] = max(0.0, args.idle_min * 60)
            gh = GitHub(cfg["repo"], ap_path("tmp"))
            return Autopilot(host, gh, cfg, local, say=say).run(once=args.once)
        finally:
            lock.close()
    except (Fatal, benchplan.PlanError) as error:
        say(f"LICHII BENCH AUTOPILOT STOPPED: {error}")
        return 2
