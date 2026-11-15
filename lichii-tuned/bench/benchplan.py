"""Plans for Lichii Bench: the launcher parser, plan checks and the run schedule.

A plan names each arm as a base launcher plus an environment difference, the schedule (warm-up
launches, then balanced blocks), the windows, the gates and the decision rules. A plan without
rules, or whose arms differ in a trace variable, is refused before anything runs.
"""

import json
import os
import re

OOT = "0400486024DEC000"

# Variables the runner sets itself; a plan or launcher must not.
RUNNER_VARS = ("EDEN_NX2_DEBUG_SERVER", "EDEN_NX2_DEBUG_DIR", "LICHII_AB_ARM", "LICHII_AB_RUN")

# Names in r66's "TEST switches in effect" line: a change of one of these shows in the log.
SWITCH_LINE_NAMES = frozenset("""
EDEN_NX2_TEST_1216 EDEN_NX2_ZERO_TRIM EDEN_NX2_KSPIN EDEN_NX2_ARB_SPIN EDEN_NX2_NATIVE_FLOAT_ATOMICS
LICHII_TFB_BARRIER_LAZY LICHII_UPSTREAM_GLOBAL_ROUTING LICHII_UMWAIT_C02 UMWAIT EDEN_NX2_IDLE_SPIN_US
EDEN_NX2_CLOCK_TRACE EDEN_NX2_HEAP_TRACE EDEN_NX2_GPU_CLOCK_PROBE LICHII_FSR_EARLY_CONTEXT LICHII_FSR_SLIM
EDEN_NX2_PREALLOC_MIB EDEN_NX2_STAGING_RING_MIB EDEN_NX2_TEST_1571
""".split())

TRACE_WORDS = ("TRACE", "PROFILE", "STATS", "PROBE", "WATCH", "DUMP", "VERIFY", "CHECK", "LOG")

METRICS = frozenset({"fps", "frame_ms", "p99_ms", "low1_fps", "low01_fps", "gpu_busy_ms", "gpu_busy_mean_ms",
                     "back_work_ms", "back_cpu_ms", "front_cpu_ms", "watch_ms", "draws_pf", "back_ghz", "pkg_w",
                     "spike_free_frame_ms", "spikes"})

# Rule 6 of the plan: the conditions every run must show in its settings dump.
DEFAULT_SETTINGS = {"force_max_clock": "true", "frame_gen": "false", "nx2_frame_rate_limit": "120",
                    "fullscreen_mode": "Exclusive", "hdr_mode": "Off", "use_speed_limit": "true"}

DEFAULTS = {"kind": "fountain", "settle_s": 60, "measure_s": 100, "cooldown_s": 60, "warmup_measure_s": 30,
            "boot_timeout_s": 300, "thumb_threshold": 24, "max_extra_blocks": 2, "chunk_s": 25}


class PlanError(Exception):
    pass


def is_trace_var(name):
    return any(word in name for word in TRACE_WORDS)


# ---------------------------------------------------------------------------------------------
# Launchers

SET_PLAIN = re.compile(r'^set ([A-Za-z_][A-Za-z0-9_]*)=(.*)$', re.I)
SET_QUOTED = re.compile(r'^set "([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"$', re.I)
START = re.compile(r'^start ""(?: /wait)? "%~dp0([^"]+\.exe)" %\*$', re.I)


def parse_launcher(text, build_dir, name="launcher"):
    """The variables a launcher sets and the exe it starts. Only the forms our launchers use are
    accepted: `set A=1&& set B=2`, `set "A=..."`, %~dp0 and `start "" "%~dp0lichii.exe" %*`."""
    env = {}
    exe = None
    dp0 = build_dir.rstrip("\\/") + "\\"
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        low = line.lower()
        if not line or low == "@echo off" or low.startswith("rem ") or low == "rem" or line.startswith("::") \
                or low == "setlocal":
            continue
        start = START.match(line)
        if start:
            exe = start[1]
            continue
        quoted = SET_QUOTED.match(line)
        if quoted:
            env[quoted[1]] = quoted[2].replace("%~dp0", dp0)
            continue
        if low.startswith("set "):
            for part in line.split("&&"):
                part = part.lstrip()
                match = SET_QUOTED.match(part.rstrip()) or SET_PLAIN.match(part)
                if not match:
                    raise PlanError(f"{name} line {number}: can't read {part!r}")
                env[match[1]] = match[2].replace("%~dp0", dp0)
            continue
        raise PlanError(f"{name} line {number}: the bench reads only set and start lines: {line!r}")
    if exe is None:
        raise PlanError(f"{name}: no start line for lichii.exe")
    if "%" in "".join(env.values()):
        raise PlanError(f"{name}: a value uses a variable other than %~dp0")
    for var in RUNNER_VARS:
        if var in env:
            raise PlanError(f"{name} sets {var}; the bench sets it itself")
    return env, exe


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        return stream.read()


def arm_env(arm, build_dir, read=None):
    """The full environment an arm adds to the scrubbed one: its base launcher, then its env."""
    read = read or read_text
    base = arm.get("base")
    env, exe = ({}, "lichii.exe")
    if base:
        env, exe = parse_launcher(read(os.path.join(build_dir, base)), build_dir, base)
    env = dict(env)
    env.update({k: str(v) for k, v in arm.get("env", {}).items()})
    return env, exe


def scrubbed_environ(environ):
    """The inherited environment without any EDEN_* or LICHII_* variable (launchers have no
    setlocal, so a console that ran one keeps its variables)."""
    return {k: v for k, v in environ.items() if not k.upper().startswith(("EDEN_", "LICHII_"))}


# ---------------------------------------------------------------------------------------------
# Plans


def expected_switches(arm):
    """What the TEST switches line must show for an arm: its own `switches`, plus each env var
    of the line's names (1 -> on, 0 -> off, otherwise the value)."""
    out = {}
    for name, value in arm.get("env", {}).items():
        if name in SWITCH_LINE_NAMES:
            value = str(value)
            out[name] = {"1": "on", "0": "off"}.get(value, value)
    out.update({k: str(v) for k, v in arm.get("switches", {}).items()})
    return out


def load_plan(path_or_dict, build_dir=None, read=None):
    plan = path_or_dict
    if not isinstance(plan, dict):
        with open(path_or_dict, encoding="utf-8") as stream:
            plan = json.load(stream)
    plan = dict(DEFAULTS, **plan)
    check_plan(plan, build_dir, read)
    return plan


def check_plan(plan, build_dir=None, read=None):
    problems = []
    if plan.get("kind") != "fountain":
        problems.append(f"kind {plan.get('kind')!r}: this runner does fresh-launch fountain sittings only")
    arms = plan.get("arms") or {}
    if not arms:
        problems.append("no arms")
    for name, arm in arms.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            problems.append(f"arm name {name!r}: letters, digits, - _ . only")
        for var in arm.get("env", {}):
            if var in RUNNER_VARS:
                problems.append(f"arm {name} sets {var}; the bench sets it itself")
            elif is_trace_var(var):
                continue
            elif var not in SWITCH_LINE_NAMES and var not in arm.get("switches", {}) and not arm.get("expect_lines"):
                problems.append(f"arm {name}: {var} doesn't show in the TEST switches line; give the arm "
                                f"expect_lines that prove it, or wait for r67's environment line")
    if arms and not problems:
        traces = {}
        for name, arm in arms.items():
            if build_dir is None:
                env = {k: str(v) for k, v in arm.get("env", {}).items()}
            else:
                try:
                    env, _ = arm_env(arm, build_dir, read)
                except (OSError, PlanError) as error:
                    problems.append(f"arm {name}: {error}")
                    continue
            traces[name] = {k: v for k, v in env.items() if is_trace_var(k)}
        if traces and len({json.dumps(t, sort_keys=True) for t in traces.values()}) > 1:
            first = next(iter(traces))
            for name, t in traces.items():
                diff = sorted(set(t.items()) ^ set(traces[first].items()))
                if diff:
                    problems.append(f"arms {first} and {name} differ in trace variables: "
                                    + ", ".join(sorted({k for k, _ in diff})))
    rules = plan.get("rules")
    if not rules:
        problems.append("no decision rules: write them into the plan before the runs")
    for i, rule in enumerate(rules or []):
        where = f"rule {i + 1}"
        for key in ("item", "control"):
            if rule.get(key) not in arms:
                problems.append(f"{where}: {key} {rule.get(key)!r} is not an arm")
        if rule.get("metric") not in METRICS:
            problems.append(f"{where}: metric {rule.get('metric')!r} unknown")
        if rule.get("better") not in ("lower", "higher"):
            problems.append(f"{where}: better must be lower or higher")
        if not isinstance(rule.get("min_effect"), (int, float)) or rule["min_effect"] <= 0:
            problems.append(f"{where}: min_effect must be a number > 0")
        for guard in rule.get("guards", []):
            if guard.get("metric") not in METRICS or not isinstance(guard.get("max_worse"), (int, float)):
                problems.append(f"{where}: a guard needs a known metric and max_worse")
    try:
        slots = schedule(plan)
        measured = {s["arm"] for s in slots if not s["warmup"]}
        for rule in rules or []:
            for key in ("item", "control"):
                if rule.get(key) in arms and rule[key] not in measured:
                    problems.append(f"rule on {rule['item']}: arm {rule[key]} has no measured run")
    except PlanError as error:
        problems.append(str(error))
    gates = plan.get("gates", {})
    probe = gates.get("probe_mhz", "sitting")
    if probe != "sitting" and not (isinstance(probe, list) and len(probe) == 2 and probe[0] < probe[1]):
        problems.append('gates.probe_mhz must be "sitting" or [low, high]')
    draws = gates.get("draws", [2600, 3200])
    if not (isinstance(draws, list) and len(draws) == 2):
        problems.append("gates.draws must be [low, high]")
    for key in ("settle_s", "measure_s", "cooldown_s"):
        if not isinstance(plan.get(key), (int, float)) or plan[key] < 0:
            problems.append(f"{key} must be a number >= 0")
    if problems:
        raise PlanError("the plan is refused:\n  " + "\n  ".join(problems))


def balanced_blocks(a, b, count):
    """A B B A | B A A B ..., the starting arm alternating from block to block."""
    return [[a, b, b, a] if i % 2 == 0 else [b, a, a, b] for i in range(count)]


def schedule(plan):
    """Slots in order: warm-up launches, then the blocks' runs. Each slot knows its block."""
    arms = plan.get("arms", {})
    if "blocks" in plan:
        blocks = plan["blocks"]
    elif "design" in plan:
        d = plan["design"]
        if len(d.get("pair", [])) != 2:
            raise PlanError("design needs a pair of arms")
        blocks = balanced_blocks(d["pair"][0], d["pair"][1], int(d.get("blocks", 2)))
    else:
        raise PlanError("no blocks: give blocks or design")
    slots = []
    for arm in plan.get("warmup", []):
        if arm not in arms:
            raise PlanError(f"warm-up arm {arm!r} is not an arm")
        slots.append({"arm": arm, "block": None, "warmup": True})
    for index, block in enumerate(blocks):
        if not block:
            raise PlanError(f"block {index + 1} is empty")
        for arm in block:
            if arm not in arms:
                raise PlanError(f"block {index + 1}: {arm!r} is not an arm")
            slots.append({"arm": arm, "block": index, "warmup": False})
    for number, slot in enumerate(slots, 1):
        slot["slot"] = number
    return slots


def plan_blocks(plan):
    """The blocks as lists of arms, as schedule() reads them."""
    if "blocks" in plan:
        return [list(b) for b in plan["blocks"]]
    d = plan["design"]
    return balanced_blocks(d["pair"][0], d["pair"][1], int(d.get("blocks", 2)))


def estimate_minutes(plan, per_run_overhead_s=135):
    """~28 s boot, ~25 s menus, ~10 s loading, the settle, the window, ~10 s quit, the cooldown."""
    total = 0.0
    for slot in schedule(plan):
        measure = plan["warmup_measure_s"] if slot["warmup"] else plan["measure_s"]
        total += per_run_overhead_s - 60 + plan["settle_s"] + measure + plan["cooldown_s"]
    return total / 60.0


def settings_gate(plan):
    out = dict(DEFAULT_SETTINGS)
    out.update({k: str(v).lower() if isinstance(v, bool) else str(v)
                for k, v in plan.get("gates", {}).get("settings", {}).items()})
    return out
