#!/usr/bin/env python3
"""Lichii Bench analyzer: Lichii logs judged by a plan whose rules were written before the runs.

    python3 -I analyze.py <plan.json> --zip <session.zip>     a bench session zip
    python3 -I analyze.py <plan.json> --session <dir>         an unpacked session folder
    python3 -I analyze.py <plan.json> --logs <dir>            logs named in the plan's "runs"
    python3 -I analyze.py --zip <session.zip>                 the plan.json inside the zip
    add --json <out.json> to keep every number

A session holds runs/NN-<arm>/eden_log.txt, each with an optional run.json from the runner
(arm, warmup, block, slot, status, reason, flags, dropped, window_log, quit.killed, frame_times). A
status other than ok, a block the runner dropped, or eden_log_2.txt or eden_crash.dmp beside the
log invalidates the run.

Kinds:
- "fountain": one launch per run at a fixed spot. Each run's window starts at its arrival (the
  first second that starts 5 s of 30+ frames a second and 2000+ draws a frame; frame rate only
  when the log has no draws lines) plus settle_s, and lasts measure_s, cut to the shortest common
  stay (to 0.1 s) so every run covers the same span after arrival.
- "route": one launch per run over the same route with area loads (slow frames per load).
- "live" (r67 on): one launch with switches flipped live in ABBA BAAB units. The runner logs
      Live switch NAME: a -> b at present N           (the server; "(client gone, present N)" on a revert)
      Debug server mark: seg K start item=I arm=A|B unit=U (present N)
      Debug server mark: seg K end (present N)
  Each segment is measured from its start mark + margin_s (and at least the item's settle_s,
  default 5 s, after its switch's last flip) to its end mark - margin_s, cut to whole hitch periods.
  The runner flips first, then writes the start mark. Consecutive segments of a unit pair up.

The periodic hitches (the 5.027 s one locked to the FSR context, perf-trace's 4.157 s one) are
estimated per run from the per-present times. Frame times are reported with and without them.

Logs are data: they are only matched with regular expressions. Standard library only.
"""
import argparse
import bisect
import io
import json
import math
import os
import re
import statistics as st
import sys
import zipfile

LINE = re.compile(r'^\[\s*([0-9.]+)\] (\S+) <(\w+)> (\S+?):(\d+):([^:]+): (.*)$')
KV = re.compile(r'(\w+)=(-?[0-9.]+)')
GF = re.compile(r'gpu_frame_span_ms=(-?[0-9.]+) .*?gpu_busy_ms=([0-9.]+) .*?dropped_submissions=(\d+) .*?draws_timed=(\d+)')
DRAWS = re.compile(r'DKB draws through_frame=(\d+) frames=(\d+) attempts=(\d+)')
WATCH = re.compile(r'([0-9a-f]{16})=([0-9.]+)ms/(\d+)x')
PROBE = re.compile(r'GPU clock probe in 10 s: median (\d+) MHz')
CLOCK_ROLE = r'(?:: |\| ){} ghz=([0-9.]+|n/a) cpu_ms=([0-9.]+) poll_ms=([0-9.]+) work_ms=([0-9.]+)'
XLATE = re.compile(r'Async translate in the last 10 s: (\d+) queued')
WORST = re.compile(r'Worst frame in the last 10 s: .*?in it (\d+) pipelines built')
PRECACHE = re.compile(r'Shader precache: (?:finished|skipped),|Precompiled pipelines: [1-9]\d* (?:records of .* to prepare|'
                      r'of \d+ (?:done|prepared)|prepared \()')
PRECACHE_COUNT = re.compile(r'(?<!of )\b(\d+)(?: of \d+)? (?:cached graphics pipelines|compute programs|precompiled pipelines)')
MARK = re.compile(r'^Debug server mark: (.*) \(present (\d+)\)$')
# r67's lines (nx2_debug_tools.cpp): "... at present N", "(client gone, present N)", "(session end, present N)".
FLIP = re.compile(r'^Live switch (\S+): (\S+) -> (\S+) (?:at present (\d+)|\((client gone|session end)(?:, present (\d+))?\))')
SEG = re.compile(r'^seg (\d+) (start|end)\b(.*)$')
SLOW_MS, BASE_MS = 33.4, 16.7

# Rule 6's conditions, checked in the settings dump unless the plan overrides them (null skips one).
DEFAULT_SETTINGS = {'force_max_clock': True, 'frame_gen': False, 'nx2_frame_rate_limit': 120,
                    'fullscreen_mode': 'Exclusive', 'hdr_mode': 'Off', 'use_speed_limit': True}
# The periodic hitches: nominal period, search range, phase tolerance, and what makes a frame a
# candidate (over rel x the median of the 121 frames around it, or min_excess ms over it).
HITCHES = [dict(name='fsr5', period=5.027, lo=4.95, hi=5.10, tol=0.06, rel=1.5),
           dict(name='trace4', period=4.157, lo=4.10, hi=4.22, tol=0.03, min_excess=7.0)]
TRACE_VAR = re.compile(r'TRACE|PROFILE|PROBE|WATCH|_STATS')


# ---------------------------------------------------------------- parsing

def parse(lines, grep=()):
    """One pass over a log's lines. Every list holds (time, ...) in log order."""
    R = dict(frames=[], gpu=[], cpu=[], clock=[], probe=[], draws=[], watch=[], xlate=[], worst=[], settings={},
             version=None, switches=None, power=None, crash=[], precache=[], errors=0, warnings=0, marks={},
             dbg=[], flips=[], grep={p: 0 for p in grep})
    patterns = [(p, re.compile(p)) for p in grep]
    mk = R['marks']
    for raw in lines:
        m = LINE.match(raw.rstrip('\r\n'))
        if not m:
            continue
        t = float(m[1]); level = m[3]; fn = m[6]; msg = m[7]
        mk['last'] = t
        for p, rx in patterns:
            if rx.search(msg):
                R['grep'][p] += 1
        if level in ('Error', 'Critical'):
            R['errors'] += 1
        elif level == 'Warning':
            R['warnings'] += 1
        if msg.startswith('DKB frame_ms n='):
            vals = [float(x) for x in msg.split()[3:]]
            end = t; seq = []
            for v in reversed(vals):
                seq.append((end, v)); end -= v / 1000.0
            R['frames'].extend(reversed(seq))
            mk.setdefault('first_frames', t)
        elif msg.startswith('DKB gpu_frame '):
            g = GF.search(msg)
            if g and float(g[1]) > 0 and int(g[3]) == 0:
                R['gpu'].append((t, float(g[2]), int(g[4])))
        elif msg.startswith('DKB cpu through_frame'):
            d = dict((k, float(v)) for k, v in KV.findall(msg))
            if d.get('frames') and 'gpu_back_cpu_ms' in d:
                R['cpu'].append((t, d['frames'], d['gpu_back_cpu_ms'], d.get('gpu_command_cpu_ms', 0.0),
                                 d.get('window_wall_ms', 1000.0)))
        elif msg.startswith('DKB draws through_frame'):
            g = DRAWS.search(msg)
            if g:
                R['draws'].append((t, int(g[2]), int(g[3])))
        elif msg.startswith('DKB gpu_watch '):
            timed = re.search(r'timed=(\d+)', msg)
            if timed and int(timed[1]) > 0:
                R['watch'].append((t, {w[1]: (float(w[2]), int(w[3])) for w in WATCH.finditer(msg)}))
        elif msg.startswith('GPU clock probe in 10 s'):
            g = PROBE.search(msg)
            if g:
                R['probe'].append((t, int(g[1])))
        elif msg.startswith('Clock trace per second'):
            c = {'t': t}
            w = re.search(r'\(EDEN_NX2_CLOCK_TRACE, ([0-9.]+) s\)', msg)
            c['win'] = float(w[1]) if w else 10.0
            for role in ('back', 'front'):
                r = re.search(CLOCK_ROLE.format(role), msg)
                if r:
                    c[role] = dict(ghz=None if r[1] == 'n/a' else float(r[1]), work=float(r[4]))
            p = re.search(r'RAPL_Package0_PKG=([0-9.]+)W', msg)
            c['pkg_w'] = float(p[1]) if p else None
            pm = re.search(r'power_mode="([^"]*)"', msg)
            c['power_mode'] = pm[1] if pm else None
            ac = re.search(r'\bac=(\w+)', msg)
            c['ac'] = ac[1] if ac else None
            R['clock'].append(c)
        elif msg.startswith('Async translate in the last 10 s'):
            g = XLATE.search(msg)
            if g:
                R['xlate'].append((t, int(g[1])))
        elif msg.startswith('Worst frame in the last 10 s'):
            g = WORST.search(msg)
            if g:
                R['worst'].append((t, int(g[1])))
        elif fn == 'LogSettings':
            s = re.search(r'([\w.]+): (.*)$', msg)
            if s:
                R['settings'].setdefault(s[1], s[2].strip())
        elif msg.startswith('TEST switches in effect'):
            R['switches'] = msg
        elif msg.startswith('Debug server mark: '):
            g = MARK.match(msg)
            if g:
                R['dbg'].append((t, g[1], int(g[2])))
        elif msg.startswith('Live switch '):
            g = FLIP.match(msg)
            if g:
                R['flips'].append(dict(t=t, name=g[1], old=g[2], new=g[3], present=int(g[4] or g[6] or 0) or None,
                                       gone=g[5] == 'client gone', end=g[5] == 'session end'))
        if R['version'] is None and 'Lichii Version:' in msg:
            R['version'] = msg.split('Lichii Version:', 1)[1].strip()
        if msg.startswith('DKB frame present=1 ') or msg.startswith('DKB renderer '):
            mk.setdefault('first_present', t)
        if msg.startswith('Booting game:'):
            mk.setdefault('boot', t)
            R['program'] = msg.split(':', 1)[1].split('|')[0].strip()
        if msg.startswith('Shutdown requested'):
            mk.setdefault('shutdown', t)
        if msg.startswith('AMD FSR: output and context made'):
            mk.setdefault('fsr_context', t)
        if 'Host crash' in msg:
            R['crash'].append(msg[:200])
        if 'Shader precache:' in msg or 'Precompiled pipelines:' in msg:
            # "N cached graphics pipelines, N of M compute programs and N precompiled pipelines to build".
            builds = 'Shader precache:' in msg and ' to build' in msg and any(int(n) for n in PRECACHE_COUNT.findall(msg))
            if builds or PRECACHE.search(msg):
                R['precache'].append(msg[:160])
        if fn == 'LogPowerMode':
            g = re.search(r'Windows power mode "([^"]*)"', msg)
            R['power'] = g[1] if g else msg[:120]
            if re.search(r', on battery power', msg):
                R['battery'] = True
    mk.setdefault('first_present', mk.get('first_frames', 0.0))
    R['frame_ends'] = [f[0] for f in R['frames']]
    return R


def end_of(R):
    return R['marks'].get('shutdown', R['marks'].get('last', 0.0))


# ---------------------------------------------------------------- windows and metrics

def frame_range(R, s, e):
    """Indices of the frames that ended in (s, e]."""
    return bisect.bisect_right(R['frame_ends'], s), bisect.bisect_right(R['frame_ends'], e)


def frames_in(R, s, e):
    lo, hi = frame_range(R, s, e)
    return [ms for _, ms in R['frames'][lo:hi]]


def nframes(R, s, e):
    lo, hi = frame_range(R, s, e)
    return hi - lo


def draws_pf(R, s, e):
    d = [x for x in R['draws'] if s < x[0] <= e]
    n = sum(x[1] for x in d)
    return sum(x[2] for x in d) / n if n else None


def arrival(R, rule=None):
    """The first second of 5 s with 30+ frames each and 2000+ draws a frame. Without draws lines,
    the first 5 s with 30-80 frames each (the menus run far faster, the loads far slower)."""
    rule = rule or {}
    secs = rule.get('seconds', 5); lo = rule.get('min_fps', 30); hi = rule.get('max_fps', 80)
    need = rule.get('min_draws', 2000)
    s0 = int(R['marks'].get('first_present', 0))
    for s in range(s0, int(end_of(R))):
        n = [nframes(R, s + k, s + k + 1) for k in range(secs)]
        if R['draws']:
            if min(n) >= lo and (draws_pf(R, s, s + secs) or 0) >= need:
                return float(s), 'draws'
        elif lo <= min(n) and max(n) <= hi:
            return float(s), 'frames only'
    return None, None


def low(v, share):
    k = max(1, int(len(v) * share))
    return 1000.0 * k / sum(sorted(v)[-k:])


def frame_metrics(v, seconds):
    if not v:
        return {}
    srt = sorted(v)
    return dict(frames=len(v), fps=len(v) / seconds if seconds > 0 else None, frame_ms=sum(v) / len(v),
                p99_ms=srt[min(len(v) - 1, int(0.99 * len(v)))], max_ms=srt[-1], low1_fps=low(v, 0.01),
                low01_fps=low(v, 0.001), over33=sum(1 for x in v if x > SLOW_MS))


def overlaps(t, win, s, e):
    """A line at t that covers the win seconds before it overlaps [s, e]."""
    return t > s and t - win < e


def metrics(R, s, e, watch=None):
    """Steady-play numbers of [s, e]: frame times, GPU busy, the back thread, clocks, draws, kernels."""
    out = dict(window=[round(s, 2), round(e, 2)], seconds=round(e - s, 2))
    out.update(frame_metrics(frames_in(R, s, e), e - s))
    g = [b for t, b, timed in R['gpu'] if s < t <= e and timed == 0]
    if g:
        out.update(gpu_busy_ms=st.median(g), gpu_busy_mean_ms=st.mean(g), gpu_samples=len(g))
    # Per-second CPU lines wholly inside the window.
    c = [x for x in R['cpu'] if x[0] - x[4] / 1000.0 >= s - 0.05 and x[0] <= e]
    n = sum(x[1] for x in c)
    if n:
        out.update(back_cpu_ms=sum(x[2] for x in c) / n, front_cpu_ms=sum(x[3] for x in c) / n)
    # Clock trace lines cover the 10 s before them: only those wholly inside the window.
    work = fwork = frames = 0.0; ghz = []; pkg = []
    for x in R['clock']:
        if 'back' not in x or not (x['t'] - x['win'] >= s - 0.5 and x['t'] <= e + 0.5):
            continue
        k = nframes(R, x['t'] - x['win'], x['t'])
        if k:
            work += x['back']['work'] * x['win']; frames += k
            fwork += x.get('front', {}).get('work', 0.0) * x['win']
            if x['back']['ghz']:
                ghz.append(x['back']['ghz'])
            if x['pkg_w']:
                pkg.append(x['pkg_w'])
    if frames:
        out.update(back_work_ms=work / frames, front_work_ms=fwork / frames, clock_lines=len(ghz) or len(pkg))
    if ghz:
        out['back_ghz'] = st.mean(ghz)
    if pkg:
        out['pkg_w'] = st.mean(pkg)
    p = [m for t, m in R['probe'] if s < t <= e]
    if p:
        out['probe_mhz'] = st.median(p)
    w = [k for t, k in R['watch'] if s < t <= e]
    if w:
        names = watch or sorted({h for x in w for h in x})
        out['watch_ms'] = st.mean(sum(x.get(h, (0.0, 0))[0] for h in names) for x in w)
        out['watch'] = {}
        for h in names:
            ms = [x.get(h, (0.0, 0))[0] for x in w]; calls = sum(x.get(h, (0.0, 0))[1] for x in w)
            out['watch'][h] = dict(ms=st.mean(ms), us_call=1000.0 * sum(ms) / calls if calls else None)
    d = draws_pf(R, s, e)
    if d is not None:
        out['draws_pf'] = d
    out['xlate_queued'] = sum(q for t, q in R['xlate'] if overlaps(t, 10.0, s, e))
    out['pipelines_built'] = sum(q for t, q in R['worst'] if overlaps(t, 10.0, s, e))
    return out


# ---------------------------------------------------------------- periodic hitches

def excess(R, i):
    """Frame i's time and the median of the 121 frames around it."""
    cache = R.setdefault('_median', {})
    if i not in cache:
        fr = R['frames']
        cache[i] = st.median(v for _, v in fr[max(0, i - 60):i + 61])
    return R['frames'][i][1], cache[i]


def find_grid(cands, lo, hi, tol, step=0.0002):
    """The period in [lo, hi] and phase that put the most candidates (time, index) within tol of a
    grid point, refined by least squares on the cycle index."""
    if len(cands) < 4:
        return None
    times = [t for t, _ in cands]
    best = (0, None, None)
    P = lo
    while P <= hi + 1e-12:
        ph = sorted(t % P for t in times)
        ext = ph + [x + P for x in ph]
        n = len(ph); j = 0
        for i in range(n):
            j = max(j, i)
            while j + 1 < i + n and ext[j + 1] - ext[i] <= 2 * tol:
                j += 1
            if j - i + 1 > best[0]:
                best = (j - i + 1, P, (ext[i] + ext[j]) / 2.0)
        P += step
    _, P, t0 = best
    hits = []
    for _ in range(3):
        hits = [(t, i) for t, i in cands if abs((t - t0) / P - round((t - t0) / P)) * P < tol]
        if len(hits) < 3:
            return None
        k = [round((t - t0) / P) for t, _ in hits]
        mk = st.mean(k); mt = st.mean(t for t, _ in hits)
        den = sum((a - mk) ** 2 for a in k)
        if den == 0:
            break
        P = sum((a - mk) * (t - mt) for a, (t, _) in zip(k, hits)) / den
        t0 = mt - P * mk
    hits = [(t, i) for t, i in cands if abs((t - t0) / P - round((t - t0) / P)) * P < tol]
    return dict(period=P, t0=t0, hits={i for _, i in hits})


def estimate_spikes(R, s, e, hitches=None, chunk_s=200.0, min_share=0.5):
    """Each hitch's grid over [s, e], in chunks of about chunk_s (long live sessions). A grid counts
    only if at least min_share of the cycles it spans (and 4) hold a spike."""
    hitches = hitches or HITCHES
    parts = max(1, round((e - s) / chunk_s))
    grids = []
    for p in range(parts):
        a = s + (e - s) * p / parts; b = s + (e - s) * (p + 1) / parts
        i0, i1 = frame_range(R, a, b)
        taken = set()
        for h in hitches:
            cands = []
            for i in range(i0, i1):
                if i in taken:
                    continue
                v, m = excess(R, i)
                if v > h.get('rel', 1e9) * m or v - m > h.get('min_excess', 1e9):
                    cands.append((R['frames'][i][0], i))
            g = find_grid(cands, h['lo'], h['hi'], h['tol'])
            cycles = (b - a) / h['period']
            if g and len(g['hits']) >= max(4, min_share * cycles):
                g.update(name=h['name'], span=[a, b], cycles=cycles)
                grids.append(g)
                # Frames next to a hit belong to it, not to the next hitch.
                for i in g['hits']:
                    taken.update(range(i - 2, i + 3))
    return grids


def spike_metrics(R, grids, s, e, hitches=None, rel=1.5):
    """Spikes in [s, e] by hitch: count, grid points (slots), median excess; frame times without
    them; big frames on no grid; and a hitch with more spike frames than its periods allow plus one."""
    hitches = hitches or HITCHES
    i0, i1 = frame_range(R, s, e)
    out = {}; removed = set(); over = []
    for h in hitches:
        gs = [g for g in grids if g['name'] == h['name'] and g['span'][0] < e and g['span'][1] > s]
        hits = sorted(i for g in gs for i in g['hits'] if i0 <= i < i1)
        slots = 0
        for g in gs:
            a, b = max(s, g['span'][0]), min(e, g['span'][1])
            slots += math.floor((b - g['t0']) / g['period']) - math.floor((a - g['t0']) / g['period'])
        out[f"spk_{h['name']}_n"] = len(hits)
        out[f"spk_{h['name']}_slots"] = slots
        if gs:
            out[f"spk_{h['name']}_period"] = st.mean(g['period'] for g in gs)
        if hits:
            out[f"spk_{h['name']}_ms"] = st.median(excess(R, i)[0] - excess(R, i)[1] for i in hits)
        removed.update(hits)
        if gs and len(hits) > slots + 1:
            over.append(f"{len(hits)} {h['name']} spike frames, its {slots} periods allow {slots + 1}")
    big = [i for i in range(i0, i1) if excess(R, i)[0] > rel * excess(R, i)[1]]
    out['spikes_other'] = sum(1 for i in big if i not in removed)
    out['spikes_over'] = over
    v = [R['frames'][i][1] for i in range(i0, i1) if i not in removed]
    if v:
        out.update(frame_ms_ns=sum(v) / len(v), fps_ns=1000.0 * len(v) / sum(v), low1_ns=low(v, 0.01))
        out['spike_free_frame_ms'] = out['frame_ms_ns']  # the runner's name for it
    out['spikes'] = len(removed)
    return out


# ---------------------------------------------------------------- validity

def setting_value(R, name):
    if name in R['settings']:
        return R['settings'][name]
    for k, v in R['settings'].items():
        if k.endswith('.' + name):
            return v
    return None


def norm(v):
    return str(v).lower() if isinstance(v, bool) else str(v)


def switch_token(value):
    return {'1': 'on', '0': 'off'}.get(str(value), str(value))


def has_switch(line, name, token):
    return bool(line) and re.search(r'(?:: |, )' + re.escape(name) + ' ' + re.escape(token) + r'(?:,|\s|$)', line) is not None


def lists_switch(line, name):
    return bool(line) and re.search(r'(?:: |, )' + re.escape(name) + ' ', line) is not None


def arm_expectations(plan):
    """Per arm: switch tokens the TEST switches line must show and tokens it must not show. From
    the arm's explicit "switches", or from its env difference."""
    arms = plan.get('arms', {})
    must = {a: dict(spec.get('switches', {})) for a, spec in arms.items()}
    mustnt = {a: {} for a in arms}
    for a, spec in arms.items():
        for name, value in spec.get('env', {}).items():
            must[a].setdefault(name, switch_token(value))
            for other in arms:
                if other != a and name not in arms[other].get('env', {}) and name not in must[other]:
                    mustnt[other][name] = switch_token(value)
    return must, mustnt


def settings_gates(plan):
    gates = dict(DEFAULT_SETTINGS) if plan.get('default_gates', plan.get('kind') != 'route') else {}
    gates.update(plan.get('settings', {}))
    gates.update(plan.get('gates', {}).get('settings', {}))
    return {k: v for k, v in gates.items() if v is not None}


def run_problems(R, arm, plan, must, mustnt, warmup=False, flags=None):
    """What must hold for a run to count: the build, the game, the arm's switches, no crash or
    precache (a warm-up may precache), the settings, the power mode and AC power. Checks that
    can't be made go to flags."""
    bad = []
    flags = [] if flags is None else flags
    if plan.get('build') and plan['build'] not in (R['version'] or ''):
        bad.append(f"build is not {plan['build']}: {R['version']}")
    want_game = plan.get('program', '0400486024DEC000')
    if want_game and R.get('program') != want_game:
        bad.append(f"game is {R.get('program') or 'not booted'}")
    explicit = plan.get('arms', {}).get(arm, {}).get('switches', {})
    for name, token in must.get(arm, {}).items():
        if has_switch(R['switches'], name, token):
            continue
        if name not in explicit and not lists_switch(R['switches'], name):
            # The TEST switches line lists only some switches: an env change it doesn't list can't be checked here.
            flags.append(f'{name} is not on the TEST switches line (not checked)')
        else:
            bad.append(f'TEST switches line lacks "{name} {token}"')
    for name, token in mustnt.get(arm, {}).items():
        if has_switch(R['switches'], name, token):
            bad.append(f'TEST switches line shows "{name} {token}" (another arm\'s change)')
    for pattern in plan.get('arms', {}).get(arm, {}).get('expect_lines', []):
        if not R['grep'].get(pattern):
            bad.append(f'no line matches {pattern!r}')
    if R['crash']:
        bad.append('host crash: ' + R['crash'][0])
    if R['precache']:
        (flags if warmup else bad).append('precache or preparation ran: ' + R['precache'][0])
    for name, value in settings_gates(plan).items():
        got = setting_value(R, name)
        if got is None or got.lower() != norm(value).lower():
            bad.append(f'setting {name} is {got}, not {norm(value)}')
    want = plan.get('gates', {}).get('power_mode', plan.get('power_mode', 'best performance'))
    modes = {c['power_mode'] for c in R['clock'] if c.get('power_mode')}
    if R['power']:
        modes.add(R['power'])
    if want and modes != {want}:
        bad.append(f'power modes {sorted(modes) or "not logged"}, not {want!r}')
    if R.get('battery') or any(c.get('ac') not in (None, 'on') for c in R['clock']):
        bad.append('on battery')
    return bad


def band_checks(values, plan, labels):
    """The sitting's bands over the runs (or segments) in labels: the GPU clock probe, the back
    thread's GHz and package W (flags), the draws (problems). Returns {label: (problems, flags)}."""
    gates = plan.get('gates', {})
    out = {lab: ([], []) for lab in labels}
    probe = gates.get('probe_mhz', plan.get('expect', {}).get('probe_mhz'))
    if probe is not None:
        vals = [values[lab]['probe_mhz'] for lab in labels if values[lab].get('probe_mhz')]
        if probe == 'sitting' and vals:
            mid = st.median(vals); pct = gates.get('probe_band_pct', 3.0)
            probe = [mid * (1 - pct / 100.0), mid * (1 + pct / 100.0)]
        if probe != 'sitting':
            for lab in labels:
                v = values[lab].get('probe_mhz')
                if v is None or not probe[0] <= v <= probe[1]:
                    out[lab][0].append(f'GPU clock probe {fmt(v)} MHz outside [{probe[0]:.0f}, {probe[1]:.0f}]')
    draws = gates.get('draws', plan.get('expect', {}).get('draws_pf'))
    if draws:
        for lab in labels:
            v = values[lab].get('draws_pf')
            if v is None or not draws[0] <= v <= draws[1]:
                out[lab][0].append(f'draws a frame {fmt(v)} outside [{draws[0]}, {draws[1]}]')
    pct = gates.get('cpu_band_pct', 1.5)
    for key, what in (('back_ghz', 'back thread GHz'), ('pkg_w', 'package W')):
        vals = [values[lab][key] for lab in labels if values[lab].get(key)]
        if len(vals) < 2:
            continue
        mid = st.median(vals)
        for lab in labels:
            v = values[lab].get(key)
            if v and abs(v - mid) > pct / 100.0 * mid:
                out[lab][1].append(f'{what} {v:.2f} is {100.0 * (v - mid) / mid:+.1f}% from the median {mid:.2f}')
    for lab in labels:
        v = values[lab]
        if v.get('xlate_queued') or v.get('pipelines_built'):
            out[lab][0].append(f"pipeline work in the window ({v.get('xlate_queued', 0)} translations queued, "
                               f"{v.get('pipelines_built', 0)} pipelines built)")
        out[lab][1].extend(v.get('spikes_over', []))
    return out


def plan_problems(plan):
    """Rules 4 and 5: a plan needs rules on its own arms, and its arms may differ only in what they test."""
    bad = []
    rules = plan.get('rules') or []
    if not rules:
        bad.append('the plan has no decision rules')
    kind = plan.get('kind')
    names = set(plan.get('items', {})) if kind == 'live' else set(plan.get('arms', {}))
    for r in rules:
        for key in ('item', 'control'):
            if r.get(key) and r[key] not in names and not (kind == 'live' and key == 'control'):
                bad.append(f"rule names {key} {r[key]!r}, which is not in the plan")
        for k in ('metric',) + (('min_effect',) if r.get('kind', 'decide') == 'decide' else ()):
            if k not in r:
                bad.append(f"rule for {r.get('item')} has no {k}")
    arms = plan.get('arms', {})
    bases = {spec.get('base') for spec in arms.values() if 'base' in spec}
    if len(bases) > 1:
        bad.append(f'arms differ in the base launcher: {sorted(bases)}')
    env_names = {n for spec in arms.values() for n in spec.get('env', {})}
    for n in sorted(env_names):
        vals = {json.dumps(spec.get('env', {}).get(n)) for spec in arms.values()}
        if TRACE_VAR.search(n) and len(vals) > 1:
            bad.append(f'arms differ in the trace variable {n}')
    for name, item in plan.get('items', {}).items():
        if item.get('power') and item.get('segment_s', 30) < 90:
            bad.append(f'{name} changes package power: segments must be 90 s or more')
    return bad


# ---------------------------------------------------------------- inputs

class Source:
    """Runs of a session zip, a session folder or a folder of named logs."""

    def __init__(self, zip_path=None, folder=None, logs=None):
        self.zip = zipfile.ZipFile(zip_path) if zip_path else None
        self.folder = folder; self.logs = logs
        if self.zip:
            self.names = [n for n in self.zip.namelist() if not n.endswith('/')]
        elif folder:
            self.names = []
            for root, _, files in os.walk(folder):
                for f in files:
                    self.names.append(os.path.relpath(os.path.join(root, f), folder).replace(os.sep, '/'))
        else:
            self.names = sorted(os.listdir(logs)) if logs and os.path.isdir(logs) else []

    def lines(self, name):
        if self.zip:
            return io.TextIOWrapper(self.zip.open(name), encoding='utf-8', errors='replace')
        return open(os.path.join(self.folder or self.logs, name), encoding='utf-8', errors='replace')

    def json(self, name):
        with self.lines(name) as f:
            return json.load(f)

    def plan(self):
        cands = sorted((n for n in self.names if n.split('/')[-1] == 'plan.json'), key=lambda n: n.count('/'))
        return self.json(cands[0]) if cands else None

    def runs(self):
        """runs/NN-<label>/eden_log.txt with their run.json and the files beside them."""
        out = []
        for n in self.names:
            m = re.search(r'(?:^|/)runs/(\d+)-([^/]+)/eden_log\.txt$', n)
            if not m:
                continue
            d = n[:-len('eden_log.txt')]
            beside = {x[len(d):] for x in self.names if x.startswith(d) and '/' not in x[len(d):]}
            info = self.json(d + 'run.json') if 'run.json' in beside else {}
            out.append(dict(nn=int(m[1]), label=f'{m[1]}-{m[2]}', dir_arm=m[2], log=n, info=info, beside=beside))
        return sorted(out, key=lambda r: r['nn'])


def plan_blocks(plan):
    """The plan's blocks, or those its "design" makes: A B B A | B A A B, alternating the start."""
    if plan.get('blocks'):
        return [list(b) for b in plan['blocks']]
    d = plan.get('design') or {}
    if len(d.get('pair', [])) == 2:
        a, b = d['pair']
        return [[a, b, b, a] if i % 2 == 0 else [b, a, a, b] for i in range(int(d.get('blocks', 2)))]
    return []


def bench_runs(plan, src):
    """The runs with their arm, role (W or measured), block and slot."""
    runs = src.runs()
    arms = plan.get('arms', {})
    for r in runs:
        info = r['info']
        arm = info.get('arm') or r['dir_arm']
        if arm not in arms and re.match(r'^W[-:]', arm):
            arm = arm[2:]; info.setdefault('warmup', True)
        r['arm'] = arm
        r['warmup'] = bool(info.get('warmup') or info.get('role') == 'W')
        r['block'] = info.get('block'); r['slot'] = info.get('slot')
    notes = []
    if not any('warmup' in r['info'] or 'role' in r['info'] for r in runs):
        # No runner roles: the plan's warm-up launches come first, in its order.
        w = plan.get('warmup', [])
        if [r['arm'] for r in runs[:len(w)]] == list(w):
            for r in runs[:len(w)]:
                r['warmup'] = True
        elif w:
            notes.append('the first runs are not the plan\'s warm-up launches; none is treated as one')
    if plan_blocks(plan) and not any(r['block'] is not None for r in runs):
        measured = [r for r in runs if not r['warmup']]
        k = 0; ok = True
        for b, block in enumerate(plan_blocks(plan)):
            for s, arm in enumerate(block):
                if k < len(measured) and measured[k]['arm'] == arm:
                    measured[k]['block'], measured[k]['slot'] = b, s; k += 1
                elif k < len(measured):
                    ok = False
        if not ok or k < len(measured):
            notes.append('the runs do not follow the plan\'s blocks (retries need run.json block and slot); '
                         'comparing each item with its neighbouring control runs')
            for r in measured:
                r['block'] = r['slot'] = None
    return runs, notes


def manual_runs(plan, src):
    """Runs listed in the plan ({file, arm, label}) or "auto": <nn>-<arm>.txt files in name order."""
    spec = plan.get('runs', [])
    if spec == 'auto':
        spec = []
        for name in src.names:
            m = re.match(r'^(\d+)-(.+?)\.txt$', name)
            if m:
                spec.append(dict(file=name, arm=m[2], label=f'{m[1]}-{m[2]}'))
    out = []
    for i, r in enumerate(spec):
        out.append(dict(nn=i + 1, label=r.get('label', f"{r['arm']}#{i + 1}"), arm=r['arm'], log=r['file'], info={},
                        beside=set(), warmup=bool(r.get('warmup')), block=r.get('block'), slot=r.get('slot')))
    return out


# ---------------------------------------------------------------- decisions

def tval(n):
    table = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18,
             15: 2.13, 20: 2.09, 30: 2.04, 60: 2.00, 120: 1.98}
    for k in sorted(table, reverse=True):
        if n >= k:
            return table[k]
    return 12.71


def delta(rule, item, control):
    d = item - control
    return 100.0 * d / control if rule.get('relative') else d


def gain(rule, item, control):
    """The item's improvement over the control (positive = better), in the metric's units or %."""
    d = delta(rule, item, control)
    return -d if rule.get('better', 'lower') == 'lower' else d


def guard_checks(rule, item_vals, control_vals):
    """Each guard: how much worse the item is on its metric, and whether that trips it. A guard
    without data is noted in notes as "no data" and blocks ADOPT, but trips nothing."""
    notes = []; tripped = []
    for guard in rule.get('guards', []):
        gi = [v.get(guard['metric']) for v in item_vals]; gc = [v.get(guard['metric']) for v in control_vals]
        if not gi or not gc or None in gi + gc:
            notes.append(f"guard {guard['metric']}: no data")
            continue
        worse = -gain(guard, st.mean(gi), st.mean(gc))
        unit = '%' if guard.get('relative') else ''
        notes.append(f"{guard['metric']} worse by {worse:+.3f}{unit} (at most {guard['max_worse']}{unit})")
        if worse > guard['max_worse']:
            tripped.append(f"{guard['metric']} worse by {worse:.3f}{unit} > {guard['max_worse']}{unit}")
    return notes, tripped


def decide_launch(rule, item_runs, control_runs, values, blocks=None):
    """Fresh launches (the owner's r66 rule). ADOPT: the minimum gain, a gain over the control
    spread, every item run better than every control run, no guard tripped. DROP: no gain or a
    guard tripped. Otherwise INCONCLUSIVE, with the runs per arm needed."""
    metric = rule['metric']
    if not item_runs:
        return dict(verdict='NO RUNS')
    if not control_runs or any(metric not in values[r] for r in item_runs + control_runs):
        return dict(verdict='NO DATA', item_runs=item_runs, control_runs=control_runs)
    iv = [values[r][metric] for r in item_runs]; cv = [values[r][metric] for r in control_runs]
    g = gain(rule, st.mean(iv), st.mean(cv))
    spread = abs(delta(rule, max(cv), min(cv))) if len(cv) > 1 else float('inf')
    better = all(gain(rule, a, b) > 0 for a in iv for b in cv)
    notes, tripped = guard_checks(rule, [values[r] for r in item_runs], [values[r] for r in control_runs])
    blind = any(n.endswith(': no data') for n in notes)
    adopt = g >= rule['min_effect'] and g > spread and better and not tripped and not blind
    drop = g <= 0 or bool(tripped)
    sd = st.stdev(cv) if len(cv) > 1 else None
    if sd is not None and rule.get('relative'):
        sd = 100.0 * sd / st.mean(cv)
    need = math.ceil(2 * ((1.96 + 0.84) * sd / g) ** 2) if sd and g > 0 else None
    out = dict(verdict='ADOPT' if adopt else 'DROP' if drop else 'INCONCLUSIVE', item_runs=item_runs,
               control_runs=control_runs, item=iv, control=cv, item_mean=st.mean(iv), control_mean=st.mean(cv),
               gain=g, control_spread=spread, all_item_runs_better=better, guards_tripped=tripped, notes=notes,
               runs_per_arm_needed=need)
    if blocks:
        out['per_block'] = blocks
    return out


def pair_stats(d):
    n = len(d)
    mean = st.mean(d); sd = st.stdev(d) if n > 1 else 0.0
    t = tval(n - 1); half = t * sd / math.sqrt(n) if n > 1 else float('inf')
    return n, mean, sd, t, half


def decide_live(rule, pairs, planned_pairs=None, null_half=None, units_run=None):
    """Live flips. ADOPT: the mean paired gain over the minimum, a 95% CI that excludes zero, 3/4 of
    the pairs agreeing, a gain over the null item's CI half-width, no guard tripped, every planned
    unit valid (or the plan's early stop). DROP: the CI excludes the minimum effect, or a guard."""
    metric = rule['metric']
    ok = [p for p in pairs if metric in p['item'] and metric in p['control']]
    kind = rule.get('kind', 'decide')
    if len(ok) < 2:
        return dict(verdict='NO DATA', pairs=len(ok))
    if kind in ('recover', 'aa'):
        d = [delta(rule, p['item'][metric], p['control'][metric]) for p in ok]
        n, mean, sd, t, half = pair_stats(d)
        out = dict(pairs=n, delta=mean, ci95=[mean - half, mean + half], sd=sd, half_width=half,
                   per_pair=[round(x, 4) for x in d])
        if kind == 'recover':
            out['verdict'] = 'RECOVERED' if mean - half <= rule['expect'] <= mean + half else 'MISSED'
        else:
            out['verdict'] = 'PASS' if -rule['max_ci'] <= mean - half and mean + half <= rule['max_ci'] else 'FAIL'
        return out
    d = [gain(rule, p['item'][metric], p['control'][metric]) for p in ok]
    n, mean, sd, t, half = pair_stats(d)
    agree = sum(1 for x in d if x > 0)
    notes, tripped = [], []
    for guard in rule.get('guards', []):
        gd = [-gain(guard, p['item'][guard['metric']], p['control'][guard['metric']]) for p in ok
              if guard['metric'] in p['item'] and guard['metric'] in p['control']]
        if not gd:
            notes.append(f"guard {guard['metric']}: no data"); continue
        notes.append(f"{guard['metric']} worse by {st.mean(gd):+.3f} (at most {guard['max_worse']})")
        if st.mean(gd) > guard['max_worse']:
            tripped.append(f"{guard['metric']} worse by {st.mean(gd):.3f} > {guard['max_worse']}")
    complete = planned_pairs is None or n >= planned_pairs
    # The early stop is the first half of the units, run and valid, and no more units run.
    early = (rule.get('early_stop') and planned_pairs and n == planned_pairs // 2 and agree == n
             and mean >= 3 * rule['min_effect'] and (units_run is None or 4 * units_run <= planned_pairs // 2))
    over_null = null_half is None or mean > null_half
    if null_half is None and rule.get('null'):
        over_null = False; notes.append(f"null item {rule['null']} has no pairs")
    blind = any(x.startswith('guard ') and x.endswith(': no data') for x in notes)
    adopt = (mean >= rule['min_effect'] and mean - half > 0 and agree >= math.ceil(rule.get('agree', 0.75) * n)
             and over_null and not tripped and not blind and (complete or early))
    drop = bool(tripped) or mean + half < rule['min_effect']
    if not complete:
        notes.append(f'{n} of {planned_pairs} planned pairs' + (' (early stop)' if early else ''))
    need = math.ceil(((t + 0.84) * sd / rule['min_effect']) ** 2) if rule['min_effect'] > 0 else None
    return dict(verdict='ADOPT' if adopt else 'DROP' if drop else 'INCONCLUSIVE', pairs=n, gain=mean,
                ci95=[mean - half, mean + half], sd=sd, pairs_better=agree, per_pair=[round(x, 4) for x in d],
                null_half_width=null_half, guards_tripped=tripped, notes=notes, pairs_needed_for_min_effect=need)


# ---------------------------------------------------------------- kinds

def window_spec(plan):
    win = plan.get('window', {})
    start = win.get('start_s', plan.get('settle_s', 60.0))
    length = win.get('length_s', plan.get('measure_s'))
    return start, length


def analyze_fountain(plan, runs, src, out):
    arms = plan.get('arms', {})
    must, mustnt = arm_expectations(plan)
    grep = sorted({p for spec in arms.values() for p in spec.get('expect_lines', [])})
    parsed = {}; res = out['runs']
    for r in runs:
        lab = r['label']
        with src.lines(r['log']) as f:
            R = parsed[lab] = parse(f, grep)
        flags = []
        problems = (run_problems(R, r['arm'], plan, must, mustnt, r['warmup'], flags) if r['arm'] in arms
                    else [f"arm {r['arm']} is not in the plan"])
        if 'eden_log_2.txt' in r['beside']:
            problems.append('eden_log_2.txt was written (another process held the log)')
        if 'eden_crash.dmp' in r['beside']:
            problems.append('eden_crash.dmp beside the log')
        info = r['info']
        given = info.get('flags', [])
        if info.get('invalid') or info.get('valid') is False or info.get('status', 'ok') != 'ok':
            problems.append(f"the runner's status is {info.get('status', 'invalid')}: " + '; '.join(
                [info['reason']] if info.get('reason') else given))
        else:
            flags += [f'runner: {x}' for x in given]
        if info.get('dropped'):
            problems.append('the runner dropped its block (a slot failed twice)')
        if (info.get('killed') or (info.get('quit') or {}).get('killed')) and not any('killed' in x for x in given):
            flags.append('killed at quit')
        if any(ft.get('truncated') for ft in info.get('frame_times', []) if isinstance(ft, dict)) \
                and not any('truncated' in x for x in given):
            flags.append('a frame_times call was truncated')
        res[lab] = dict(file=r['log'], arm=r['arm'], warmup=r['warmup'], block=r['block'], slot=r['slot'],
                        version=R['version'], problems=problems, flags=flags)
    start, length = window_spec(plan)
    arr = {}
    for lab, R in parsed.items():
        a, how = arrival(R, plan.get('arrival'))
        arr[lab] = a
        if a is None:
            res[lab]['problems'].append('no arrival found')
        elif how != 'draws':
            res[lab]['flags'].append('arrival from the frame rate only (no draws lines)')
    # The shortest stay of the measured runs that can count sets every window's length.
    stays = {lab: end_of(parsed[lab]) - 3.0 - (arr[lab] + start) for lab in parsed
             if arr[lab] is not None and not res[lab]['warmup'] and not res[lab]['problems']}
    want = length or (min(stays.values()) if stays else 0.0)
    for lab, s in stays.items():
        if s < 0.5 * want:
            res[lab]['problems'].append(f'stay too short ({s:.1f} s of {want:.1f})')
    common = min([s for s in stays.values() if s >= 0.5 * want] or [want])
    length = round(min(want, common), 1)
    out['window'] = dict(start_s=start, length_s=length, planned_s=want)
    values = {}
    for lab, R in parsed.items():
        if arr[lab] is None:
            values[lab] = {}; continue
        s = arr[lab] + start; e = s + length
        v = metrics(R, s, e, plan.get('watch'))
        grids = estimate_spikes(R, arr[lab] + 2.0, end_of(R) - 1.0, plan.get('hitches'))
        v.update(spike_metrics(R, grids, s, e, plan.get('hitches')))
        v['arrival_s'] = arr[lab]
        values[lab] = v
        res[lab]['values'] = v
        uw = res[lab].get('runner_window') or r_window(runs, lab)
        tol = plan.get('runner_window_tolerance_s', 5.0)
        if uw and (s < uw[0] - tol or e > uw[1] + tol):
            res[lab]['flags'].append(f'window {s:.1f}-{e:.1f} is outside the runner\'s {uw[0]:.1f}-{uw[1]:.1f}')
    measured = [lab for lab in values if not res[lab]['warmup'] and values[lab]]
    for lab, (p, f) in band_checks(values, plan, measured).items():
        res[lab]['problems'] += p; res[lab]['flags'] += f
    if measured:
        ref = parsed[measured[0]]['settings']
        for lab in measured[1:]:
            other = parsed[lab]['settings']
            diff = sorted(k for k in set(ref) | set(other) if ref.get(k) != other.get(k))
            if diff:
                res[lab]['flags'].append(f'settings differ from {measured[0]}: ' + ', '.join(diff[:8]))
    return values


def r_window(runs, lab):
    r = next(x for x in runs if x['label'] == lab)
    w = r['info'].get('window_log') or r['info'].get('window_uptime')
    return w if isinstance(w, list) and len(w) == 2 else None


def fountain_verdicts(plan, runs, values, out):
    """Blocks with a slot that has no valid run are dropped whole (rule 1); then each rule compares
    the item's runs with the control runs of the blocks that hold them, or, without blocks, with
    the nearest control run before and after."""
    res = out['runs']
    valid = {lab for lab, r in res.items() if not r['problems'] and not r['warmup']}
    if plan.get('strict_flags'):
        valid = {lab for lab in valid if not res[lab]['flags']}
    measured = [r for r in runs if not r['warmup']]
    use_blocks = any(r['block'] is not None for r in measured)
    arm_of = {r['label']: r['arm'] for r in runs}
    planned_blocks = plan_blocks(plan)
    shapes = [sorted(b) for b in planned_blocks]
    kept = {}
    if use_blocks:
        by_block = {}
        for r in measured:
            by_block.setdefault(r['block'], {}).setdefault(r['slot'], []).append(r['label'])
        for b, slots in by_block.items():
            chosen = {}
            for s, labs in slots.items():
                ok = [lab for lab in labs if lab in valid]
                if ok:
                    chosen[s] = ok[-1]
            # Blocks the runner appended copy a dropped block's order, so they match some planned block.
            want = planned_blocks[b] if isinstance(b, int) and 0 <= b < len(planned_blocks) else None
            ran = sorted(arm_of[x] for x in chosen.values())
            if len(chosen) != len(slots):
                out['notes'].append(f'block {b} dropped: slot(s) {sorted(set(slots) - set(chosen))} have no valid run')
            elif (want is not None and ran != sorted(want)) or (want is None and shapes and ran not in shapes):
                out['notes'].append(f'block {b} dropped: it holds {" ".join(arm_of[x] for x in chosen.values())}, '
                                    f'not a whole planned block')
            else:
                kept[b] = list(chosen.values())
    for rule in plan.get('rules', []):
        item, control = rule['item'], rule['control']
        if use_blocks:
            blocks = [b for b, labs in kept.items() if any(arm_of[x] == item for x in labs)]
            its = [x for b in blocks for x in kept[b] if arm_of[x] == item]
            ctl = [x for b in blocks for x in kept[b] if arm_of[x] == control]
            per_block = {}
            for b in blocks:
                i_ = [values[x][rule['metric']] for x in kept[b] if arm_of[x] == item and rule['metric'] in values[x]]
                c_ = [values[x][rule['metric']] for x in kept[b] if arm_of[x] == control and rule['metric'] in values[x]]
                if i_ and c_:
                    per_block[str(b)] = delta(rule, st.mean(i_), st.mean(c_))
            planned = sum(1 for blk in plan_blocks(plan) if item in blk)
            v = decide_launch(rule, its, ctl, values, per_block)
            if planned and len(blocks) < planned:
                v.setdefault('notes', []).append(f'{len(blocks)} of {planned} planned blocks')
                # Rule 4: the number of blocks is fixed in advance; fewer is an early stop the plan must allow.
                if v['verdict'] == 'ADOPT' and len(blocks) < rule.get('min_blocks', planned):
                    v['verdict'] = 'INCONCLUSIVE'
                    v['notes'].append('ADOPT needs every planned block' + (f" (or {rule['min_blocks']})" if 'min_blocks' in rule else ''))
        else:
            order = [r['label'] for r in measured if r['label'] in valid]
            idx = [i for i, lab in enumerate(order) if arm_of[lab] == item]
            if not idx:
                v = dict(verdict='NO RUNS')
            else:
                before = [order[i] for i in range(idx[0] - 1, -1, -1) if arm_of[order[i]] == control][:1]
                after = [order[i] for i in range(idx[-1] + 1, len(order)) if arm_of[order[i]] == control][:1]
                v = decide_launch(rule, [order[i] for i in idx], before + after, values)
        out['verdicts'][rule_key(rule)] = v


def loads(R):
    """Area loads: clusters of frames over 150 ms with a fade (a second under 30 draws a frame) at
    most 6.5 s before them."""
    dr = [(t, a / max(1, n)) for t, n, a in R['draws']]
    first_play = next((t for t, d in dr if d > 200), None)
    if first_play is None:
        return []
    fade = [t for t, d in dr if d < 30]
    out = []
    for e, ms in R['frames']:
        if ms <= 150 or e <= first_play - 8 or e > end_of(R):
            continue
        s = e - ms / 1000.0
        if out and s - out[-1][1] < 6.0:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [c for i, c in enumerate(out) if i == 0 or any(c[0] - 6.5 <= t <= c[1] + 0.5 for t in fade)]


def route_metrics(R):
    L = loads(R)
    if not L:
        return dict(loads=0)
    start = L[0][0] - 3.0
    slow = [(e, ms) for e, ms in R['frames'] if start <= e <= end_of(R) and ms > SLOW_MS]
    out = dict(loads=len(L), excess_s=sum(ms - BASE_MS for _, ms in slow) / 1000.0, frames_over33=len(slow),
               over50=sum(1 for _, ms in slow if ms > 50), over100=sum(1 for _, ms in slow if ms > 100),
               worst_ms=max((ms for _, ms in slow), default=0.0))
    for i, (s, e) in enumerate(L):
        out[f'L{i + 1}_ms'] = sum(ms - BASE_MS for t, ms in slow if s - 0.05 <= t <= e + 3.0)
    last_e = L[-1][1]
    out['excess_wo_last_tail_s'] = out['excess_s'] - sum(ms - BASE_MS for t, ms in slow if last_e + 3.0 < t <= last_e + 25.0) / 1000.0
    v = metrics(R, start, end_of(R))
    out.update({k: v[k] for k in ('probe_mhz', 'pkg_w') if k in v})

    def steady(t):
        return start < t < end_of(R) - 3.0 and not any(s - 3.0 <= t <= e + 25.0 for s, e in L)
    cpu = [x for x in R['cpu'] if steady(x[0])]
    draws = sum(a for t, n, a in R['draws'] if steady(t))
    if cpu and draws:
        out['steady_back_us_per_draw'] = 1000.0 * sum(x[2] for x in cpu) / draws
        out['steady_back_cpu_ms'] = sum(x[2] for x in cpu) / sum(x[1] for x in cpu)
    return out


def analyze_route(plan, runs, src, out):
    must, mustnt = arm_expectations(plan)
    values = {}
    for r in runs:
        with src.lines(r['log']) as f:
            R = parse(f)
        flags = []
        out['runs'][r['label']] = dict(file=r['log'], arm=r['arm'], warmup=r['warmup'], block=r['block'], slot=r['slot'],
                                       version=R['version'], flags=flags,
                                       problems=run_problems(R, r['arm'], plan, must, mustnt, r['warmup'], flags))
        values[r['label']] = out['runs'][r['label']]['values'] = route_metrics(R)
    return values


def segments(R, plan):
    """Segments from the runner's marks, with each switch's value at their start and what makes one
    invalid: a flip or a revert inside it, the wrong value for its arm, a missing end."""
    items = plan.get('items', {})
    starts = {}; segs = []
    for t, text, present in R['dbg']:
        m = SEG.match(text)
        if not m:
            continue
        k = int(m[1]); tok = dict(re.findall(r'(\w+)=(\S+)', m[3]))
        if m[2] == 'start':
            starts[k] = dict(k=k, start=t, present=present, item=tok.get('item'), arm=tok.get('arm'),
                             unit=tok.get('unit'), problems=[])
        elif k in starts:
            seg = starts.pop(k); seg['end'] = t; segs.append(seg)
    for k, seg in starts.items():
        seg['end'] = None; seg['problems'].append('no end mark'); segs.append(seg)
    segs.sort(key=lambda x: x['start'])

    def value_at(name, t):
        before = [f for f in R['flips'] if f['name'] == name and f['t'] <= t]
        if before:
            return before[-1]['new']
        after = [f for f in R['flips'] if f['name'] == name]
        return after[0]['old'] if after else None
    for seg in segs:
        item = items.get(seg['item'])
        if item is None:
            seg['problems'].append(f"item {seg['item']} is not in the plan"); continue
        if seg['arm'] not in ('A', 'B'):
            seg['problems'].append(f"arm {seg['arm']} is not A or B"); continue
        want = str(item['b'] if seg['arm'] == 'B' else item['a'])
        got = value_at(item['switch'], seg['start'])
        if got is not None and got != want:
            seg['problems'].append(f"{item['switch']} is {got}, not {want}")
        elif got is None and want != str(item['a']):
            seg['problems'].append(f"no Live switch line set {item['switch']} to {want}")
        if seg['end'] is not None:
            for f in R['flips']:
                if seg['start'] <= f['t'] <= seg['end']:
                    seg['problems'].append(f"{f['name']} {'reverted (client gone)' if f['gone'] else 'flipped'} inside it")
    return segs


def analyze_live(plan, R, out):
    """Segment metrics, pairs per unit, and the rules' verdicts."""
    res = out['runs']['live']
    margin = plan.get('margin_s', 2.0); period = plan.get('period_s', 5.027)
    items = plan.get('items', {})
    segs = segments(R, plan)
    if not segs:
        res['problems'].append('no segment marks')
        return
    first = min(x['start'] for x in segs)
    grids = estimate_spikes(R, first - 2.0, max(x['end'] or x['start'] for x in segs) + 2.0, plan.get('hitches'))
    values = {}
    for seg in segs:
        lab = f"{seg['k']}:{seg['item']}:{seg['arm']}"
        seg['label'] = lab
        if seg['end'] is None:
            values[lab] = {}; continue
        s, e = seg['start'] + margin, seg['end'] - margin
        # Rule 2's settle counts from the switch's last flip, whatever the runner's mark timing.
        sw = items.get(seg['item'], {}).get('switch')
        flips = [f['t'] for f in R['flips'] if f['name'] == sw and f['t'] <= seg['start']]
        if flips:
            s = max(s, flips[-1] + items[seg['item']].get('settle_s', plan.get('segment_settle_s', 5.0)))
        if e - s >= period:
            e = s + math.floor((e - s) / period) * period
        if e - s < plan.get('min_window_s', 10.0):
            seg['problems'].append(f'window {e - s:.1f} s is too short')
        v = metrics(R, s, e, plan.get('watch'))
        v.update(spike_metrics(R, grids, s, e, plan.get('hitches')))
        v.update(seg=seg['k'], item=seg['item'], arm=seg['arm'], unit=seg['unit'])
        values[lab] = v
    labels = [seg['label'] for seg in segs if values[seg['label']]]
    checks = band_checks(values, plan, labels)
    for seg in segs:
        p, f = checks.get(seg['label'], ([], []))
        seg['problems'] += p; seg['flags'] = f
        values[seg['label']]['problems'] = seg['problems']; values[seg['label']]['flags'] = f
    out['segments'] = [values[seg['label']] | dict(label=seg['label']) for seg in segs]
    # Units: by the marks' unit token, else 8 consecutive segments of an item.
    units = {}
    for seg in segs:
        units.setdefault(seg['item'], {})
    for item in units:
        mine = [seg for seg in segs if seg['item'] == item]
        if all(seg['unit'] is not None for seg in mine):
            for seg in mine:
                units[item].setdefault(seg['unit'], []).append(seg)
        else:
            for i, seg in enumerate(mine):
                units[item].setdefault(str(i // 8), []).append(seg)
    pairs = {}; drop = plan.get('drop', 'unit')
    for item, us in units.items():
        pairs[item] = []
        for u, ss in us.items():
            unit_pairs = []; bad = []
            if len(ss) % 2:
                bad.append(f'{len(ss)} segments')
            for i in range(0, len(ss) - 1, 2):
                x, y = ss[i], ss[i + 1]
                if {x['arm'], y['arm']} != {'A', 'B'}:
                    bad.append(f"segments {x['k']} and {y['k']} are not one A and one B"); continue
                if x['problems'] or y['problems']:
                    bad.append(f"segment {x['k'] if x['problems'] else y['k']} is invalid"); continue
                a, b = (x, y) if x['arm'] == 'A' else (y, x)
                unit_pairs.append(dict(item=values[b['label']], control=values[a['label']], unit=u))
            if bad and drop == 'unit':
                out['notes'].append(f'{item} unit {u} dropped: ' + '; '.join(bad))
                continue
            if bad:
                out['notes'].append(f'{item} unit {u}: ' + '; '.join(bad))
            pairs[item] += unit_pairs
    out['pairs'] = {item: len(p) for item, p in pairs.items()}
    items = plan.get('items', {})
    nulls = [n for n, it in items.items() if it.get('null')]
    for rule in plan.get('rules', []):
        item = rule['item']
        planned = items.get(item, {}).get('units')
        null = rule.get('null', nulls[0] if nulls and item not in nulls else None)
        null_half = None
        if null and rule.get('kind', 'decide') == 'decide' and len(pairs.get(null, [])) > 1:
            d = [gain(rule, p['item'][rule['metric']], p['control'][rule['metric']]) for p in pairs[null]
                 if rule['metric'] in p['item'] and rule['metric'] in p['control']]
            null_half = pair_stats(d)[4] if len(d) > 1 else None
        r2 = dict(rule, null=null) if null else rule
        out['verdicts'][rule_key(rule)] = decide_live(r2, pairs.get(item, []), planned * 4 if planned else None, null_half,
                                                      len(units.get(item, {})))


def rule_key(rule):
    return f"{rule['item']} {rule['metric']}" + (f" {rule['kind']}" if rule.get('kind', 'decide') != 'decide' else '')


# ---------------------------------------------------------------- output

def fmt(v):
    if v is None:
        return '-'
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f'{v:.0f}'
        return f'{v:.3f}' if abs(v) < 100 else f'{v:.2f}'
    return str(v)


def table(rows, cols, file=None):
    file = file or sys.stdout
    width = {c: max(len(c), *(len(fmt(r[1].get(c))) for r in rows)) for c in cols}
    name = max(5, *(len(r[0]) for r in rows))
    print(' ' * name + '  ' + '  '.join(c.rjust(width[c]) for c in cols), file=file)
    for lab, r in rows:
        print(lab.ljust(name) + '  ' + '  '.join(fmt(r.get(c)).rjust(width[c]) for c in cols), file=file)


FRAME_COLS = ['fps', 'frame_ms', 'frame_ms_ns', 'low1_fps', 'low1_ns', 'low01_fps', 'p99_ms', 'over33',
              'spk_fsr5_n', 'spk_fsr5_ms', 'spk_trace4_n', 'spk_trace4_ms', 'spikes_other']
WORK_COLS = ['gpu_busy_ms', 'gpu_busy_mean_ms', 'back_work_ms', 'back_cpu_ms', 'watch_ms', 'draws_pf',
             'probe_mhz', 'back_ghz', 'pkg_w']
ROUTE_COLS = ['excess_s', 'excess_wo_last_tail_s', 'frames_over33', 'over100', 'worst_ms', 'loads',
              'steady_back_us_per_draw', 'probe_mhz']


def verdict_line(rule, v):
    unit = '%' if rule.get('relative') else ''
    line = f"{rule['item']} vs {rule.get('control', 'its A value')} on {rule['metric']}: {v['verdict']}"
    if 'gain' in v:
        line += f", gain {v['gain']:+.3f}{unit} (needs {rule['min_effect']}{unit})"
    if 'delta' in v:
        line += f", delta {v['delta']:+.3f}{unit}"
    if 'ci95' in v:
        line += f", 95% CI [{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}]"
        if 'pairs_better' in v:
            line += f", {v['pairs_better']} of {v['pairs']} pairs better"
        else:
            line += f" over {v['pairs']} pairs"
        if v.get('null_half_width') is not None:
            line += f", null half-width {v['null_half_width']:.3f}"
        if v['verdict'] == 'INCONCLUSIVE' and v.get('pairs_needed_for_min_effect'):
            line += f", ~{v['pairs_needed_for_min_effect']} pairs would resolve {rule['min_effect']}{unit}"
    if 'control_spread' in v:
        line += f", control spread {v['control_spread']:.3f}, every item run better: {v['all_item_runs_better']}"
        if v['verdict'] == 'INCONCLUSIVE' and v.get('runs_per_arm_needed'):
            line += f", ~{v['runs_per_arm_needed']} runs per arm for this gain"
    if v.get('per_block'):
        line += '; per block ' + ', '.join(f'{b}: {x:+.3f}' for b, x in v['per_block'].items())
    if v.get('notes'):
        line += '; ' + '; '.join(v['notes'])
    if v.get('guards_tripped'):
        line += '; GUARD: ' + '; '.join(v['guards_tripped'])
    return line


def report(plan, out, values, file=None):
    file = file or sys.stdout
    kind = plan['kind']
    print(f"{plan.get('name', '')} ({kind})", file=file)
    for n in out['notes_before']:
        print('NOTE: ' + n, file=file)
    if kind == 'live':
        segs = out.get('segments', [])
        if segs:
            cols = ['fps', 'frame_ms', 'frame_ms_ns', 'gpu_busy_ms', 'back_work_ms', 'back_cpu_ms', 'back_ghz', 'pkg_w',
                    'spk_fsr5_n', 'probe_mhz']
            table([(s['label'], s) for s in segs], cols, file)
            for s in segs:
                for p in s.get('problems', []):
                    print(f"PROBLEM segment {s['label']}: {p}", file=file)
                for f in s.get('flags', []):
                    print(f"flag segment {s['label']}: {f}", file=file)
    else:
        order = list(out['runs'])
        rows = [(lab + (' (W)' if out['runs'][lab]['warmup'] else ''), values.get(lab, {})) for lab in order]
        if kind == 'fountain':
            w = out['window']
            print(f"window: arrival + {w['start_s']:g} s for {w['length_s']:.1f} s"
                  + (f" (planned {w['planned_s']:.1f})" if w['planned_s'] and w['planned_s'] > w['length_s'] + 0.05 else ''),
                  file=file)
            table(rows, FRAME_COLS, file); print(file=file); table(rows, WORK_COLS, file)
            cols = FRAME_COLS[:4] + WORK_COLS
        else:
            table(rows, ROUTE_COLS, file); cols = ROUTE_COLS
        per_arm = {}
        for lab in order:
            r = out['runs'][lab]
            if not r['warmup'] and not r['problems']:
                per_arm.setdefault(r['arm'], []).append(values[lab])
        if per_arm:
            print('\nper arm (mean of its valid measured runs):', file=file)
            table([(arm, {c: st.mean([v[c] for v in vs if c in v]) if any(c in v for v in vs) else None for c in cols}
                    | {'runs': len(vs)}) for arm, vs in per_arm.items()], cols + ['runs'], file)
    for lab, r in out['runs'].items():
        for p in r['problems']:
            print(f'PROBLEM {lab}: {p} (left out of the verdicts)', file=file)
        for f in r.get('flags', []):
            print(f'flag {lab}: {f}', file=file)
    for n in out['notes']:
        print('NOTE: ' + n, file=file)
    print(file=file)
    for rule in plan.get('rules', []):
        v = out['verdicts'].get(rule_key(rule))
        if v:
            print(verdict_line(rule, v), file=file)


def analyze(plan, src):
    """The whole analysis of one session: returns (out, values)."""
    kind = plan['kind']
    out = dict(name=plan.get('name'), kind=kind, runs={}, verdicts={}, notes=[], notes_before=[])
    values = {}
    if kind == 'live':
        runs = src.runs() or ([dict(label='live', log=plan['log'], info={}, beside=set())] if plan.get('log') else [])
        if len(runs) != 1:
            out['notes_before'].append(f'a live session needs one log, found {len(runs)}')
            return out, values
        with src.lines(runs[0]['log']) as f:
            R = parse(f)
        arm = next(iter(plan.get('arms', {})), None)
        must, mustnt = arm_expectations(plan)
        flags = []
        out['runs']['live'] = dict(file=runs[0]['log'], arm=arm, warmup=False, version=R['version'], flags=flags,
                                   problems=run_problems(R, arm, plan, must, mustnt, False, flags))
        if 'eden_log_2.txt' in runs[0]['beside']:
            out['runs']['live']['problems'].append('eden_log_2.txt was written (another process held the log)')
        if not out['runs']['live']['problems']:
            analyze_live(plan, R, out)
        return out, values
    if plan.get('runs'):
        runs = manual_runs(plan, src)
    else:
        runs, notes = bench_runs(plan, src)
        out['notes_before'] += notes
    if kind == 'fountain':
        values = analyze_fountain(plan, runs, src, out)
        fountain_verdicts(plan, runs, values, out)
    elif kind == 'route':
        values = analyze_route(plan, runs, src, out)
        fountain_verdicts(plan, runs, values, out)
    else:
        raise SystemExit(f'unknown plan kind {kind!r}')
    return out, values


def main(argv=None):
    # Log text can hold any character; a redirected Windows console must not stop the report.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('plan', nargs='?')
    ap.add_argument('--zip'); ap.add_argument('--session'); ap.add_argument('--logs'); ap.add_argument('--json')
    a = ap.parse_args(argv)
    src = Source(zip_path=a.zip, folder=a.session, logs=a.logs or (os.path.dirname(os.path.abspath(a.plan)) if a.plan else None))
    plan = None
    if a.plan:
        with open(a.plan, encoding='utf-8') as f:
            plan = json.load(f)
    ran = src.plan() if (a.zip or a.session) else None
    if plan is None:
        plan = ran
    if plan is None:
        ap.error('no plan: pass one, or a session whose zip holds plan.json')
    bad = plan_problems(plan)
    if bad:
        for b in bad:
            print('PLAN: ' + b, file=sys.stderr)
        return 2
    differs = []
    if ran is not None and ran is not plan:
        # The runner's plan.json carries its defaults: a given plan without them uses the runner's.
        for key in ('settle_s', 'measure_s'):
            if key not in plan and key in ran:
                plan[key] = ran[key]
        for key in ('rules', 'arms', 'blocks', 'design', 'items', 'warmup', 'gates', 'settle_s', 'measure_s', 'window', 'build'):
            if json.dumps(ran.get(key), sort_keys=True) != json.dumps(plan.get(key), sort_keys=True):
                differs.append(f'the plan given differs from the plan the runner ran in "{key}"')
    out, values = analyze(plan, src)
    out['notes_before'] += differs
    report(plan, out, values)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=1, default=float)
    return 0


if __name__ == '__main__':
    sys.exit(main())
