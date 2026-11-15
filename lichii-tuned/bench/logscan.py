"""Lichii logs for the bench: incremental parsing, finding this run's log, and validity checks.

Logs are data: lines are only matched with regular expressions. The same parser reads the disk
log while a run goes (flushed about once a second) and the copied log afterwards.
"""

import codecs
import hashlib
import os
import re
import statistics as st

LINE = re.compile(r'^\[\s*([0-9.]+)\] (\S+) <(\w+)> ([^:]+):(\d+):(\S+?): (.*)$')
DRAWS = re.compile(r'DKB draws through_frame=\d+ frames=(\d+) attempts=(\d+)')
GPU = re.compile(r'gpu_busy_ms=([0-9.]+) measured_submissions=\d+ dropped_submissions=(\d+) .*?draws_timed=(\d)')
PROBE = re.compile(r'GPU clock probe in 10 s: median (\d+) MHz')
BACK = re.compile(r'\bback (?:core=\d+ )?ghz=([0-9.]+|n/a) cpu_ms=[0-9.]+ poll_ms=[0-9.]+ work_ms=([0-9.]+)')
PKG = re.compile(r'RAPL_Package0_PKG=([0-9.]+)W')
MODE = re.compile(r'power_mode="([^"]*)"')
AC = re.compile(r'\bac=(\w+)')
WIN = re.compile(r'\(EDEN_NX2_CLOCK_TRACE, ([0-9.]+) s\)')
POWER_LINE = re.compile(r'Windows power mode "([^"]*)".*?, on (AC|battery) power')
TRANSLATE = re.compile(r'Async translate in the last 10 s: (\d+) queued')
WORST = re.compile(r'Worst frame in the last 10 s: .*?in it (\d+) pipelines built')
BOOT = re.compile(r'Booting game: ([0-9A-Fa-f]{16})')
SETTING = re.compile(r'^\S\S ([\w.]+): ?(.*)$')
SWITCH_ITEM = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*) (\S+)')
# Shader precache or pipeline preparation that ran in this launch (a measured run must have none).
QUIET_PRECACHE = ("nothing to precache", "compute programs already done", "skipped")
PREP_WORK = re.compile(r'Precompiled pipelines: (?:assembling|\d+ of \d+ done|.*\bto prepare\b|.*\bin [0-9.]+ s: )')


class LogError(Exception):
    """`final`: waiting longer won't help; `fatal`: retrying the run won't help either."""

    def __init__(self, message, final=False, fatal=False):
        super().__init__(message)
        self.final = final
        self.fatal = fatal


class LogState:
    def __init__(self, expect_lines=()):
        self.partial = ""
        self.lines = 0
        self.head = []
        self.version = None
        self.version_line = None
        self.program_id = None
        self.settings = {}
        self.switches = None
        self.power = None
        self.probes = []
        self.clock = []
        self.draws = []
        self.frames = []
        self.gpu = []
        self.translate = []
        self.worst = []
        self.precache = []
        self.crash = []
        self.first_present = None
        self.shutdown = None
        self.closed = None
        self.last_t = None
        self.expect = {p: 0 for p in expect_lines}
        self._expect_rx = [(p, re.compile(p)) for p in expect_lines]

    def feed(self, text):
        """Feeds raw text; a trailing partial line waits for the next call."""
        text = self.partial + text
        lines = text.split("\n")
        self.partial = lines.pop()
        for line in lines:
            self.line(line.rstrip("\r"))

    def line(self, raw):
        self.lines += 1
        if len(self.head) < 8:
            self.head.append(raw)
        m = LINE.match(raw)
        if not m:
            return
        t = float(m[1])
        fn, msg = m[6], m[7]
        self.last_t = t
        for p, rx in self._expect_rx:
            if rx.search(msg):
                self.expect[p] += 1
        if msg.startswith("DKB "):
            if msg.startswith("DKB draws through_frame"):
                d = DRAWS.search(msg)
                if d and int(d[1]) > 0:
                    self.draws.append((t, int(d[1]), int(d[2]) / int(d[1])))
            elif msg.startswith("DKB frame_ms n="):
                vals = [float(x) for x in msg.split()[3:]]
                end = t
                seq = []
                for v in reversed(vals):
                    seq.append((end, v))
                    end -= v / 1000.0
                self.frames.extend(reversed(seq))
            elif msg.startswith("DKB gpu_frame"):
                g = GPU.search(msg)
                if g and int(g[2]) == 0 and g[3] == "0":
                    self.gpu.append((t, float(g[1])))
            elif msg.startswith("DKB frame present=1 ") and self.first_present is None:
                self.first_present = t
            return
        if self.version is None and "Lichii Version:" in msg:
            self.version = msg.split("Lichii Version:", 1)[1].strip()
            self.version_line = self.lines
        if fn == "LogSettings":
            s = SETTING.match(msg)
            if s:
                self.settings.setdefault(s[1], s[2].strip())
        elif msg.startswith("TEST switches in effect"):
            self.switches = parse_switches(msg)
        elif msg.startswith("GPU clock probe in 10 s"):
            p = PROBE.search(msg)
            if p:
                self.probes.append((t, int(p[1])))
        elif msg.startswith("Clock trace per second"):
            b = BACK.search(msg)
            w = WIN.search(msg)
            pkg = PKG.search(msg)
            mode = MODE.search(msg)
            ac = AC.search(msg)
            self.clock.append(dict(t=t, win=float(w[1]) if w else 10.0,
                                   ghz=None if not b or b[1] == "n/a" else float(b[1]),
                                   work=float(b[2]) if b else None, pkg_w=float(pkg[1]) if pkg else None,
                                   mode=mode[1] if mode else None, ac=ac[1] if ac else None))
        elif msg.startswith("Async translate in the last 10 s"):
            q = TRANSLATE.search(msg)
            if q:
                self.translate.append((t, int(q[1])))
        elif msg.startswith("Worst frame in the last 10 s"):
            w = WORST.search(msg)
            if w:
                self.worst.append((t, int(w[1])))
        elif msg.startswith("Shader precache:"):
            if not any(q in msg for q in QUIET_PRECACHE):
                self.precache.append((t, msg[:160]))
        elif msg.startswith("Precompiled pipelines") and PREP_WORK.search(msg):
            self.precache.append((t, msg[:160]))
        elif fn == "LogPowerMode" or msg.startswith("Windows power mode"):
            p = POWER_LINE.search(msg)
            self.power = dict(mode=p[1], ac=p[2] == "AC") if p else dict(mode=None, ac=None)
        elif "Host crash" in msg:
            self.crash.append(msg[:200])
        b = BOOT.search(msg) if self.program_id is None else None
        if b:
            self.program_id = b[1].upper()
        if "Shutdown requested" in msg and self.shutdown is None:
            self.shutdown = t
        if "Closing frontend after emulation cleanup" in msg:
            self.closed = t

    def latest_draws(self, after=None):
        if self.draws and (after is None or self.draws[-1][0] > after):
            return self.draws[-1]
        return None

    def setting(self, short):
        """A setting by its short name (force_max_clock) or full name (Renderer.force_max_clock)."""
        if short in self.settings:
            return self.settings[short]
        for key, value in self.settings.items():
            if key.rsplit(".", 1)[-1] == short:
                return value
        return None


def parse_switches(msg):
    body = msg.split(":", 1)[1] if ":" in msg else msg
    out = {}
    for item in body.split(","):
        m = SWITCH_ITEM.match(item.strip())
        if m:
            out[m[1]] = m[2]
    return out


def parse_file(path, expect_lines=(), opener=None):
    state = LogState(expect_lines)
    opener = opener or (lambda p: open(p, "rb"))
    with opener(path) as stream:
        state.feed(stream.read().decode("utf-8", errors="replace"))
    if state.partial:
        state.line(state.partial)
        state.partial = ""
    return state


class Tail:
    """Reads new bytes of a growing log, opening it for each read (never held across a launch).
    A UTF-8 character split between two reads (a path with an accented user name) is decoded
    whole on the next read."""

    def __init__(self, path, state, opener):
        self.path = path
        self.state = state
        self.opener = opener
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def poll(self):
        try:
            with self.opener(self.path) as stream:
                stream.seek(self.offset)
                data = stream.read()
        except OSError:
            return 0
        self.offset += len(data)
        if data:
            self.state.feed(self.decoder.decode(data))
        return len(data)


# ---------------------------------------------------------------------------------------------
# Finding this run's log


def digest(path, opener=None):
    opener = opener or (lambda p: open(p, "rb"))
    h = hashlib.sha256()
    try:
        with opener(path) as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                h.update(block)
    except FileNotFoundError:
        return None
    return h.hexdigest()


def log_paths(log_dir):
    return dict(log=os.path.join(log_dir, "eden_log.txt"), old=os.path.join(log_dir, "eden_log.txt.old.txt"),
                second=os.path.join(log_dir, "eden_log_2.txt"))


def identify_log(log_dir, launched, previous_digest, build=None, opener=None, stat=os.stat):
    """This run's log: written after the launch, rotated over the previous one, starting with
    the plan's build and an Ocarina boot. Raises LogError with the reason otherwise."""
    p = log_paths(log_dir)
    try:
        second = stat(p["second"])
        if second.st_mtime >= launched:
            raise LogError("Lichii wrote eden_log_2.txt: the log was held open elsewhere during the launch", final=True)
    except FileNotFoundError:
        pass
    try:
        info = stat(p["log"])
    except FileNotFoundError:
        raise LogError("no eden_log.txt")
    if info.st_mtime < launched:
        raise LogError("eden_log.txt is older than the launch")
    if previous_digest is not None and digest(p["old"], opener) != previous_digest:
        raise LogError("eden_log.txt.old.txt isn't the previous log: the log did not rotate", final=True)
    state = LogState()
    with (opener or (lambda q: open(q, "rb")))(p["log"]) as stream:
        state.feed(stream.read(256 * 1024).decode("utf-8", errors="replace"))
    if state.version is None or state.version_line > 5:
        raise LogError("the version line isn't among the first lines")
    if build and build not in state.version:
        raise LogError(f"the log is of build {state.version}, not {build}", final=True, fatal=True)
    return p["log"], state


# ---------------------------------------------------------------------------------------------
# Windows and validity


def in_window(items, s, e):
    return [x for x in items if s < x[0] <= e]


def covering(items, s, e, span=10.0):
    """10-s report lines whose span overlaps [s, e]."""
    return [x for x in items if x[0] > s and x[0] - span < e]


def arrival(state, draws_low=2000, seconds=5):
    """Log time at which the draws first stay at the fountain's level for `seconds` lines."""
    d = state.draws
    for i in range(len(d) - seconds + 1):
        if all(x[2] >= draws_low for x in d[i:i + seconds]):
            return d[i][0] - 1.0
    return None


def window_metrics(state, s, e):
    out = dict(seconds=round(e - s, 1))
    v = [ms for t, ms in state.frames if s < t <= e]
    if v:
        srt = sorted(v)
        out.update(frames=len(v), fps=1000.0 * len(v) / sum(v), frame_ms=sum(v) / len(v),
                   p99_ms=srt[min(len(v) - 1, int(0.99 * len(v)))])
    g = [b for _, b in in_window(state.gpu, s, e)]
    if g:
        out["gpu_busy_ms"] = st.median(g)
    p = [m for t, m in state.probes if s + 10 <= t <= e + 0.5]
    if p:
        out["probe_mhz"] = st.median(p)
    cl = [c for c in state.clock if c["t"] - c["win"] >= s - 0.5 and c["t"] <= e + 0.5]
    ghz = [c["ghz"] for c in cl if c["ghz"]]
    pkg = [c["pkg_w"] for c in cl if c["pkg_w"]]
    if ghz:
        out["back_ghz"] = st.mean(ghz)
    if pkg:
        out["pkg_w"] = st.mean(pkg)
    d = in_window(state.draws, s, e)
    if d:
        out["draws_pf"] = sum(x[1] * x[2] for x in d) / sum(x[1] for x in d)
        head = [x[2] for x in d if x[0] <= s + 10]
        tail = [x[2] for x in d if x[0] > e - 10]
        if head and tail:
            out["draws_drift_pct"] = 100.0 * (st.mean(tail) - st.mean(head)) / st.mean(head)
    out["pipelines_queued"] = sum(q for _, q in covering(state.translate, s, e))
    out["pipelines_built"] = sum(b for _, b in covering(state.worst, s, e))
    return out


def norm(value):
    return str(value).strip().lower()


def validate_run(state, plan, arm_name, window, settings_gate, switches, expect_lines=()):
    """Problems invalidate the run; flags are reported. `window` is (start, end) in log time."""
    problems = []
    flags = []
    gates = plan.get("gates", {})
    build = plan.get("build")
    if state.version is None:
        problems.append("no version line")
    elif build and build not in state.version:
        problems.append(f"build is {state.version}, not {build}")
    if state.program_id != "0400486024DEC000":
        problems.append(f"booted {state.program_id or 'nothing'}, not Ocarina of Time")
    if not state.settings:
        problems.append("no settings dump")
    for key, want in settings_gate.items():
        have = state.setting(key)
        if have is None or norm(have) != norm(want):
            problems.append(f"setting {key} is {have}, not {want}")
    if state.switches is None:
        problems.append("no TEST switches line")
    else:
        for name, want in switches.items():
            have = state.switches.get(name)
            if have is None or norm(have) != norm(want):
                problems.append(f"TEST switches line shows {name} {have}, not {want}")
    for pattern in expect_lines:
        if not state.expect.get(pattern):
            problems.append(f"no line matches {pattern!r}")
    want_mode = gates.get("power_mode", "best performance")
    modes = {c["mode"] for c in state.clock if c["mode"]}
    if state.power and state.power.get("mode"):
        modes.add(state.power["mode"])
    if want_mode and modes != {want_mode}:
        problems.append(f"power mode {sorted(modes) or 'not logged'}, not {want_mode}")
    if (state.power and state.power.get("ac") is False) or any(c["ac"] == "off" for c in state.clock):
        problems.append("on battery")
    if state.crash:
        problems.append("host crash: " + state.crash[0])
    if state.precache:
        problems.append("shader precache or preparation ran: " + state.precache[0][1])
    values = {}
    if window is None:
        problems.append("no measured window")
    else:
        values = window_metrics(state, *window)
        if values.get("pipelines_queued") or values.get("pipelines_built"):
            problems.append(f"shaders still compiling in the window ({values['pipelines_queued']} queued, "
                            f"{values['pipelines_built']} built on the GPU thread)")
        lo, hi = gates.get("draws", [2600, 3200])
        dpf = values.get("draws_pf")
        if dpf is None or not lo <= dpf <= hi:
            problems.append(f"draws a frame {dpf if dpf is None else round(dpf)} outside [{lo}, {hi}]")
        if abs(values.get("draws_drift_pct", 0.0)) > gates.get("draws_drift_pct", 6.0):
            flags.append(f"draws drifted {values['draws_drift_pct']:+.1f}% in the window")
        band = gates.get("probe_mhz", "sitting")
        mhz = values.get("probe_mhz")
        if isinstance(band, list):
            if mhz is None or not band[0] <= mhz <= band[1]:
                problems.append(f"GPU clock probe {mhz} MHz outside {band}")
        elif mhz is None:
            flags.append("no GPU clock probe line in the window")
        if values.get("back_ghz") is None or values.get("pkg_w") is None:
            flags.append("no Clock trace back GHz or package W in the window")
    return problems, flags, values


def sitting_checks(runs, gates):
    """Across a sitting's valid runs: each window's back GHz and package W within the band of the
    sitting's median, the probe within its band in "sitting" mode, and identical settings dumps.
    `runs` maps label -> dict(values=..., settings=...); returns label -> [flags]."""
    out = {label: [] for label in runs}
    pct = gates.get("cpu_band_pct", 1.5)
    checks = [("back_ghz", pct, "back GHz"), ("pkg_w", pct, "package W")]
    if gates.get("probe_mhz", "sitting") == "sitting":
        checks.append(("probe_mhz", gates.get("probe_band_pct", 3.0), "GPU clock probe"))
    for key, band, name in checks:
        vals = {lab: r["values"].get(key) for lab, r in runs.items() if r["values"].get(key) is not None}
        if len(vals) < 2:
            continue
        mid = st.median(vals.values())
        for lab, v in vals.items():
            if abs(v - mid) > band / 100.0 * mid:
                out[lab].append(f"{name} {v:.2f} is {100.0 * (v - mid) / mid:+.1f}% from the sitting's median "
                                f"{mid:.2f} (band ±{band}%)")
    labels = list(runs)
    if labels:
        ref = runs[labels[0]].get("settings", {})
        for lab in labels[1:]:
            other = runs[lab].get("settings", {})
            diff = sorted(k for k in set(ref) | set(other) if ref.get(k) != other.get(k))
            if diff:
                out[lab].append(f"settings differ from {labels[0]}: " + ", ".join(diff[:8]))
    return out
