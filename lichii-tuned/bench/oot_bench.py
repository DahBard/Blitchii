#!/usr/bin/env python3
"""Lichii Bench: unattended fresh-launch A/B sittings at Ocarina's Market fountain.

    bench.bat setup                 one time: game file, hotkeys, save backup, local.json
    bench.bat check                 a windowed launch you watch (records the menu screens), then
                                    two fullscreen launches on their own
    bench.bat run <plan.json>       a sitting (or drag the plan onto bench.bat)
    bench.bat prepare               a new build's first launch (precache), unattended
    bench.bat restore-config <session>
    bench.bat zip <session>
    bench.bat autopilot [--once]    new builds from CI, their preparation and queued plans, unattended
                                    (autopilot.py; install-autostart starts it at logon)

Each run: launch lichii.exe with the arm's environment and the debug server, find this run's
log, check the boot lines, press A through the menus to Link's save, settle, measure, quit
(answering the "Are you sure" box), copy the log and check it. Hold F3 for a second (or press
Ctrl+C) to stop: the runner closes Lichii and keeps what it has. Standard library only.
"""

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import statistics as st
import sys
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import benchplan  # noqa: E402
import logscan  # noqa: E402
import navigate  # noqa: E402
import screens  # noqa: E402
import winhost  # noqa: E402
from client import Client, ClientError, NotListening, ServerError  # noqa: E402

ADDRESS = "127.0.0.1:6545"
DATA = os.environ.get("BENCH_DATA_DIR", r"C:\LichiiBench")
GAME_CONFIG = "0400486024DEC000.ini"

# Lichii's default controller hotkeys (uisettings.cpp), used where qt-config.ini has none.
DEFAULT_HOTKEYS = {
    "Audio Mute/Unmute": "Home+Dpad_Right", "Audio Volume Down": "Home+Dpad_Down",
    "Audio Volume Up": "Home+Dpad_Up", "Capture Screenshot": "Screenshot", "Change Adapting Filter": "Home+L",
    "Change Docked Mode": "Home+X", "Change GPU Mode": "Home+R", "Continue/Pause Emulation": "Home+Plus",
    "Exit Eglantine": "Home+Minus", "Fullscreen": "Home+B", "Restart Emulation": "R+Plus+Minus",
    "Stop Emulation": "L+Plus+Minus", "Toggle Framerate Limit": "Home+Y",
}

# Problems after which retrying the run can't help: the sitting stops.
FATAL_PREFIXES = ("build is", "no version line", "booted", "no settings dump", "setting ", "no TEST switches",
                  "TEST switches line", "no line matches", "power mode", "on battery", "GPU clock probe")

# A settle-time probe this far from the sitting's median is a wrong clock state (r66 sat at
# 1.8 GHz against r64's 3.1): the sitting stops. Smaller differences are the end's flags.
PROBE_FATAL_PCT = 10.0


class Fatal(Exception):
    """Stop the sitting: the setup or the machine state is wrong."""


class RunFailed(Exception):
    """This launch failed: retry it in the same slot."""


class Aborted(Exception):
    """F3, Ctrl+C or control.json."""


# ---------------------------------------------------------------------------------------------
# Small helpers


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "plan"


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def parse_qt_ini(text):
    """Lichii's INI format: [Section], key=value, nested keys with backslashes, %20 for spaces.
    Returns {"Section/a/b": value}."""
    out = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().replace("\\\\", "\\").replace("\\", "/").replace("%20", " ")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[f"{section}/{key}"] = value
    return out


def read_hotkeys(ini):
    hotkeys = dict(DEFAULT_HOTKEYS)
    for key, value in ini.items():
        m = re.match(r"^UI/Shortcuts/[^/]+/([^/]+)/Controller_KeySeq$", key)
        if m:
            hotkeys[m[1]] = value
    return hotkeys


class Clock:
    """Time for the runner; every sleep polls F3 (held over two polls) and control.json. A guard
    (the autopilot's owner-input check) is called on every poll while F3 isn't down; it raises
    to stop the run."""

    def __init__(self, host, control_path=None, guard=None):
        self.host = host
        self.control_path = control_path
        self.guard = guard
        self.f3_polls = 0
        self.checks = 0

    def now(self):
        return self.host.now()

    def check(self):
        self.checks += 1
        if self.host.f3_down():
            self.f3_polls += 1
            if self.f3_polls >= 2:
                raise Aborted("F3")
        else:
            self.f3_polls = 0
        if self.guard is not None and self.f3_polls == 0:
            self.guard()
        if self.control_path and self.checks % 10 == 0:
            control = read_json(self.control_path, {}) or {}
            if control.get("abort"):
                raise Aborted("control.json abort")

    def sleep(self, seconds):
        end = self.host.now() + seconds
        while True:
            self.check()
            left = end - self.host.now()
            if left <= 0:
                return
            self.host.sleep(min(0.5, left))


# ---------------------------------------------------------------------------------------------
# Quitting


def quit_lichii(host, process, game_title=None, timeout_s=60, resend_s=5):
    """WM_CLOSE to the main window, re-sent every few seconds (close_pending makes the shutdown
    run once), answering the confirm box when it shows; after timeout_s the process is killed."""
    record = {"close_sent": 0, "confirm_answered": 0, "killed": False}
    start = host.now()
    last_close = None
    while process.poll() is None:
        now = host.now()
        if now - start > timeout_s:
            process.kill()
            record["killed"] = True
            break
        wins = host.windows(process.pid)
        box = winhost.pick_confirm_dialog(wins)
        if box is not None:
            # Y clicks Yes (its &Yes mnemonic); Enter after three tries, in case it isn't English.
            if record["confirm_answered"] < 3:
                host.press_key(box, winhost.VK_Y, "y")
            else:
                host.press_key(box, winhost.VK_RETURN)
            record["confirm_answered"] += 1
        elif last_close is None or now - last_close >= resend_s:
            main = winhost.pick_main_window(wins, game_title)
            if main is not None:
                host.close_window(main)
                record["close_sent"] += 1
                last_close = now
        host.sleep(0.5)
    for _ in range(40):
        if process.poll() is not None:
            break
        host.sleep(0.25)
    record["exit_code"] = process.poll()
    record["quit_s"] = round(host.now() - start, 1)
    return record


# ---------------------------------------------------------------------------------------------
# The schedule with its retry rules


def run_schedule(plan, run_one, say=print):
    """Runs every slot. A failed run is retried once in its slot; if the retry fails too, the
    block is dropped and a new complete block appended (at most max_extra_blocks). Two failed
    slots in a row, a Fatal or an abort stop the sitting. Returns (results, notes, stopped)."""
    slots = benchplan.schedule(plan)
    blocks = benchplan.plan_blocks(plan)
    queue = list(slots)
    results = []
    notes = []
    failed_in_row = 0
    extra = 0
    dropped_blocks = set()
    stopped = None
    i = 0
    while i < len(queue):
        slot = queue[i]
        i += 1
        if slot["block"] is not None and slot["block"] in dropped_blocks:
            continue
        if plan.get("_stop_after_run"):
            stopped = "stopped after a run at the owner's request"
            break
        outcome = None
        for attempt in (1, 2):
            try:
                outcome = run_one(slot, attempt)
            except (Fatal, Aborted) as error:
                stopped = f"{'aborted' if isinstance(error, Aborted) else 'stopped'}: {error}"
                if getattr(error, "record", None):
                    results.append(error.record)
                break
            except KeyboardInterrupt:
                stopped = "aborted: Ctrl+C"
                break
            results.append(outcome)
            if outcome["status"] == "ok":
                break
            say(f"run {outcome.get('label')} failed: {outcome.get('reason')}" + (", retrying in its slot" if attempt == 1 else ""))
        if stopped:
            break
        if outcome["status"] == "ok":
            failed_in_row = 0
            continue
        failed_in_row += 1
        if failed_in_row >= 2:
            stopped = "stopped: two failed slots in a row"
            break
        if slot["block"] is not None:
            dropped_blocks.add(slot["block"])
            for r in results:
                if r.get("block") == slot["block"]:
                    r["dropped"] = True
            if extra < plan.get("max_extra_blocks", 2):
                extra += 1
                new_index = len(blocks)
                order = blocks[slot["block"]]
                blocks.append(list(order))
                for arm in order:
                    queue.append({"arm": arm, "block": new_index, "warmup": False, "slot": len(queue) + 1})
                notes.append(f"block {slot['block'] + 1} dropped (slot {slot['slot']} failed twice); "
                             f"block {new_index + 1} appended with the same order")
            else:
                notes.append(f"block {slot['block'] + 1} dropped; no more blocks may be added")
        else:
            notes.append(f"warm-up slot {slot['slot']} ({slot['arm']}) failed twice")
    return results, notes, stopped


# ---------------------------------------------------------------------------------------------
# One launch


class Runner:
    def __init__(self, host, local, plan, session_dir, client_factory=None, script=None, refs=None,
                 say=print, ask=None, guard=None, threaded=True):
        self.host = host
        self.local = local
        self.plan = plan
        self.session_dir = session_dir
        self.client_factory = client_factory or (lambda: Client(ADDRESS))
        self.script = script or read_json(os.path.join(HERE, "oot_boot.json"))
        self.refs = refs if refs is not None else load_refs()
        self.say = say
        self.ask = ask
        self.clock = Clock(host, os.path.join(session_dir, "control.json"), guard)
        self.launches = 0
        self.threaded = threaded
        self.probe_values = []
        self.hotkeys = navigate.hotkey_sets(local.get("hotkeys", DEFAULT_HOTKEYS))
        self.log_dir = local["log_dir"]

    def log(self, text):
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {text}"
        self.say(line)
        with open(os.path.join(self.session_dir, "runner.log"), "a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def status(self, **fields):
        path = os.path.join(self.session_dir, "status.json")
        data = read_json(path, {}) or {}
        data.update(fields, updated=datetime.datetime.now().isoformat(timespec="seconds"))
        write_json(path, data)

    def ensure_closed(self, wait_s=30):
        start = self.host.now()
        while self.host.running(self.local.get("exe", "lichii.exe")) or self.host.port_open(ADDRESS):
            if self.host.now() - start > wait_s:
                raise Fatal("lichii.exe is still running or port 6545 still answers: close Lichii first")
            self.host.sleep(1.0)

    def environment(self, arm_name, label):
        arm = self.plan["arms"][arm_name]
        extra, exe = benchplan.arm_env(arm, self.local["build_dir"], self.read_text)
        env = benchplan.scrubbed_environ(os.environ)
        env.update(extra)
        env.update({"EDEN_NX2_DEBUG_SERVER": ADDRESS,
                    "EDEN_NX2_DEBUG_DIR": os.path.join(self.session_dir, "runs", label, "debug"),
                    "LICHII_AB_ARM": arm_name, "LICHII_AB_RUN": f"{os.path.basename(self.session_dir)}/{label}"})
        return env, exe

    def read_text(self, path):
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()

    def call(self, method, params=None):
        return self.client.call(method, params or {})

    def call_polled(self, method, params):
        """A long read-only call (frame_times) on a thread, so F3 still works meanwhile."""
        if not self.threaded:
            return self.client.call(method, params)
        box = {}

        def work():
            try:
                box["result"] = self.client.call(method, params)
            except Exception as error:  # noqa: BLE001 - handed to the caller
                box["error"] = error
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                self.clock.sleep(0.2)
                thread.join(0.01)
        except Aborted:
            self.client.close()
            raise
        if "error" in box:
            raise box["error"]
        return box["result"]

    def shot(self, run_dir, name):
        try:
            result = self.call("screenshot", {"height": 144, "timeout_s": 5})
        except ClientError as error:
            return None, str(error)
        png = base64.b64decode(result["png_base64"])
        os.makedirs(os.path.join(run_dir, "shots"), exist_ok=True)
        with open(os.path.join(run_dir, "shots", name + ".png"), "wb") as stream:
            stream.write(png)
        return screens.thumb_hash(png), None

    def draws_source(self):
        def newest(n):
            self.tail.poll()
            return self.state.draws[-n:]
        return newest

    def gate(self, arm_name, record):
        """The machine state while settling: the probe band and the power mode. Fatal if wrong.
        Only probe lines whose 10 s lie wholly after arrival count: the first one after arrival
        still covers the loading screen, where the clock differs (r64: 3,299 MHz against ~3,140
        at the fountain)."""
        gates = self.plan.get("gates", {})
        since = record["arrival_log_t"]
        probes = [m for t, m in self.state.probes if t - 10.0 >= since]
        band = gates.get("probe_mhz", "sitting")
        if probes:
            mhz = probes[-1]
            if isinstance(band, list) and not band[0] <= mhz <= band[1]:
                raise Fatal(f"GPU clock probe {mhz} MHz outside {band} (Force maximum clocks?)")
            if band == "sitting" and self.probe_values:
                mid = st.median(self.probe_values)
                pct = max(PROBE_FATAL_PCT, gates.get("probe_band_pct", 3.0))
                if abs(mhz - mid) > pct / 100.0 * mid:
                    raise Fatal(f"GPU clock probe {mhz} MHz is more than {pct:g}% from the sitting's {mid:.0f} MHz")
        modes = {c["mode"] for c in self.state.clock if c["t"] > since and c["mode"]}
        want = gates.get("power_mode", "best performance")
        if want and modes and modes != {want}:
            raise Fatal(f"power mode {sorted(modes)}, not {want}")
        if any(c["ac"] == "off" for c in self.state.clock if c["t"] > since):
            raise Fatal("the laptop went on battery")

    def launch(self, arm_name, label, measure_s, fullscreen=True, boot_timeout_s=None, settle_s=None,
               allow_precache=False):
        """One launch from start to the copied log. Returns its record (run.json)."""
        plan = self.plan
        run_dir = os.path.join(self.session_dir, "runs", label)
        os.makedirs(run_dir, exist_ok=True)
        arm = plan["arms"][arm_name]
        settle_s = plan["settle_s"] if settle_s is None else settle_s
        boot_timeout_s = boot_timeout_s or plan["boot_timeout_s"]
        record = {"label": label, "arm": arm_name, "env_diff": arm.get("env", {}), "base": arm.get("base")}
        self.ensure_closed()
        paths = logscan.log_paths(self.log_dir)
        previous = logscan.digest(paths["log"], self.host.open_shared)
        env, exe = self.environment(arm_name, label)
        cmd = [os.path.join(self.local["build_dir"], exe)] + (["-f"] if fullscreen else []) + ["-g", self.local["game_file"]]
        launched = self.host.now()
        process = self.host.launch(cmd, env, self.local["build_dir"])
        created = self.host.creation_time(process.pid) or launched
        record.update(launched=launched, created=created, pid=process.pid, fullscreen=fullscreen)
        self.client = self.client_factory()
        self.tail = None
        expect = arm.get("expect_lines", [])
        self.state = logscan.LogState(expect)
        failure = None
        try:
            self.boot(record, process, launched, previous, expect, boot_timeout_s, arm_name, allow_precache)
            nav = navigate.Navigator(self.client, self.draws_source(), self.clock, self.script, self.refs,
                                     self.hotkeys, ask=self.ask, say=self.log)
            try:
                record["navigation"] = nav_record = nav.run()
            except navigate.NavError as error:
                raise RunFailed(f"navigation: {error}")
            if nav_record.get("taught"):
                self.refs.update(nav.refs)
                record["taught"] = nav_record["taught"]
            arrival = self.host.now()
            self.tail.poll()
            record["arrival_wall"] = arrival
            record["arrival_log_t"] = arrival - created
            png = nav_record.pop("arrival_png", None)
            if png:
                os.makedirs(os.path.join(run_dir, "shots"), exist_ok=True)
                with open(os.path.join(run_dir, "shots", "arrival.png"), "wb") as stream:
                    stream.write(png)
            self.status(phase="settle", run=label, arm=arm_name)
            self.settle(record, arm_name, arrival, settle_s)
            record["pre_thumb"], err = self.shot(run_dir, "pre-window")
            self.clock.sleep(max(0.0, arrival + settle_s - self.host.now()))
            self.status(phase="measure", run=label, arm=arm_name)
            self.measure(record, created, measure_s)
            self.clock.sleep(2.0)
            record["end_thumb"], err = self.shot(run_dir, "end")
            if record["end_thumb"] is None:
                raise RunFailed(f"no screenshot after the window (minimized?): {err}")
            d = screens.distance(record["pre_thumb"], record["end_thumb"])
            record["window_thumb_distance"] = d
            if d is not None and d > plan.get("thumb_threshold", 24):
                record.setdefault("flags", []).append(f"the screen changed during the window ({d} bits)")
        except (RunFailed, Fatal) as error:
            failure = error
        except navigate.NavSetupError as error:
            failure = Fatal(f"boot script: {error}")
        except ClientError as error:
            failure = RunFailed(f"debug server: {error}")
        except Aborted as error:
            failure = error
        except KeyboardInterrupt:
            failure = Aborted("Ctrl+C")
        finally:
            if isinstance(failure, RunFailed):
                try:
                    self.on_failure(record, run_dir, process, str(failure))
                except Exception as error:  # noqa: BLE001 - quitting Lichii comes first
                    record["failure_evidence"] = f"not taken: {error}"
            self.client.close()  # releases any held input
            self.status(phase="quit", run=label)
            record["quit"] = quit_lichii(self.host, process, self.local.get("game_title", "Ocarina of Time"))
            if record["quit"]["killed"]:
                record.setdefault("flags", []).append("Lichii was killed after 60 s (the last second of the log may be lost)")
            self.collect(record, run_dir, launched)
        if isinstance(failure, Aborted):
            record.update(status="aborted", reason=str(failure))
            write_json(os.path.join(run_dir, "run.json"), record)
            failure.record = record
            raise failure
        if failure is None:
            self.check_log(record, run_dir, arm_name, allow_precache)
        else:
            record.update(status="fatal" if isinstance(failure, Fatal) else "failed", reason=str(failure))
        write_json(os.path.join(run_dir, "run.json"), record)
        if record["status"] == "fatal":
            error = Fatal(record["reason"])
            error.record = record
            raise error
        return record

    def on_failure(self, record, run_dir, process, reason):
        """A thumbnail if the server still answers; a thread-stack dump if Lichii looks hung."""
        if process.poll() is not None:
            return
        try:
            record["failure_thumb"], _ = self.shot(run_dir, "failure")
        except ClientError:
            pass
        if any(word in reason for word in ("presents stopped", "no answer", "timed out", "no frame within")):
            try:
                record["hang_dump"] = self.host.minidump(process.pid, os.path.join(run_dir, "hang.dmp"))
            except OSError as error:
                record["hang_dump"] = str(error)

    def boot(self, record, process, launched, previous, expect, boot_timeout_s, arm_name, allow_precache):
        # This run's log: newer than the launch, rotated over the previous one, of the plan's build.
        deadline = launched + 60
        while True:
            if process.poll() is not None:
                raise RunFailed(f"lichii.exe exited with {process.poll()} while booting")
            try:
                path, _ = logscan.identify_log(self.log_dir, launched, previous, self.plan.get("build"),
                                               self.host.open_shared, self.host_stat)
                break
            except logscan.LogError as error:
                if error.final:
                    raise (Fatal if error.fatal else RunFailed)(str(error))
                if self.host.now() > deadline:
                    raise RunFailed(f"this run's log: {error}")
            self.clock.sleep(1.0)
        self.tail = logscan.Tail(path, self.state, self.host.open_shared)
        # Boot lines: the game, the settings dump and the switches line, within 60 s.
        switches = benchplan.expected_switches(self.plan["arms"][arm_name])
        while not (self.state.program_id and self.state.settings and self.state.switches is not None):
            self.tail.poll()
            if self.host.now() > deadline:
                raise RunFailed("no Booting game, settings dump or TEST switches line within 60 s")
            if process.poll() is not None:
                raise RunFailed(f"lichii.exe exited with {process.poll()} while booting")
            self.clock.sleep(1.0)
        if self.state.program_id != benchplan.OOT:
            raise Fatal(f"booted {self.state.program_id}, not Ocarina of Time: fix game_file in local.json")
        for key, want in benchplan.settings_gate(self.plan).items():
            have = self.state.setting(key)
            if have is None or logscan.norm(have) != logscan.norm(want):
                raise Fatal(f"setting {key} is {have}, not {want}")
        for name, want in switches.items():
            have = self.state.switches.get(name)
            if have is None or logscan.norm(have) != logscan.norm(want):
                raise Fatal(f"TEST switches line shows {name} {have}, not {want}")
        # The server and the first present.
        deadline = launched + boot_timeout_s
        while True:
            if process.poll() is not None:
                raise RunFailed(f"lichii.exe exited with {process.poll()} before the first frame")
            try:
                status = self.call("status", {"fps_window_ms": 0})
                if status.get("program_id") != benchplan.OOT:
                    raise Fatal(f"the debug server reports {status.get('program_id')}")
                if status.get("presents", 0) > 0:
                    record["server_build"] = status.get("build")
                    break
            except NotListening:
                pass
            self.tail.poll()
            if self.state.precache and not allow_precache:
                raise RunFailed("this launch runs the shader precache: run bench.bat prepare first")
            if self.host.now() > deadline:
                raise RunFailed(f"no frame within {boot_timeout_s} s")
            self.clock.sleep(1.0)
        record["first_present_wall"] = self.host.now()

    @property
    def host_stat(self):
        return getattr(self.host, "stat", os.stat)

    def settle(self, record, arm_name, arrival, settle_s):
        """No input. Every few seconds: presents still advancing, the gates from the disk log."""
        last_presents = None
        stalled_since = None
        end = arrival + settle_s - 3.0
        while self.host.now() < end:
            self.tail.poll()
            self.gate(arm_name, record)
            status = self.call("status", {"fps_window_ms": 0})
            now = self.host.now()
            if status.get("presents") == last_presents:
                stalled_since = stalled_since or now
                if now - stalled_since > 20:
                    raise RunFailed("presents stopped while settling")
            else:
                last_presents, stalled_since = status.get("presents"), None
            self.clock.sleep(min(5.0, max(0.0, end - self.host.now())))

    def measure(self, record, created, measure_s):
        """frame_times calls of at most chunk_s each; nothing else touches the server meanwhile."""
        chunk = min(25.0, float(self.plan.get("chunk_s", 25)))
        start = self.host.now()
        self.tail.poll()
        newest_start = self.state.last_t
        results = []
        left = float(measure_s)
        while left > 0.05:
            seconds = min(chunk, left)
            result = self.call_polled("frame_times", {"seconds": seconds, "refresh_hz": 120})
            results.append({k: result.get(k) for k in ("frames", "mean_ms", "fps", "p50_ms", "p99_ms", "max_ms",
                                                      "stutters", "truncated")})
            left = measure_s - (self.host.now() - start)
        end = self.host.now()
        self.tail.poll()
        record["window_wall"] = [start, end]
        record["window_log"] = [round(start - created, 3), round(end - created, 3)]
        record["window_log_newest"] = [newest_start, self.state.last_t]
        record["frame_times"] = results
        if any(r.get("truncated") for r in results):
            record.setdefault("flags", []).append("a frame_times call was truncated")
        frames = sum(r.get("frames") or 0 for r in results)
        if frames:
            record["server_fps"] = sum((r.get("fps") or 0) * (r.get("frames") or 0) for r in results) / frames

    def collect(self, record, run_dir, launched):
        paths = logscan.log_paths(self.log_dir)
        for _ in range(10):
            try:
                with self.host.open_shared(paths["log"]) as src, open(os.path.join(run_dir, "eden_log.txt"), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                record["log_copied"] = True
                break
            except OSError as error:
                record["log_copied"] = f"not copied: {error}"
                self.host.sleep(1.0)
        crash = os.path.join(self.log_dir, "eden_crash.dmp")
        try:
            if self.host_stat(crash).st_mtime >= launched:
                shutil.copy2(crash, os.path.join(run_dir, "eden_crash.dmp"))
                record["crash_dump"] = True
        except OSError:
            pass

    def check_log(self, record, run_dir, arm_name, allow_precache):
        arm = self.plan["arms"][arm_name]
        log = os.path.join(run_dir, "eden_log.txt")
        if record.get("log_copied") is not True:
            record.update(status="failed", reason=f"the log was {record.get('log_copied')}")
            return
        state = logscan.parse_file(log, arm.get("expect_lines", []))
        problems, flags, values = logscan.validate_run(
            state, self.plan, arm_name, record.get("window_log"), benchplan.settings_gate(self.plan),
            benchplan.expected_switches(arm), arm.get("expect_lines", []))
        if allow_precache:
            problems = [p for p in problems if not p.startswith("shader precache")]
        record["values"] = values
        record["settings"] = state.settings
        record.setdefault("flags", []).extend(flags)
        record["problems"] = problems
        fatal = [p for p in problems if p.startswith(FATAL_PREFIXES)]
        if fatal:
            record.update(status="fatal", reason="; ".join(fatal))
        elif problems:
            record.update(status="failed", reason="; ".join(problems))
        else:
            record["status"] = "ok"
            if values.get("probe_mhz"):
                self.probe_values.append(values["probe_mhz"])

    def run_one(self, slot, attempt):
        self.launches += 1
        label = f"{self.launches:02d}-{'W-' if slot['warmup'] else ''}{slot['arm']}"
        measure = self.plan["warmup_measure_s"] if slot["warmup"] else self.plan["measure_s"]
        self.log(f"LICHII BENCH RUNNING: launch {self.launches}, slot {slot['slot']}, arm {slot['arm']}"
                 f"{' (warm-up)' if slot['warmup'] else ''}{', retry' if attempt > 1 else ''}. Hold F3 to stop.")
        self.status(phase="launch", run=label, arm=slot["arm"], slot=slot["slot"], attempt=attempt)
        info = dict(slot=slot["slot"], block=slot["block"], warmup=slot["warmup"], attempt=attempt)
        try:
            record = self.launch(slot["arm"], label, measure)
        except (Fatal, Aborted) as error:
            if getattr(error, "record", None):
                error.record.update(info)
                write_json(os.path.join(self.session_dir, "runs", label, "run.json"), error.record)
            raise
        record.update(info)
        write_json(os.path.join(self.session_dir, "runs", label, "run.json"), record)
        self.log(f"{label}: {record['status']}" + (f" ({record.get('reason')})" if record.get("reason") else "")
                 + (f", {record['server_fps']:.2f} fps" if record.get("server_fps") else ""))
        control = read_json(os.path.join(self.session_dir, "control.json"), {}) or {}
        if control.get("stop_after_run"):
            self.plan["_stop_after_run"] = True
        # The same cooldown after every launch, so every run starts from the same rest. A stop
        # during it keeps this run's record (Lichii is already closed).
        try:
            self.clock.sleep(self.plan["cooldown_s"])
        except (Aborted, KeyboardInterrupt) as error:
            stop = error if isinstance(error, Aborted) else Aborted("Ctrl+C")
            stop.record = record
            raise stop
        return record


# ---------------------------------------------------------------------------------------------
# Session end: sitting checks, config comparison, summary, zip


IGNORED_CONFIG_KEYS = re.compile(r"fullscreen|geometry|state|windowState|lastPath|play_time", re.I)


def compare_configs(start_dir, current_files):
    lines = []
    for name, path in current_files.items():
        try:
            before = parse_qt_ini(benchplan.read_text(os.path.join(start_dir, name)))
            after = parse_qt_ini(benchplan.read_text(path))
        except OSError as error:
            lines.append(f"{name}: can't compare ({error})")
            continue
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key) and not IGNORED_CONFIG_KEYS.search(key):
                lines.append(f"{name}: {key}: {before.get(key)!r} -> {after.get(key)!r}")
    return lines


def finish(session_dir, plan, results, notes, stopped, config_lines):
    counted = {r["label"]: r for r in results if r["status"] == "ok" and not r.get("warmup") and not r.get("dropped")}
    sitting = logscan.sitting_checks({k: {"values": r.get("values", {}), "settings": r.get("settings", {})}
                                      for k, r in counted.items()}, plan.get("gates", {}))
    for label, flags in sitting.items():
        counted[label].setdefault("flags", []).extend(flags)
    # Every run.json gets the final word: a dropped block and the sitting's flags.
    for r in results:
        path = os.path.join(session_dir, "runs", r["label"], "run.json")
        if os.path.isdir(os.path.dirname(path)):
            write_json(path, r)
    index = [{k: r.get(k) for k in ("label", "arm", "slot", "block", "warmup", "attempt", "status", "dropped",
                                    "reason", "flags", "window_log", "server_fps")} for r in results]
    write_json(os.path.join(session_dir, "runs.json"), {"plan": plan.get("name"), "runs": index, "notes": notes,
                                                         "stopped": stopped})
    out = [f"Lichii Bench: {plan.get('name')}", f"session {os.path.basename(session_dir)}",
           f"result: {stopped or 'finished'}", ""]
    cols = ("server_fps", "fps", "frame_ms", "p99_ms", "gpu_busy_ms", "probe_mhz", "back_ghz", "pkg_w", "draws_pf")
    out.append(f"{'run':<16} {'status':<8} " + " ".join(f"{c:>11}" for c in cols))
    for r in results:
        v = dict(r.get("values", {}), server_fps=r.get("server_fps"))
        mark = " dropped" if r.get("dropped") else " (W)" if r.get("warmup") else ""
        out.append(f"{r['label']:<16} {r['status']:<8} " + " ".join(
            f"{v[c]:>11.2f}" if isinstance(v.get(c), (int, float)) else f"{'-':>11}" for c in cols) + mark)
        for text in ([r["reason"]] if r.get("reason") else []) + r.get("flags", []):
            out.append(f"    {text}")
    out.append("")
    out.append("per arm (counted runs only):")
    arms = {}
    for r in counted.values():
        arms.setdefault(r["arm"], []).append(dict(r.get("values", {}), server_fps=r.get("server_fps")))
    for arm, vals in arms.items():
        parts = [f"{len(vals)} runs"]
        for c in ("server_fps", "fps", "gpu_busy_ms", "probe_mhz"):
            xs = [v[c] for v in vals if isinstance(v.get(c), (int, float))]
            if xs:
                parts.append(f"{c} {st.mean(xs):.3f}")
        out.append(f"  {arm}: " + ", ".join(parts))
    out.append("")
    out.append("per block (second arm minus first, GPU busy ms and fps):")
    blocks = {}
    for r in counted.values():
        if r.get("block") is not None:
            blocks.setdefault(r["block"], []).append(r)
    for b in sorted(blocks):
        rs = blocks[b]
        names = list(dict.fromkeys(r["arm"] for r in rs))
        if len(names) == 2:
            def mean_of(arm, key):
                xs = [r.get("values", {}).get(key) for r in rs if r["arm"] == arm]
                xs = [x for x in xs if x is not None]
                return st.mean(xs) if xs else None
            parts = []
            for key in ("gpu_busy_ms", "fps"):
                a, c = mean_of(names[1], key), mean_of(names[0], key)
                if a is not None and c is not None:
                    parts.append(f"{key} {a - c:+.3f}")
            out.append(f"  block {b + 1} ({names[1]} - {names[0]}): " + ", ".join(parts))
    if notes:
        out += ["", "notes:"] + [f"  {n}" for n in notes]
    if config_lines:
        out += ["", "config changes since the start (bench.bat restore-config puts the start copies back):"]
        out += [f"  {line}" for line in config_lines]
    out += ["", "The verdicts come from analyze.py with the plan's rules; upload the zip."]
    text = "\n".join(out) + "\n"
    with open(os.path.join(session_dir, "summary.txt"), "w", encoding="utf-8") as stream:
        stream.write(text)
    return text


ZIP_FILES = re.compile(r"^(plan\.json|status\.json|runner\.log|summary\.txt|runs\.json|config-diff\.txt|"
                       r"runs/[^/]+/(eden_log\.txt|run\.json|eden_crash\.dmp|hang\.dmp|shots/[^/]+\.png))$")


def make_zip(session_dir, results_dir, exclude=None, name=None):
    """Logs, run.json files, thumbnails and the summary; never saves, keys, configs, nand or sdmc.
    `exclude` (a regex on the path in the session) leaves more out, for a smaller zip."""
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, (name or os.path.basename(session_dir)) + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(session_dir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, session_dir).replace(os.sep, "/")
                if ZIP_FILES.match(rel) and not (exclude and re.search(exclude, rel)):
                    z.write(full, os.path.basename(session_dir) + "/" + rel)
    return path


# ---------------------------------------------------------------------------------------------
# Setup data


def refs_dir():
    return os.path.join(DATA, "refs")


def load_refs():
    return read_json(os.path.join(refs_dir(), "refs.json"), {}) or {}


def save_refs(refs):
    os.makedirs(refs_dir(), exist_ok=True)
    write_json(os.path.join(refs_dir(), "refs.json"), refs)


def load_local():
    local = read_json(os.path.join(DATA, "local.json"))
    if not local:
        raise Fatal(f"no {os.path.join(DATA, 'local.json')}: run bench.bat setup first")
    return local


def config_files(local):
    return {"qt-config.ini": local["config_file"], GAME_CONFIG: os.path.join(local["data_root"], "config", "custom", GAME_CONFIG)}


def preflight(host, local, need_ac=True):
    problems = []
    if host.running(local.get("exe", "lichii.exe")) or host.port_open(ADDRESS):
        problems.append("Lichii is running (or port 6545 answers): close it first")
    if need_ac and host.ac_power() is False:
        problems.append("the laptop is on battery: plug it in")
    if host.reboot_pending():
        problems.append("Windows has a restart pending: restart first, so it can't happen during the sitting")
    try:
        if host.disk_free(DATA) < 2 << 30:
            problems.append(f"less than 2 GB free on {DATA}")
    except OSError:
        pass
    if problems:
        raise Fatal("; ".join(problems))


def new_session(plan, kind="run"):
    base = os.path.join(DATA, "sessions", f"{stamp()}-{slug(plan.get('name', kind))}")
    session_dir, n = base, 1
    while os.path.exists(session_dir):
        n += 1
        session_dir = f"{base}-{n}"
    os.makedirs(os.path.join(session_dir, "runs"))
    write_json(os.path.join(session_dir, "plan.json"), plan)
    write_json(os.path.join(session_dir, "control.json"), {"abort": False, "stop_after_run": False})
    return session_dir


def copy_configs(local, session_dir):
    start = os.path.join(session_dir, "config-start")
    os.makedirs(start, exist_ok=True)
    for name, path in config_files(local).items():
        if os.path.exists(path):
            shutil.copy2(path, os.path.join(start, name))
    return start


# ---------------------------------------------------------------------------------------------
# Commands


def cmd_run(host, plan_path, yes=False, say=print, ask=input):
    local = load_local()
    plan = benchplan.load_plan(plan_path, local["build_dir"])
    slots = benchplan.schedule(plan)
    say(f"Plan: {plan.get('name')}")
    for name, arm in plan["arms"].items():
        say(f"  arm {name}: {arm.get('base')} + {arm.get('env') or 'nothing'}")
    say(f"  {len(slots)} launches ({sum(s['warmup'] for s in slots)} warm-up), about "
        f"{benchplan.estimate_minutes(plan):.0f} min; rules: "
        + "; ".join(f"{r['item']} vs {r['control']} on {r['metric']}" for r in plan["rules"]))
    preflight(host, local)
    say("Plugged in, Do Not Disturb on, other GPU programs closed, controller put down?")
    if not yes:
        ask("Press Enter to start (Ctrl+C to cancel). Then walk away; hold F3 to stop. ")
    session_dir = new_session(plan)
    copy_configs(local, session_dir)
    host.keep_awake(True)
    host.lower_priority()
    runner = Runner(host, local, plan, session_dir, say=say)
    try:
        try:
            results, notes, stopped = run_schedule(plan, runner.run_one, say=runner.log)
        except KeyboardInterrupt:
            results, notes, stopped = [], [], "aborted (Ctrl+C)"
        return end_session(host, local, plan, session_dir, results, notes, stopped, runner, say)
    finally:
        host.keep_awake(False)


def end_session(host, local, plan, session_dir, results, notes, stopped, runner, say, quiet=False):
    if not results:
        results = [read_json(os.path.join(session_dir, "runs", d, "run.json"))
                   for d in sorted(os.listdir(os.path.join(session_dir, "runs")))]
        results = [r for r in results if r]
    config_lines = compare_configs(os.path.join(session_dir, "config-start"), config_files(local))
    with open(os.path.join(session_dir, "config-diff.txt"), "w", encoding="utf-8") as stream:
        stream.write("\n".join(config_lines) + "\n")
    text = finish(session_dir, plan, results, notes, stopped, config_lines)
    runner.status(phase="done", result=stopped or "finished")
    path = make_zip(session_dir, os.path.join(DATA, "results"))
    say(text)
    if quiet:
        return path
    say(f"Upload this one file: {path}")
    try:
        host.beep()
        host.show_file(path)
    except OSError:
        pass
    return path


def check_plan_for(local, settle_s, measure_s):
    base = local.get("check_base", "perf-trace-noprofile.bat")
    return benchplan.load_plan({
        "name": "check", "build": local.get("build"), "arms": {"N": {"base": base, "env": {}}},
        "blocks": [["N"]], "settle_s": settle_s, "measure_s": measure_s, "cooldown_s": 20,
        "gates": {"probe_mhz": "sitting"},
        "rules": [{"item": "N", "control": "N", "metric": "fps", "better": "higher", "min_effect": 1}],
    }, local["build_dir"])


def cmd_check(host, say=print, ask=input, fullscreen_only=False):
    local = load_local()
    preflight(host, local)
    plan = check_plan_for(local, 15, 15)
    session_dir = new_session(plan, "check")
    host.keep_awake(True)
    ok = True
    try:
        if not fullscreen_only:
            say("Check 1 of 3, windowed, while you watch: Lichii opens Ocarina in a window. At each menu the "
                "console asks whether the expected screen shows; press Enter to let it press A.")
            runner = Runner(host, local, plan, session_dir, say=say, ask=ask, refs={})
            record = runner.launch("N", "01-check-windowed", 15, fullscreen=False)
            save_refs(runner.refs)
            say(f"windowed check: {record['status']} {record.get('reason', '')}")
            boxes = record["quit"]["confirm_answered"]
            say(f"quit: {record['quit']['close_sent']} close request(s); the 'Are you sure' box was "
                f"{'answered' if boxes else 'not shown'}" + ("; set Confirm before stopping emulation to "
                "'Never ask' for sittings if you prefer no box" if boxes else ""))
            ambiguous = ambiguous_refs(runner.refs, runner.script.get("thumb_threshold", 24))
            for a, b, d in ambiguous:
                say(f"warning: the {a} and {b} screens look alike ({d} bits); check the shots")
            ok = record["status"] == "ok"
        for n in (2, 3):
            if not ok:
                break
            say(f"Check {n} of 3, fullscreen, on its own.")
            runner = Runner(host, local, plan, session_dir, say=say)
            runner.launches = n - 1
            record = runner.launch("N", f"0{n}-check-fullscreen", 15)
            say(f"fullscreen check {n}: {record['status']} {record.get('reason', '')}")
            ok = record["status"] == "ok"
    except Fatal as error:
        say(f"check stopped: {error}")
        ok = False
    finally:
        host.keep_awake(False)
    path = make_zip(session_dir, os.path.join(DATA, "results"))
    say(("All checks passed." if ok else "The check failed; upload the zip.") + f" {path}")
    return ok


def ambiguous_refs(refs, threshold):
    names = list(refs)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = screens.distance(refs[a]["hash"], refs[b]["hash"])
            if d is not None and d < 2 * threshold:
                out.append((a, b, d))
    return out


def cmd_prepare(host, say=print, local=None, guard=None, runner_kwargs=None):
    """A new build's first launch: the precache and preparation (up to 75 min), then 60 s at the
    save and a clean quit, so measured runs start warm. The autopilot passes the new build's
    local (its build_dir and build) and its owner-input guard."""
    local = local or load_local()
    preflight(host, local)
    plan = check_plan_for(local, 60, 10)
    session_dir = new_session(plan, "prepare")
    host.keep_awake(True)
    try:
        runner = Runner(host, local, plan, session_dir, say=say, guard=guard, **(runner_kwargs or {}))
        try:
            record = runner.launch("N", "01-prepare", 10, boot_timeout_s=75 * 60, allow_precache=True)
        except (Aborted, Fatal) as error:
            if getattr(error, "record", None):
                error.record["session"] = session_dir
            raise
    finally:
        host.keep_awake(False)
    record["session"] = session_dir
    say(f"prepare: {record['status']} {record.get('reason', '')}")
    return record


def cmd_setup(host, say=print, ask=input):
    build_dir = os.path.dirname(HERE)
    exe = os.path.join(build_dir, "lichii.exe")
    if not os.path.exists(exe):
        raise Fatal(f"no lichii.exe in {build_dir}: unzip the bench kit into the build folder")
    portable = os.path.join(build_dir, "user")
    data_root = portable if os.path.isdir(portable) else os.path.join(os.environ.get("APPDATA", ""), "lichii")
    config_file = os.path.join(data_root, "config", "qt-config.ini")
    ini = parse_qt_ini(benchplan.read_text(config_file)) if os.path.exists(config_file) else {}
    say(f"Lichii data folder: {data_root}")
    old = read_json(os.path.join(DATA, "local.json"), {}) or {}
    recent = [p for p in ini.get("UI/Paths/recentFiles", "").split(", ") if p]
    guess = old.get("game_file") or next((p for p in recent if re.search(r"ocarina|zelda|oot", p, re.I)), "")
    answer = ask(f"Ocarina of Time file [{guess}]: ").strip().strip('"')
    game_file = answer or guess
    if not game_file or not os.path.exists(game_file):
        raise Fatal(f"no game file at {game_file!r}")
    hotkeys = read_hotkeys(ini)
    confirm = ini.get("UI/confirmStop", "0")
    local = {"build_dir": build_dir, "exe": "lichii.exe", "game_file": game_file, "data_root": data_root,
             "log_dir": os.path.join(data_root, "log"), "config_file": config_file, "hotkeys": hotkeys,
             "check_base": "perf-trace-noprofile.bat", "game_title": "Ocarina of Time",
             "build": old.get("build")}
    os.makedirs(DATA, exist_ok=True)
    write_json(os.path.join(DATA, "local.json"), local)
    say(f"Wrote {os.path.join(DATA, 'local.json')}")
    for name, combo in navigate.hotkey_sets(hotkeys):
        say(f"  controller hotkey {name}: {'+'.join(sorted(combo))} (the bench never presses it)")
    say({"0": "Confirm before stopping: Always ask. The bench answers the 'Are you sure' box itself; "
              "'Never ask' (Emulation settings) avoids it during sittings.",
         "1": "Confirm before stopping: only if the game asks.", "2": "Confirm before stopping: Never ask."}
        .get(confirm, f"Confirm before stopping: {confirm}"))
    if ini.get("UI/pauseWhenInBackground", "false").lower() == "true":
        say("WARNING: 'Pause emulation when in background' is on: turn it off for sittings.")
    if ini.get("Controls/player_0_connected", "true").lower() == "false":
        say("WARNING: Player 1 isn't connected in Lichii's controller settings.")
    backup_saves(local, say)
    say(f"AC power: {host.ac_power()}; restart pending: {host.reboot_pending()}")
    say("Next: bench.bat check")
    return local


def backup_saves(local, say=print):
    """A copy of Ocarina's save folders; the bench never writes to them."""
    root = os.path.join(local["data_root"], "nand", "user", "save")
    found = []
    for base, dirs, _ in os.walk(root):
        for d in dirs:
            if d.upper() == benchplan.OOT:
                found.append(os.path.join(base, d))
    if not found:
        say(f"no Ocarina save folder under {root}; nothing backed up")
        return None
    target = os.path.join(DATA, "save-backups", stamp())
    for i, src in enumerate(found):
        shutil.copytree(src, os.path.join(target, f"{i}-{os.path.basename(src)}"))
    say(f"Saves backed up to {target}")
    return target


def cmd_restore_config(host, session, say=print):
    local = load_local()
    if host.running(local.get("exe", "lichii.exe")):
        raise Fatal("close Lichii first")
    session_dir = session if os.path.isdir(session) else os.path.join(DATA, "sessions", session)
    start = os.path.join(session_dir, "config-start")
    keep = os.path.join(session_dir, "config-before-restore")
    os.makedirs(keep, exist_ok=True)
    for name, path in config_files(local).items():
        src = os.path.join(start, name)
        if os.path.exists(src):
            if os.path.exists(path):
                shutil.copy2(path, os.path.join(keep, name))
            shutil.copy2(src, path)
            say(f"restored {path} (the replaced one is in {keep})")


def make_host():
    if os.name != "nt":
        raise Fatal("Lichii Bench runs on Windows")
    return winhost.WinHost()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lower().endswith(".json"):
        argv = ["run"] + argv
    parser = argparse.ArgumentParser(prog="bench.bat", description="Lichii Bench")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    check = sub.add_parser("check")
    check.add_argument("--fullscreen-only", action="store_true")
    sub.add_parser("prepare")
    run = sub.add_parser("run")
    run.add_argument("plan")
    run.add_argument("--yes", action="store_true")
    restore = sub.add_parser("restore-config")
    restore.add_argument("session")
    z = sub.add_parser("zip")
    z.add_argument("session")
    sub.add_parser("validate").add_argument("plan")
    auto = sub.add_parser("autopilot", help="install new CI builds, prepare them and run queued plans, unattended")
    auto.add_argument("--once", action="store_true", help="one pass over the work there is now, then exit")
    auto.add_argument("--idle-min", type=float, help="minutes the owner must be idle before a launch (default 15)")
    sub.add_parser("autopilot-stop", help="ask a running autopilot to stop after the current launch")
    sub.add_parser("install-autostart", help="start the autopilot at logon (a Startup entry)")
    sub.add_parser("uninstall-autostart", help="remove the Startup entry")
    args = parser.parse_args(argv)
    if args.command in ("autopilot", "autopilot-stop", "install-autostart", "uninstall-autostart"):
        import autopilot
        return autopilot.main(args)
    try:
        if args.command == "validate":
            plan = benchplan.load_plan(args.plan)
            print(f"{plan['name']}: {len(benchplan.schedule(plan))} launches, ~{benchplan.estimate_minutes(plan):.0f} min")
            return 0
        if args.command == "zip":
            session_dir = args.session if os.path.isdir(args.session) else os.path.join(DATA, "sessions", args.session)
            print(make_zip(session_dir, os.path.join(DATA, "results")))
            return 0
        host = make_host()
        if args.command == "setup":
            cmd_setup(host)
        elif args.command == "check":
            return 0 if cmd_check(host, fullscreen_only=args.fullscreen_only) else 1
        elif args.command == "prepare":
            cmd_prepare(host)
        elif args.command == "run":
            cmd_run(host, args.plan, yes=args.yes)
        elif args.command == "restore-config":
            cmd_restore_config(host, args.session)
    except (Fatal, benchplan.PlanError, navigate.NavSetupError) as error:
        print(f"LICHII BENCH STOPPED: {error}")
        return 2
    except KeyboardInterrupt:
        print("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
