#!/usr/bin/env python3
"""A/B analysis of Lichii logs by a schedule written before the runs (lichii-tuned).

    python3 -I ab_analyze.py <schedule.json> [--logs <dir>] [--json <out.json>]

The schedule names the logs, the arm of each, what each arm's boot lines must show, the window and
the decision rules. Three kinds:

`runs` may be "auto": every <nn>-<arm>.txt in the log folder, in name order (a launcher that copies
its log after the run names it so), the arm read from an `A/B arm: <name>` line when there is one.

- "fountain": one launch per run at a fixed spot. Each run's window starts at its arrival (the
  first second that starts 5 s of 30+ frames and 2000+ draws a frame) plus `start_s` and lasts
  `length_s` (default: the shortest stay, so every run covers the same span after arrival).
- "route": one launch per run over the same route with area loads: slow frames (over 33.4 ms) and
  their time over 16.7 ms from the first load on, per load.
- "live": one launch whose arms are switched live; `AB block <k> arm <label> from present <n>`
  lines start each block and `AB end` ends the last. The first `settle_s` of each block is
  dropped; consecutive blocks pair up (ABBA BAAB keeps a linear drift out of the mean).

Logs are data: they are only matched with regular expressions. Standard library only.
"""
import argparse
import bisect
import json
import math
import os
import re
import statistics as st
import sys

LINE = re.compile(r'^\[\s*([0-9.]+)\] (\S+) <(\w+)> ([^:]+):(\d+):(\S+?): (.*)$')
GF = re.compile(r'DKB gpu_frame renderer_frame=(\d+) .*?gpu_frame_span_ms=(-?[0-9.]+) .*?gpu_busy_ms=([0-9.]+) '
                r'measured_submissions=(\d+) dropped_submissions=(\d+) .*?draws_timed=(\d)')
DRAWS = re.compile(r'DKB draws through_frame=(\d+) frames=(\d+) attempts=(\d+)')
KV = re.compile(r'(\w+)=(-?[0-9.]+)')
PROBE = re.compile(r'GPU clock probe in 10 s: median (\d+) MHz')
CLOCK_ROLE = r'\b{} (?:core=\d+ )?ghz=([0-9.]+|n/a) cpu_ms=([0-9.]+) poll_ms=([0-9.]+) work_ms=([0-9.]+)'
WATCH = re.compile(r'([0-9a-f]{16})=([0-9.]+)ms/(\d+)x')
AB_BLOCK = re.compile(r'AB block (\d+) arm (\S+) from present (\d+)(?:: (.*))?$')
AB_END = re.compile(r'AB end at present (\d+)')
SLOW_MS, BASE_MS = 33.4, 16.7


def parse(path, grep=()):
    R = dict(frames=[], gpu=[], cpu=[], clock=[], probe=[], draws=[], watch=[], blocks=[], settings={},
             version=None, switches=None, power=None, crash=[], errors=0, marks={}, grep={p: 0 for p in grep})
    patterns = [(p, re.compile(p)) for p in grep]
    mk = R['marks']
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
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
            if msg.startswith('DKB frame_ms n='):
                vals = [float(x) for x in msg.split()[3:]]
                end = t; seq = []
                for v in reversed(vals):
                    seq.append((end, v)); end -= v / 1000.0
                R['frames'].extend(reversed(seq))
            elif msg.startswith('DKB gpu_frame'):
                g = GF.search(msg)
                if g and int(g[5]) == 0 and float(g[2]) > 0:
                    R['gpu'].append((t, float(g[3]), int(g[6])))
            elif msg.startswith('DKB cpu through_frame'):
                d = dict((k, float(v)) for k, v in KV.findall(msg))
                if d.get('frames') and 'gpu_back_cpu_ms' in d:
                    R['cpu'].append((t, d['frames'], d['gpu_back_cpu_ms'], d.get('gpu_command_cpu_ms', 0.0)))
            elif msg.startswith('DKB draws through_frame'):
                g = DRAWS.search(msg)
                if g:
                    R['draws'].append((t, int(g[2]), int(g[3])))
            elif msg.startswith('DKB gpu_watch'):
                timed = re.search(r'timed=(\d+)', msg)
                if timed and int(timed[1]) > 0:
                    R['watch'].append((t, sum(float(w[2]) for w in WATCH.finditer(msg))))
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
                R['clock'].append(c)
            elif fn == 'LogSettings':
                s = re.search(r'([\w.]+): (.*)$', msg)
                if s:
                    R['settings'].setdefault(s[1], s[2].strip())
            elif fn == 'LogTestSwitches' or msg.startswith('TEST switches in effect'):
                R['switches'] = msg
            if R['version'] is None and 'Lichii Version:' in msg:
                R['version'] = msg.split('Lichii Version:', 1)[1].strip()
            if msg.startswith('DKB frame present=1 '):
                mk.setdefault('first_present', t)
            if 'Shutdown requested' in msg:
                mk.setdefault('shutdown', t)
            if 'Host crash' in msg:
                R['crash'].append(msg[:200])
            if fn == 'LogPowerMode' or msg.startswith('Power mode'):
                R['power'] = msg[:200]
            b = AB_BLOCK.search(msg)
            if b:
                R['blocks'].append(dict(t=t, k=int(b[1]), arm=b[2], present=int(b[3]), set=b[4] or ''))
            e = AB_END.search(msg)
            if e:
                mk['ab_end'] = t
    R['frame_ends'] = [f[0] for f in R['frames']]
    return R


def end_of(R):
    return R['marks'].get('shutdown', R['marks'].get('last', 0.0))


def frames_in(R, s, e):
    lo = bisect.bisect_right(R['frame_ends'], s); hi = bisect.bisect_right(R['frame_ends'], e)
    return [ms for _, ms in R['frames'][lo:hi]]


def draws_pf(R, s, e):
    d = [x for x in R['draws'] if s < x[0] <= e]
    return sum(x[2] for x in d) / max(1, sum(x[1] for x in d)) if d else 0.0


def arrival(R):
    s0 = int(R['marks'].get('first_present', 0))
    for s in range(s0, int(end_of(R))):
        if all(len(frames_in(R, s + k, s + k + 1)) >= 30 for k in range(5)) and draws_pf(R, s, s + 5) >= 2000:
            return float(s)
    return None


def low(v, share):
    k = max(1, int(len(v) * share))
    return 1000.0 * k / sum(sorted(v)[-k:])


def metrics(R, s, e):
    """Steady-play numbers of [s, e]: frame times, GPU busy, the back thread, clocks, draws."""
    v = frames_in(R, s, e)
    out = dict(seconds=round(e - s, 1))
    if v:
        srt = sorted(v)
        out.update(frames=len(v), fps=1000.0 * len(v) / sum(v), frame_ms=sum(v) / len(v),
                   p99_ms=srt[min(len(v) - 1, int(0.99 * len(v)))], low1_fps=low(v, 0.01), low01_fps=low(v, 0.001),
                   over33=sum(1 for x in v if x > SLOW_MS))
    g = [b for t, b, timed in R['gpu'] if s < t <= e and timed == 0]
    if g:
        out.update(gpu_busy_ms=st.median(g), gpu_busy_mean_ms=st.mean(g), gpu_samples=len(g))
    c = [x for x in R['cpu'] if s < x[0] <= e]
    if c:
        n = sum(x[1] for x in c)
        out.update(back_cpu_ms=sum(x[2] for x in c) / n, front_cpu_ms=sum(x[3] for x in c) / n)
    # Clock trace lines cover the 10 s before them: only those wholly inside the window.
    cl = [x for x in R['clock'] if x['t'] - x['win'] >= s - 0.5 and x['t'] <= e + 0.5 and 'back' in x]
    work = frames = 0.0; ghz = []; pkg = []
    for x in cl:
        n = len(frames_in(R, x['t'] - x['win'], x['t']))
        if n:
            work += x['back']['work'] * x['win']; frames += n
            if x['back']['ghz']:
                ghz.append(x['back']['ghz'])
            if x['pkg_w']:
                pkg.append(x['pkg_w'])
    if frames:
        out['back_work_ms'] = work / frames
    if ghz:
        out['back_ghz'] = st.mean(ghz)
    if pkg:
        out['pkg_w'] = st.mean(pkg)
    p = [m for t, m in R['probe'] if s + 10 <= t <= e + 0.5]
    if p:
        out['probe_mhz'] = st.median(p)
    w = [x for t, x in R['watch'] if s < t <= e]
    if w:
        out['watch_ms'] = st.mean(w)
    out['draws_pf'] = draws_pf(R, s, e)
    return out


def loads(R):
    """Area loads as r64-sitting/budget64.py finds them: clusters of frames over 150 ms with a fade
    (a second under 30 draws a frame) at most 6.5 s before them."""
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
        # The load itself and its first 3 s (budget64's classes A and B).
        out[f'L{i + 1}_ms'] = sum(ms - BASE_MS for t, ms in slow if s - 0.05 <= t <= e + 3.0)
    last_s, last_e = L[-1]
    out['excess_wo_last_tail_s'] = out['excess_s'] - sum(ms - BASE_MS for t, ms in slow if last_e + 3.0 < t <= last_e + 25.0) / 1000.0
    out.update({k: v for k, v in metrics(R, start, end_of(R)).items() if k in ('probe_mhz', 'pkg_w')})
    # Steady play between the loads (3 s before each to 25 s after it left out): the back thread's
    # CPU time per draw, which a smoothness item must not raise.
    def steady(t):
        return t > start and t < end_of(R) - 3.0 and not any(s - 3.0 <= t <= e + 25.0 for s, e in L)
    cpu = [x for x in R['cpu'] if steady(x[0])]
    draws = sum(a for t, n, a in R['draws'] if steady(t))
    if cpu and draws:
        out['steady_back_us_per_draw'] = 1000.0 * sum(x[2] for x in cpu) / draws
        out['steady_back_cpu_ms'] = sum(x[2] for x in cpu) / sum(x[1] for x in cpu)
        out['steady_seconds'] = len(cpu)
    return out


def checks(lab, R, spec, schedule):
    """What must hold for a run to count: the build, the arm's switches, no crash, the machine state."""
    bad = []; warn = []
    if schedule.get('build') and schedule['build'] not in (R['version'] or ''):
        bad.append(f"build is not {schedule['build']}: {R['version']}")
    for name, value in spec.get('switches', {}).items():
        if not R['switches'] or f'{name} {value}' not in R['switches']:
            bad.append(f'TEST switches line lacks "{name} {value}"')
    for pattern in spec.get('expect_lines', []):
        if not R['grep'].get(pattern):
            bad.append(f'no line matches {pattern!r}')
    if R['crash']:
        bad.append('host crash: ' + R['crash'][0])
    modes = {c['power_mode'] for c in R['clock'] if c.get('power_mode')}
    if len(modes) > 1 or (schedule.get('power_mode') and modes and schedule['power_mode'] not in modes):
        bad.append(f'power modes {sorted(modes)}')
    for name, value in schedule.get('settings', {}).items():
        if R['settings'].get(name) != value:
            bad.append(f'setting {name} is {R["settings"].get(name)}, not {value}')
    return bad, warn


def expect(values, schedule):
    """The schedule's ranges for the machine state and the scene (e.g. probe_mhz [3000, 3500])."""
    out = []
    for metric, (lo, hi) in schedule.get('expect', {}).items():
        v = values.get(metric)
        if v is None or not lo <= v <= hi:
            out.append(f'{metric} {fmt(v)} outside [{lo}, {hi}]')
    return out


def tval(n):
    table = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18,
             15: 2.13, 20: 2.09, 30: 2.04}
    for k in sorted(table, reverse=True):
        if n >= k:
            return table[k]
    return 12.71


def gain(rule, item, control):
    """The item's improvement over the control in the rule's metric (positive = better)."""
    d = item - control
    if rule.get('relative'):
        d = 100.0 * d / control
    return -d if rule.get('better', 'lower') == 'lower' else d


def decide_launch(rule, order, values):
    """Launch design (r64-r66): the item's runs against the control runs next to them."""
    item, control, metric = rule['item'], rule['control'], rule['metric']
    idx = [i for i, (_, arm) in enumerate(order) if arm == item]
    if not idx:
        return dict(verdict='NO RUNS')
    before = [i for i in range(idx[0] - 1, -1, -1) if order[i][1] == control][:1]
    after = [i for i in range(idx[-1] + 1, len(order)) if order[i][1] == control][:1]
    ctl = [order[i][0] for i in before + after]
    its = [order[i][0] for i in idx]
    if not ctl or any(metric not in values[r] for r in its + ctl):
        return dict(verdict='NO DATA', item_runs=its, control_runs=ctl)
    iv = [values[r][metric] for r in its]; cv = [values[r][metric] for r in ctl]
    g = gain(rule, st.mean(iv), st.mean(cv))
    spread = abs(gain(rule, max(cv), min(cv))) if len(cv) > 1 else float('inf')
    better = all(gain(rule, a, b) > 0 for a in iv for b in cv)
    reasons = []; violated = []
    for guard in rule.get('guards', []):
        gv = [values[r].get(guard['metric']) for r in its]; gc = [values[r].get(guard['metric']) for r in ctl]
        if None in gv + gc:
            reasons.append(f"guard {guard['metric']}: no data"); continue
        worse = -gain(guard, st.mean(gv), st.mean(gc))
        reasons.append(f"{guard['metric']} worse by {worse:+.3f}{'%' if guard.get('relative') else ''} (at most {guard['max_worse']})")
        if worse > guard['max_worse']:
            violated.append(f"{guard['metric']} worse by {worse:.3f} > {guard['max_worse']}")
    adopt = g >= rule['min_effect'] and g > spread and better and not violated
    drop = g <= 0 or bool(violated)
    sd = st.stdev(cv) if len(cv) > 1 else None
    if sd is not None and rule.get('relative'):
        sd = 100.0 * sd / st.mean(cv)
    need = math.ceil(2 * ((1.96 + 0.84) * sd / g) ** 2) if sd and g > 0 else None
    return dict(verdict='ADOPT' if adopt else 'DROP' if drop else 'INCONCLUSIVE', item_runs=its, control_runs=ctl,
                item=iv, control=cv, gain=g, control_spread=spread, all_item_runs_better=better,
                guards_violated=violated, notes=reasons, runs_per_arm_needed=need)


def decide_live(rule, pairs):
    """Live design: one difference per pair of neighbouring blocks (item minus control)."""
    metric = rule['metric']
    d = [gain(rule, p['item'][metric], p['control'][metric]) for p in pairs
         if metric in p['item'] and metric in p['control']]
    n = len(d)
    if n < 2:
        return dict(verdict='NO DATA', pairs=n)
    mean = st.mean(d); sd = st.stdev(d); se = sd / math.sqrt(n); t = tval(n - 1)
    agree = sum(1 for x in d if x > 0)
    violated = []
    for guard in rule.get('guards', []):
        gd = [-gain(guard, p['item'][guard['metric']], p['control'][guard['metric']]) for p in pairs
              if guard['metric'] in p['item'] and guard['metric'] in p['control']]
        if gd and st.mean(gd) > guard['max_worse']:
            violated.append(f"{guard['metric']} worse by {st.mean(gd):.3f} > {guard['max_worse']}")
    adopt = (n >= rule.get('min_pairs', 4) and mean >= rule['min_effect'] and mean - t * se > 0
             and agree >= math.ceil(rule.get('agree', 0.8) * n) and not violated)
    drop = bool(violated) or (n >= rule.get('min_pairs', 4) and mean + t * se < rule['min_effect'])
    need = math.ceil(((t + 0.84) * sd / rule['min_effect']) ** 2) if rule['min_effect'] > 0 else None
    return dict(verdict='ADOPT' if adopt else 'DROP' if drop else 'INCONCLUSIVE', pairs=n, gain=mean, ci95=[mean - t * se, mean + t * se],
                pairs_better=agree, per_pair=[round(x, 4) for x in d], guards_violated=violated, pairs_needed_for_min_effect=need)


def rule_key(rule):
    return f"{rule['item']} {rule['metric']}"


def fmt(v):
    return '-' if v is None else (f'{v:.3f}' if isinstance(v, float) and v != int(v) or isinstance(v, float) and abs(v) < 100 else
                                  str(int(v)) if isinstance(v, float) else str(v))


def table(rows, cols):
    width = {c: max(len(c), *(len(fmt(r[1].get(c))) for r in rows)) for c in cols}
    name = max(5, *(len(r[0]) for r in rows))
    print(' ' * name + '  ' + '  '.join(c.rjust(width[c]) for c in cols))
    for lab, r in rows:
        print(lab.ljust(name) + '  ' + '  '.join(fmt(r.get(c)).rjust(width[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('schedule'); ap.add_argument('--logs'); ap.add_argument('--json')
    a = ap.parse_args()
    S = json.load(open(a.schedule, encoding='utf-8'))
    logs = a.logs or S.get('logs') or os.path.dirname(os.path.abspath(a.schedule))
    kind = S['kind']; arms = S.get('arms', {}); result = dict(name=S.get('name'), kind=kind, runs={}, verdicts={})
    print(f"{S.get('name', a.schedule)} ({kind})")
    cols = S.get('columns') or (['excess_s', 'excess_wo_last_tail_s', 'frames_over33', 'over100', 'worst_ms', 'loads',
                                 'steady_back_us_per_draw', 'probe_mhz']
                                if kind == 'route' else
                                ['fps', 'frame_ms', 'low1_fps', 'gpu_busy_ms', 'back_work_ms', 'back_cpu_ms', 'draws_pf', 'probe_mhz', 'back_ghz', 'pkg_w'])
    if kind in ('fountain', 'route'):
        if S.get('runs') == 'auto':
            # Logs a launcher saved as <nn>-<arm>.txt, in name order; an 'A/B arm: <name>' boot line wins.
            S['runs'] = []
            for name in sorted(os.listdir(logs)):
                m = re.match(r'^(\d+)-(.+?)\.txt$', name)
                if m:
                    arm = m[2]
                    with open(os.path.join(logs, name), encoding='utf-8', errors='replace') as f:
                        for raw in f:
                            a_ = re.search(r'A/B arm: (\S+)', raw)
                            if a_:
                                arm = a_[1]; break
                    S['runs'].append(dict(file=name, arm=arm, label=f'{m[1]}-{arm}'))
        order = [(r.get('label', f"{r['arm']}#{i + 1}"), r['arm']) for i, r in enumerate(S['runs'])]
        parsed = {}; values = {}; win = S.get('window', {})
        for (lab, arm), r in zip(order, S['runs']):
            parsed[lab] = R = parse(os.path.join(logs, r['file']), arms.get(arm, {}).get('expect_lines', []))
            bad, warn = checks(lab, R, arms.get(arm, {}), S)
            result['runs'][lab] = dict(file=r['file'], arm=arm, version=R['version'], problems=bad, warnings=warn)
        if kind == 'fountain':
            arr = {lab: arrival(R) for lab, R in parsed.items()}
            stays = [end_of(parsed[lab]) - t for lab, t in arr.items() if t is not None]
            start = win.get('start_s', 2.0)
            length = win.get('length_s') or (min(stays) - 3.0 - start if stays else 0)
            for lab, R in parsed.items():
                if arr[lab] is None:
                    result['runs'][lab]['problems'].append('no arrival found'); values[lab] = {}
                    continue
                s = arr[lab] + start; e = min(s + length, end_of(R) - 3.0)
                values[lab] = metrics(R, s, e); values[lab]['arrival_s'] = arr[lab]
            print(f"window: arrival + {start:g} s for {length:.1f} s")
        else:
            for lab, R in parsed.items():
                values[lab] = route_metrics(R)
        for lab in values:
            result['runs'][lab]['values'] = values[lab]
            result['runs'][lab]['problems'] += expect(values[lab], S)
        table([(lab, values[lab]) for lab, _ in order], cols)
        # Per arm: the mean of its runs.
        per_arm = {}
        for lab, arm in order:
            per_arm.setdefault(arm, []).append(values[lab])
        print('\nper arm (mean of its runs):')
        table([(arm, {c: st.mean([v[c] for v in vs if c in v]) if any(c in v for v in vs) else None for c in cols} | {'runs': len(vs)})
               for arm, vs in per_arm.items()], cols + ['runs'])
        for rule in S.get('rules', []):
            result['verdicts'][rule_key(rule)] = decide_launch(rule, [(lab, arm) for lab, arm in order if not result['runs'][lab]['problems']], values)
    elif kind == 'live':
        R = parse(os.path.join(logs, S['log']), [p for spec in arms.values() for p in spec.get('expect_lines', [])])
        bad, warn = checks('live', R, {}, S)
        result['runs']['live'] = dict(file=S['log'], version=R['version'], problems=bad, warnings=warn)
        settle = S.get('settle_s', 3.0); B = R['blocks']
        stop = R['marks'].get('ab_end', end_of(R))
        blocks = []
        for i, b in enumerate(B):
            e = B[i + 1]['t'] if i + 1 < len(B) else stop
            v = metrics(R, b['t'] + settle, e - 0.5)
            v.update(block=b['k'], arm=b['arm']); blocks.append(v)
            wrong = [f'{n}={x}' for n, x in arms.get(b['arm'], {}).get('set', {}).items() if f'{n}={x}' not in b['set']]
            wrong += expect(v, S)
            if wrong:
                v['left_out'] = True
                result['runs']['live']['problems'].append(f"block {b['k']} ({b['arm']}): " + ', '.join(wrong))
        table([(f"{v['block']}:{v['arm']}", v) for v in blocks], cols)
        result['blocks'] = blocks
        for rule in S.get('rules', []) if not bad else []:
            pairs = []
            for i in range(0, len(blocks) - 1, 2):
                x, y = blocks[i], blocks[i + 1]
                if x.get('left_out') or y.get('left_out'):
                    continue
                arms_ = {x['arm'], y['arm']}
                if arms_ == {rule['item'], rule['control']}:
                    pairs.append(dict(item=x if x['arm'] == rule['item'] else y, control=y if x['arm'] == rule['item'] else x))
            result['verdicts'][rule_key(rule)] = decide_live(rule, pairs)
    for lab, r in result['runs'].items():
        for p in r['problems']:
            print(f'PROBLEM {lab}: {p} (left out of the verdicts)' if lab != 'live' else f'PROBLEM: {p} (pair left out)')
        for w in r['warnings']:
            print(f'warning {lab}: {w}')
    if kind != 'live':
        mhz = {lab: v.get('probe_mhz') for lab, v in values.items() if v.get('probe_mhz')}
        if mhz and (max(mhz.values()) - min(mhz.values())) > 0.02 * min(mhz.values()):
            print(f'warning: the GPU clock differs between runs by more than 2% ({min(mhz.values()):.0f}-{max(mhz.values()):.0f} MHz)')
    print()
    for key, v in result['verdicts'].items():
        rule = next(r for r in S['rules'] if rule_key(r) == key)
        unit = '%' if rule.get('relative') else ''
        line = f"{rule['item']} vs {rule['control']} on {rule['metric']}: {v['verdict']}"
        if 'gain' in v:
            line += f", gain {v['gain']:+.3f}{unit} (needs {rule['min_effect']}{unit})"
        if 'ci95' in v:
            line += f", 95% CI [{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}], {v['pairs_better']} of {v['pairs']} pairs better"
            if v['verdict'] == 'INCONCLUSIVE' and v.get('pairs_needed_for_min_effect'):
                line += f", ~{v['pairs_needed_for_min_effect']} pairs would resolve {rule['min_effect']}{unit}"
        if 'control_spread' in v:
            line += f", control spread {v['control_spread']:.3f}, every item run better: {v['all_item_runs_better']}"
            if v.get('runs_per_arm_needed'):
                line += f", ~{v['runs_per_arm_needed']} runs per arm for this gain"
        if v.get('notes'):
            line += '; ' + '; '.join(v['notes'])
        if v.get('guards_violated'):
            line += '; GUARD: ' + '; '.join(v['guards_violated'])
        print(line)
    if a.json:
        json.dump(result, open(a.json, 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
