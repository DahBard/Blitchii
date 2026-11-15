"""Tests for analyze.py: generated logs always; r66 sitting 1's eight logs when
LICHII_BENCH_R66_LOGS names the folder that holds them (they are never committed).

    python3 -I lichii-tuned/bench/test_analyze.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze as A  # noqa: E402
import fakelog as F  # noqa: E402

FOUNTAIN_RULES = [{"item": "X", "control": "N", "metric": "gpu_busy_ms", "better": "lower", "min_effect": 0.10,
                   "guards": [{"metric": "back_work_ms", "max_worse": 0.10}]}]


def fountain_plan(**kw):
    p = {"name": "generated fountain", "kind": "fountain", "build": "0123456789",
         "arms": {"N": {"base": "perf-trace-noprofile.bat", "env": {}},
                  "X": {"base": "perf-trace-noprofile.bat", "env": {"EDEN_NX2_TEST_X": "1"}}},
         "warmup": ["N"], "blocks": [["N", "X", "X", "N"], ["X", "N", "N", "X"]],
         "settle_s": 60, "measure_s": 100, "cooldown_s": 60,
         "gates": {"probe_mhz": "sitting", "cpu_band_pct": 1.5, "draws": [2600, 3200]},
         "rules": FOUNTAIN_RULES}
    p.update(kw)
    return p


def run_log(arm, seed, x_busy=14.8, x_back=14.5, **kw):
    on = arm == 'X'
    args = dict(seed=seed, switches=f"EDEN_NX2_TEST_X {'on' if on else 'off'}", arrival=60 + seed % 7,
                stay=172.0 + seed % 5, gpu_busy=x_busy if on else 15.2, back_work=x_back if on else 14.5)
    args.update(kw)
    return F.fountain(**args)


def write_session(root, plan, runs, as_zip=True, with_info=True):
    """runs: [(label, log text, run.json dict or None, extra files)] -> a session zip or folder."""
    files = {'plan.json': json.dumps(plan)}
    for i, (label, text, info, extra) in enumerate(runs):
        d = f'runs/{i + 1:02d}-{label}/'
        files[d + 'eden_log.txt'] = text
        if info is not None and with_info:
            files[d + 'run.json'] = json.dumps(info)
        for name in extra:
            files[d + name] = ''
    if as_zip:
        path = os.path.join(root, 'session.zip')
        with zipfile.ZipFile(path, 'w') as z:
            for name, text in files.items():
                z.writestr('2026-11-15-test/' + name, text)
        return A.Source(zip_path=path)
    for name, text in files.items():
        p = os.path.join(root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
    return A.Source(folder=root)


def standard_runs(plan, change=None, x_busy=14.8, x_back=14.5):
    """W, then the plan's blocks; change(index, label) may return (log, info, extra) for one run."""
    out = []; seed = 1
    for arm in plan['warmup']:
        out.append((f'W-{arm}', run_log(arm, seed), dict(arm=arm, warmup=True), []))
        seed += 1
    for b, block in enumerate(A.plan_blocks(plan)):
        for s, arm in enumerate(block):
            log = run_log(arm, seed, x_busy, x_back)
            item = (arm, log, dict(arm=arm, block=b, slot=s), [])
            if change:
                item = change(len(out), item) or item
            out.append(item)
            seed += 1
    return out


def analyze(plan, src):
    out, values = A.analyze(plan, src)
    buf = io.StringIO()
    A.report(plan, out, values, file=buf)
    return out, values, buf.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Spikes(Tmp):
    def test_periods_and_removal(self):
        R = A.parse(F.fountain(seed=3, stay=180.0, noise=0.6).splitlines())
        arr, how = A.arrival(R)
        self.assertEqual((arr, how), (70.0, 'draws'))
        grids = A.estimate_spikes(R, arr + 2, A.end_of(R) - 1)
        by = {g['name']: g for g in grids}
        self.assertAlmostEqual(by['fsr5']['period'], 5.027, delta=0.002)
        self.assertAlmostEqual(by['trace4']['period'], 4.157, delta=0.002)
        v = A.spike_metrics(R, grids, arr + 60, arr + 160)
        self.assertGreaterEqual(v['spk_fsr5_n'], v['spk_fsr5_slots'] - 1)
        self.assertLessEqual(v['spk_fsr5_n'], v['spk_fsr5_slots'] + 1)
        self.assertGreaterEqual(v['spk_trace4_n'], v['spk_trace4_slots'] - 3)
        self.assertAlmostEqual(v['spk_fsr5_ms'], 26.0, delta=2.0)
        self.assertAlmostEqual(v['frame_ms_ns'], 19.6, delta=0.06)
        m = A.metrics(R, arr + 60, arr + 160)
        self.assertGreater(m['frame_ms'], v['frame_ms_ns'])
        self.assertGreater(v['low1_ns'], m['low1_fps'])
        self.assertEqual(v['spikes_over'], [])

    def test_no_trace_hitch(self):
        R = A.parse(F.fountain(seed=4, hitches=((5.027, 0.2, 25.0),)).splitlines())
        grids = A.estimate_spikes(R, 72, A.end_of(R) - 1)
        self.assertEqual([g['name'] for g in grids], ['fsr5'])

    def test_hitch_removed_by_an_item(self):
        R = A.parse(F.fountain(seed=5, hitches=()).splitlines())
        grids = A.estimate_spikes(R, 72, A.end_of(R) - 1)
        self.assertEqual(grids, [])
        v = A.spike_metrics(R, grids, 130, 230)
        self.assertEqual(v['spk_fsr5_n'], 0)


class Fountain(Tmp):
    def test_adopt_from_zip(self):
        plan = fountain_plan()
        src = write_session(self.dir, plan, standard_runs(plan))
        out, values, text = analyze(plan, src)
        self.assertEqual(out['window']['length_s'], 100.0)
        self.assertEqual(out['window']['start_s'], 60)
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(v['verdict'], 'ADOPT', text)
        self.assertAlmostEqual(v['gain'], 0.4, delta=0.02)
        self.assertEqual(len(v['item_runs']), 4); self.assertEqual(len(v['control_runs']), 4)
        self.assertEqual(sorted(v['per_block']), ['0', '1'])
        warm = [r for r in out['runs'].values() if r['warmup']]
        self.assertEqual(len(warm), 1)
        for lab, r in out['runs'].items():
            self.assertEqual(r['problems'], [], lab)
            self.assertAlmostEqual(r['values']['seconds'], 100.0, delta=0.01)
        self.assertIn('X vs N on gpu_busy_ms: ADOPT', text)

    def test_guard_drops(self):
        plan = fountain_plan()
        src = write_session(self.dir, plan, standard_runs(plan, x_back=14.8))
        out, _, text = analyze(plan, src)
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(v['verdict'], 'DROP', text)
        self.assertTrue(v['guards_tripped'])

    def test_small_gain_inconclusive(self):
        plan = fountain_plan()
        src = write_session(self.dir, plan, standard_runs(plan, x_busy=15.17))
        v = analyze(plan, src)[0]['verdicts']['X gpu_busy_ms']
        self.assertIn(v['verdict'], ('INCONCLUSIVE', 'DROP'))
        self.assertNotEqual(v['verdict'], 'ADOPT')

    def test_folder_without_run_json_infers_roles(self):
        plan = fountain_plan()
        runs = [(lab.replace('W-', ''), log, None, extra) for lab, log, info, extra in standard_runs(plan)]
        src = write_session(self.dir, plan, runs, as_zip=False)
        out, _, text = analyze(plan, src)
        self.assertEqual(sum(r['warmup'] for r in out['runs'].values()), 1)
        self.assertEqual({r['block'] for r in out['runs'].values() if not r['warmup']}, {0, 1})
        self.assertEqual(out['verdicts']['X gpu_busy_ms']['verdict'], 'ADOPT', text)

    def test_wrong_switch_drops_its_block(self):
        plan = fountain_plan()

        def change(i, item):
            if i == 2:  # block 0, slot 1 (X) shows the control's switch
                arm, log, info, extra = item
                return arm, log.replace('EDEN_NX2_TEST_X on', 'EDEN_NX2_TEST_X off'), info, extra
        src = write_session(self.dir, plan, standard_runs(plan, change))
        out, _, text = analyze(plan, src)
        bad = [lab for lab, r in out['runs'].items() if r['problems']]
        self.assertEqual(len(bad), 1)
        self.assertIn('lacks "EDEN_NX2_TEST_X on"', out['runs'][bad[0]]['problems'][0])
        self.assertTrue(any('block 0 dropped' in n for n in out['notes']), text)
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(len(v['item_runs']), 2)
        self.assertIn('1 of 2 planned blocks', ' '.join(v['notes']))

    def test_retry_in_the_same_slot_keeps_the_block(self):
        plan = fountain_plan()
        runs = standard_runs(plan)
        arm, log, info, _ = runs[2]
        runs.insert(2, (arm, log.replace('EDEN_NX2_TEST_X on', 'EDEN_NX2_TEST_X off'), dict(info), ['eden_log_2.txt']))
        src = write_session(self.dir, plan, runs)
        out, _, text = analyze(plan, src)
        self.assertFalse(any('dropped' in n for n in out['notes']), text)
        bad = [r for r in out['runs'].values() if r['problems']]
        self.assertEqual(len(bad), 1)
        self.assertTrue(any('eden_log_2.txt' in p for p in bad[0]['problems']))
        self.assertEqual(len(out['verdicts']['X gpu_busy_ms']['item_runs']), 4)

    def test_run_problems(self):
        plan = fountain_plan()
        cases = {
            'settings': (dict(settings={'Renderer.hdr_mode': 'Auto'}), 'setting hdr_mode is Auto'),
            'build': (dict(build='ffffffffff'), 'build is not 0123456789'),
            'power': (dict(power_mode='balanced'), 'power modes'),
            'crash': (dict(crash=True), 'host crash'),
            'pipelines': (dict(xlate=(150.0,)), 'pipeline work in the window'),
            'probe': (dict(probe=3100), 'GPU clock probe 3100'),
            'draws': (dict(draws=2500), 'draws a frame'),
            'game': (dict(program='0100152000022000'), 'game is 0100152000022000'),
        }
        for name, (kw, want) in cases.items():
            with self.subTest(name):
                def change(i, item, kw=kw):
                    if i == 4:
                        arm, _, info, extra = item
                        return arm, run_log(arm, 40 + i, **kw), info, extra
                d = os.path.join(self.dir, name); os.makedirs(d)
                out, _, _ = analyze(plan, write_session(d, plan, standard_runs(plan, change)))
                probs = [p for r in out['runs'].values() for p in r['problems']]
                self.assertTrue(any(want in p for p in probs), (name, probs))

    def test_cpu_band_flags(self):
        plan = fountain_plan()

        def change(i, item):
            if i == 3:
                arm, _, info, extra = item
                return arm, run_log(arm, 43, pkg_w=48.0), info, extra
        out, _, _ = analyze(plan, write_session(self.dir, plan, standard_runs(plan, change)))
        flags = [f for r in out['runs'].values() for f in r['flags']]
        self.assertTrue(any('package W' in f for f in flags), flags)
        self.assertEqual(out['verdicts']['X gpu_busy_ms']['verdict'], 'ADOPT')

    def test_shortest_common_stay(self):
        plan = fountain_plan()

        def change(i, item):
            if i == 5:
                arm, _, info, extra = item
                return arm, run_log(arm, 45, stay=150.0), info, extra
        out, _, text = analyze(plan, write_session(self.dir, plan, standard_runs(plan, change)))
        w = out['window']
        self.assertEqual(w['planned_s'], 100)
        self.assertAlmostEqual(w['length_s'], 150.0 - 0.3 - 3.0 - 60.0 + 0.3, delta=1.1)
        self.assertLess(w['length_s'], 100)
        lengths = {round(r['values']['seconds'], 2) for r in out['runs'].values()}
        self.assertEqual(len(lengths), 1)

    def test_runner_flags_and_invalid(self):
        plan = fountain_plan()

        def change(i, item):
            arm, log, info, extra = item
            if i == 1:
                return arm, log, dict(info, flags=['the screen changed during the window (30 bits)'], quit={'killed': True},
                                      window_log=[10.0, 20.0]), extra
            if i == 8:
                return arm, log, dict(info, status='failed', reason='no screenshot after the window (minimized?)'), extra
        out, _, text = analyze(plan, write_session(self.dir, plan, standard_runs(plan, change)))
        labs = list(out['runs'])
        self.assertIn('killed at quit', out['runs'][labs[1]]['flags'])
        self.assertTrue(any('outside the runner' in f for f in out['runs'][labs[1]]['flags']))
        self.assertTrue(any("runner's status is failed: no screenshot" in p for p in out['runs'][labs[8]]['problems']))
        self.assertTrue(any('block 1 dropped' in n for n in out['notes']), text)

    def test_design_and_runner_dropped_block(self):
        plan = fountain_plan(design={'pair': ['N', 'X'], 'blocks': 2})
        del plan['blocks']
        self.assertEqual(A.plan_blocks(plan), [['N', 'X', 'X', 'N'], ['X', 'N', 'N', 'X']])

        def change(i, item):
            arm, log, info, extra = item
            if i >= 5:
                return arm, log, dict(info, dropped=True), extra
        out, _, text = analyze(plan, write_session(self.dir, plan, standard_runs(plan, change), as_zip=False))
        self.assertTrue(any('block 1 dropped' in n for n in out['notes']), text)
        self.assertEqual(len(out['verdicts']['X gpu_busy_ms']['item_runs']), 2)
        self.assertIn('spike_free_frame_ms', next(iter(out['runs'].values()))['values'])

    def test_shipped_plans(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plans')
        for name in sorted(os.listdir(here)) if os.path.isdir(here) else []:
            if name.endswith('.json'):
                with self.subTest(name), open(os.path.join(here, name), encoding='utf-8') as f:
                    plan = json.load(f)
                    self.assertEqual(A.plan_problems(plan), [])
                    if plan['kind'] == 'fountain':
                        self.assertTrue(A.plan_blocks(plan))

    def test_plan_problems(self):
        self.assertIn('the plan has no decision rules', A.plan_problems(fountain_plan(rules=[])))
        p = fountain_plan()
        p['arms']['X']['env']['EDEN_NX2_CLOCK_TRACE'] = '0'
        self.assertTrue(any('trace variable EDEN_NX2_CLOCK_TRACE' in b for b in A.plan_problems(p)))
        p = fountain_plan()
        p['arms']['X']['base'] = 'perf-trace.bat'
        self.assertTrue(any('base launcher' in b for b in A.plan_problems(p)))
        p = fountain_plan(rules=[dict(FOUNTAIN_RULES[0], item='Y')])
        self.assertTrue(any("'Y'" in b for b in A.plan_problems(p)))
        self.assertEqual(A.plan_problems(fountain_plan()), [])

    def test_given_plan_differs_from_the_zip(self):
        plan = fountain_plan()
        write_session(self.dir, plan, standard_runs(plan))
        other = fountain_plan(rules=[dict(FOUNTAIN_RULES[0], min_effect=0.01)])
        path = os.path.join(self.dir, 'other.json')
        with open(path, 'w') as f:
            json.dump(other, f)
        buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
        try:
            rc = A.main([path, '--zip', os.path.join(self.dir, 'session.zip'), '--json', os.path.join(self.dir, 'o.json')])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertIn('differs from the plan the runner ran in "rules"', buf.getvalue())
        with open(os.path.join(self.dir, 'o.json')) as f:
            self.assertEqual(json.load(f)['verdicts']['X gpu_busy_ms']['verdict'], 'ADOPT')

    def test_neighbour_rule_without_blocks(self):
        plan = fountain_plan(warmup=[], blocks=[])
        plan['runs'] = [dict(file=f'{i}.txt', arm=a, label=f'{a}{i}') for i, a in enumerate('NXXNXXN')]
        for i, a in enumerate('NXXNXXN'):
            with open(os.path.join(self.dir, f'{i}.txt'), 'w') as f:
                f.write(run_log(a, i + 1))
        out, _, text = analyze(plan, A.Source(logs=self.dir))
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(v['item_runs'], ['X1', 'X2', 'X4', 'X5'])
        self.assertEqual(v['control_runs'], ['N0', 'N6'])

    def test_partial_block_is_dropped(self):
        # The sitting stopped two runs into block 1: that block is unbalanced and must not count.
        plan = fountain_plan()
        runs = standard_runs(plan)[:1 + 4 + 2]
        out, _, text = analyze(plan, write_session(self.dir, plan, runs))
        self.assertTrue(any('block 1 dropped' in n and 'not a whole planned block' in n for n in out['notes']), text)
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(len(v['item_runs']), 2)
        self.assertEqual(v['verdict'], 'INCONCLUSIVE', text)
        self.assertIn('1 of 2 planned blocks', ' '.join(v['notes']))

    def test_appended_block_counts(self):
        # Block 0 dropped by the runner, block 2 appended with block 0's order: two whole blocks remain.
        plan = fountain_plan()
        runs = standard_runs(plan, lambda i, item: (item[0], item[1], dict(item[2], dropped=True), item[3]) if 1 <= i <= 4 else None)
        for s, arm in enumerate(plan['blocks'][0]):
            runs.append((arm, run_log(arm, 60 + s), dict(arm=arm, block=2, slot=10 + s), []))
        out, _, text = analyze(plan, write_session(self.dir, plan, runs))
        v = out['verdicts']['X gpu_busy_ms']
        self.assertEqual(v['verdict'], 'ADOPT', text)
        self.assertEqual(sorted(v['per_block']), ['1', '2'])

    def test_guard_without_data_blocks_adopt_only(self):
        rule = dict(FOUNTAIN_RULES[0])
        vals = {'i1': {'gpu_busy_ms': 14.8}, 'i2': {'gpu_busy_ms': 14.81}, 'c1': {'gpu_busy_ms': 15.2}, 'c2': {'gpu_busy_ms': 15.21}}
        v = A.decide_launch(rule, ['i1', 'i2'], ['c1', 'c2'], vals)
        self.assertEqual(v['verdict'], 'INCONCLUSIVE')
        self.assertEqual(v['guards_tripped'], [])
        self.assertIn('guard back_work_ms: no data', v['notes'])

    def test_more_run_checks(self):
        plan = fountain_plan()
        plan['arms']['X']['env']['LICHII_NOT_LISTED'] = '1'
        battery = run_log('N', 41).replace('on AC power', 'on battery power').replace('ac=on', 'ac=off')
        no_mode = '\n'.join(x for x in run_log('N', 42).splitlines() if 'LogPowerMode' not in x and 'power_mode=' not in x) + '\n'
        precache = run_log('N', 43, extra_lines=[F.line(20.0, 'Render.Vulkan', 'vk_pipeline_cache.cpp', 'PrecacheShaders',
                           'Shader precache: 3549 of 3996 compute programs and 120124 precompiled pipelines to build on 15 threads')])
        quiet = run_log('N', 44, extra_lines=[F.line(20.0, 'Render.Vulkan', 'vk_pipeline_cache.cpp', 'PrecacheShaders',
                        'Shader precache: 0 cached graphics pipelines and 0 of 3996 compute programs to build on 15 threads')])
        logs = {1: battery, 4: no_mode, 5: precache, 8: quiet}

        def change(i, item):
            if i in logs:
                return item[0], logs[i], item[2], item[3]
        runs = standard_runs(plan, change)
        runs[0] = (runs[0][0], precache, runs[0][2], [])  # the warm-up may precache
        out, _, text = analyze(plan, write_session(self.dir, plan, runs))
        r = list(out['runs'].values())
        self.assertIn('on battery', r[1]['problems'])
        self.assertTrue(any('not logged' in p for p in r[4]['problems']), r[4]['problems'])
        self.assertTrue(any('precache' in p for p in r[5]['problems']))
        self.assertFalse(any('precache' in p for p in r[8]['problems']), r[8]['problems'])
        self.assertFalse(r[0]['problems'], r[0]['problems'])
        self.assertTrue(any('precache' in f for f in r[0]['flags']))
        x = [y for y in r if y['arm'] == 'X' and not y['problems']]
        self.assertTrue(x and all(any('LICHII_NOT_LISTED is not on the TEST switches line' in f for f in y['flags']) for y in x))

    def test_settings_differ_flag(self):
        plan = fountain_plan()

        def change(i, item):
            if i == 6:
                return item[0], run_log(item[0], 46, settings={'Audio.volume': '50'}), item[2], item[3]
        out, _, _ = analyze(plan, write_session(self.dir, plan, standard_runs(plan, change)))
        flags = [f for r in out['runs'].values() for f in r['flags']]
        self.assertTrue(any('settings differ' in f and 'Audio.volume' in f for f in flags), flags)


LIVE_ITEMS = {"null": {"switch": "LICHII_AB_NULL", "a": "0", "b": "1", "units": 2, "segment_s": 30, "null": True},
              "burn": {"switch": "LICHII_AB_BURN_US", "a": "0", "b": "200", "units": 2, "segment_s": 30},
              "fast": {"switch": "LICHII_TEST_FAST", "a": "0", "b": "1", "units": 4, "segment_s": 30}}


def live_plan(**kw):
    p = {"name": "generated live", "kind": "live", "build": "0123456789",
         "arms": {"base": {"base": "perf-trace-noprofile.bat", "env": {}}},
         "items": LIVE_ITEMS, "margin_s": 2,
         "gates": {"probe_mhz": "sitting", "cpu_band_pct": 1.5, "draws": [2600, 3200]},
         "rules": [{"item": "fast", "metric": "frame_ms_ns", "better": "lower", "relative": True, "min_effect": 1.0,
                    "guards": [{"metric": "back_cpu_ms", "max_worse": 0.10}]},
                   {"item": "null", "metric": "frame_ms_ns", "relative": True, "kind": "aa", "max_ci": 1.0},
                   {"item": "burn", "metric": "back_cpu_ms", "kind": "recover", "expect": 0.2}]}
    p.update(kw)
    return p


def live_log(fast_pct=2.0, gone_in=None, flip_in=None, burn_ms=0.2):
    items = [(n, it['switch'], it['a'], it['b'], it['units']) for n, it in LIVE_ITEMS.items()]
    events, segs, value_at, t_end = F.live_schedule(200.0, items, gone_in=gone_in, flip_in=flip_in)

    def frame_ms(t):
        return 19.6 * (1 - fast_pct / 100.0 if value_at('LICHII_TEST_FAST', t) == '1' else 1.0)

    def back_cpu(t):
        return 15.0 + (burn_ms if value_at('LICHII_AB_BURN_US', t) == '200' else 0.0)
    text = F.fountain(seed=11, arrival=70, stay=t_end - 70 + 5, frame_ms=frame_ms, back_cpu=back_cpu, events=events)
    return text, segs


class Live(Tmp):
    @classmethod
    def setUpClass(cls):
        cls.text, cls.segs = live_log()

    def session(self, text):
        return write_session(self.dir, live_plan(), [('live', text, dict(arm='base'), [])])

    def test_verdicts(self):
        plan = live_plan()
        self.assertEqual(A.plan_problems(plan), [])
        out, _, text = analyze(plan, self.session(self.text))
        self.assertEqual(out['pairs'], {'null': 8, 'burn': 8, 'fast': 16}, text)
        v = out['verdicts']['fast frame_ms_ns']
        self.assertEqual(v['verdict'], 'ADOPT', text)
        self.assertAlmostEqual(v['gain'], 2.0, delta=0.3)
        self.assertEqual(v['pairs_better'], 16)
        self.assertIsNotNone(v['null_half_width'])
        self.assertEqual(out['verdicts']['null frame_ms_ns aa']['verdict'], 'PASS', text)
        b = out['verdicts']['burn back_cpu_ms recover']
        self.assertEqual(b['verdict'], 'RECOVERED', text)
        self.assertAlmostEqual(b['delta'], 0.2, delta=0.02)

    def test_segments(self):
        out, _, text = analyze(live_plan(), self.session(self.text))
        segs = out['segments']
        self.assertEqual(len(segs), len(self.segs))
        for s in segs:
            self.assertEqual(s['problems'], [], s['label'])
            # 25.2 s between the margins, cut to five 5.027 s periods.
            self.assertAlmostEqual(s['seconds'], 5 * 5.027, delta=0.011)
            self.assertIn('back_ghz', s); self.assertIn('pkg_w', s)
            self.assertLessEqual(s['spk_fsr5_n'], s['spk_fsr5_slots'] + 1)
            self.assertGreaterEqual(s['spk_fsr5_n'], 4)

    def test_no_effect_is_not_adopted(self):
        text, _ = live_log(fast_pct=0.0)
        out, _, report = analyze(live_plan(), self.session(text))
        self.assertEqual(out['verdicts']['fast frame_ms_ns']['verdict'], 'DROP', report)

    def test_client_gone_drops_the_unit(self):
        # Segment 1 is null's first B segment; the revert ends it early.
        text, _ = live_log(gone_in=1)
        out, _, report = analyze(live_plan(), self.session(text))
        self.assertTrue(any('null unit 0 dropped' in n for n in out['notes']), report)
        self.assertEqual(out['pairs']['null'], 4)
        bad = [s for s in out['segments'] if s['problems']]
        self.assertTrue(any('client gone' in p for s in bad for p in s['problems']))

    def test_flip_inside_and_incomplete(self):
        # Segment 33 lies in fast's first unit (segments 32-39).
        text, _ = live_log(flip_in=33)
        out, _, report = analyze(live_plan(), self.session(text))
        self.assertEqual(out['pairs']['fast'], 12)
        v = out['verdicts']['fast frame_ms_ns']
        self.assertNotEqual(v['verdict'], 'ADOPT', report)
        self.assertIn('12 of 16 planned pairs', ' '.join(v['notes']))

    def test_early_stop(self):
        plan = live_plan()
        plan['rules'][0] = dict(plan['rules'][0], early_stop=True, min_effect=0.5)
        text, _ = live_log(fast_pct=3.0, flip_in=33)
        # Two units of four valid is not the early-stop point (it needs exactly half), so no ADOPT.
        out, _, _ = analyze(plan, self.session(text))
        self.assertNotEqual(out['verdicts']['fast frame_ms_ns']['verdict'], 'ADOPT')
        v = A.decide_live(dict(plan['rules'][0], guards=[]), [dict(item={'frame_ms_ns': 19.0}, control={'frame_ms_ns': 19.6 + 0.01 * i})
                                                   for i in range(8)], planned_pairs=16, null_half=0.1)
        self.assertEqual(v['verdict'], 'ADOPT')

    def test_settle_counts_from_the_flip(self):
        # A runner that marks the start at the flip itself: the analyzer still skips 5 s after it.
        items = [(n, it['switch'], it['a'], it['b'], it['units']) for n, it in LIVE_ITEMS.items()]
        events, segs, _, t_end = F.live_schedule(200.0, items, settle=0.0, margin=2.0, measure=25.2)
        text = F.fountain(seed=12, arrival=70, stay=t_end - 70 + 5, events=events)
        out, _, report = analyze(live_plan(), self.session(text))
        by = {s['seg']: s for s in out['segments']}
        self.assertAlmostEqual(by[1]['window'][0], segs[1][3] + 5.0, delta=0.01)  # B after A: flipped at its start
        self.assertAlmostEqual(by[1]['seconds'], 4 * 5.027, delta=0.011)
        self.assertAlmostEqual(by[2]['window'][0], segs[2][3] + 2.0, delta=0.01)  # B after B: no flip
        self.assertAlmostEqual(by[2]['seconds'], 5 * 5.027, delta=0.011)

    def test_early_stop_needs_the_first_half_only(self):
        rule = dict(live_plan()['rules'][0], guards=[], early_stop=True, min_effect=0.5)
        pairs = [dict(item={'frame_ms_ns': 19.0}, control={'frame_ms_ns': 19.6 + 0.01 * i}) for i in range(8)]
        self.assertEqual(A.decide_live(rule, pairs, 16, 0.1, units_run=2)['verdict'], 'ADOPT')
        # Two valid units out of four run (two dropped) is not an early stop.
        self.assertNotEqual(A.decide_live(rule, pairs, 16, 0.1, units_run=4)['verdict'], 'ADOPT')

    def test_r67_switch_lines(self):
        # The exact lines r67's nx2_debug_tools.cpp writes (SwitchChangeLine, RevertClientSwitches, RestoreBootSwitches).
        R = A.parse(['[  10.000000] Debug <Info> core/debugger/nx2_debug_tools.cpp:1064:HandleSwitch: Live switch LICHII_AB_NULL: 0 -> 1 at present 11',
                     '[  11.000000] Debug <Info> core/debugger/nx2_debug_tools.cpp:1064:HandleSwitch: Live switch LICHII_AB_NULL: 1 -> 2 at present 12 (kept after the client leaves)',
                     '[  12.000000] Debug <Info> core/debugger/nx2_debug_tools.cpp:1083:RevertClientSwitches: Live switch LICHII_AB_NULL: 2 -> 0 (client gone, present 20)',
                     '[  13.000000] Debug <Info> core/debugger/nx2_debug_tools.cpp:1100:RestoreBootSwitches: Live switch EDEN_NX2_EXP12: 5 -> 0 (session end, present 26)'])
        self.assertEqual([(f['old'], f['new'], f['present'], f['gone'], f['end']) for f in R['flips']],
                         [('0', '1', 11, False, False), ('1', '2', 12, False, False), ('2', '0', 20, True, False), ('5', '0', 26, False, True)])

    def test_power_switch_needs_long_segments(self):
        items = dict(LIVE_ITEMS, burn=dict(LIVE_ITEMS['burn'], power=True))
        self.assertTrue(any('90 s' in b for b in A.plan_problems(live_plan(items=items))))


R66 = os.environ.get('LICHII_BENCH_R66_LOGS')
R66_PLAN = {
    "name": "r66 sitting 1", "kind": "fountain", "build": "bfe83e2de4", "window": {"start_s": 2},
    "arms": {"N": {"switches": {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": "off", "LICHII_TFB_BARRIER_LAZY": "off"}},
             "A43": {"switches": {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": "on"}, "expect_lines": ["Float atomic add: native"]},
             "A44": {"switches": {"LICHII_TFB_BARRIER_LAZY": "on"}}, "SH": {}},
    "runs": [{"file": "36c131bc-no_profile_1.txt", "arm": "N", "label": "N1"},
             {"file": "6e9e0cdc-ab_43_1.txt", "arm": "A43", "label": "A43-1"},
             {"file": "d5ced9da-ab_43_2.txt", "arm": "A43", "label": "A43-2"},
             {"file": "1a19dd5b-no_profile_2.txt", "arm": "N", "label": "N2"},
             {"file": "c471eeec-ab_44_1.txt", "arm": "A44", "label": "A44-1"},
             {"file": "d7724ef8-ab_44_2.txt", "arm": "A44", "label": "A44-2"},
             {"file": "1725639d-no_profile_3.txt", "arm": "N", "label": "N3"},
             {"file": "cc9a5cad-spike_hunt.txt", "arm": "SH", "label": "SPIKE"}],
    "gates": {"probe_mhz": "sitting", "cpu_band_pct": 1.5, "draws": [2600, 3200]},
    "rules": [{"item": "A43", "control": "N", "metric": "gpu_busy_ms", "better": "lower", "min_effect": 0.10,
               "guards": [{"metric": "back_work_ms", "max_worse": 0.10}]},
              {"item": "A44", "control": "N", "metric": "gpu_busy_ms", "better": "lower", "min_effect": 0.05,
               "guards": [{"metric": "back_work_ms", "max_worse": 0.10}]}]}
# HANDOFF's r66 sitting 1 table: window, draws, fps, 1% low, GPU busy median/mean, back, watched, probe.
R66_TABLE = {
    'N1': ((80.0, 195.6), 2748, 50.55, 26.3, 15.236, 15.273, 14.461, 1.097, 1797),
    'A43-1': ((77.0, 192.6), 2751, 50.77, 26.5, 15.072, 15.038, 14.604, 0.914, 1767),
    'A43-2': ((77.0, 192.6), 2752, 50.22, 27.1, 15.136, 15.047, 14.650, 0.918, 1770),
    'N2': ((69.0, 184.6), 2752, 51.01, 27.1, 15.095, 15.031, 14.539, 1.089, 1796),
    'A44-1': ((75.0, 190.6), 2751, 50.48, 26.9, 15.338, 15.273, 14.722, 1.082, 1767),
    'A44-2': ((70.0, 185.6), 2750, 50.76, 26.1, 15.190, 15.117, 14.685, 1.086, 1774),
    'N3': ((103.0, 218.6), 2749, 50.17, 27.1, 15.358, 15.327, 14.663, 1.086, 1769),
}


@unittest.skipUnless(R66 and os.path.isdir(R66), 'set LICHII_BENCH_R66_LOGS to the folder with the r66 sitting 1 logs')
class R66Sitting1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.values = A.analyze(R66_PLAN, A.Source(logs=R66))

    def test_table(self):
        for lab, (win, draws, fps, low1, med, mean, back, watch, probe) in R66_TABLE.items():
            with self.subTest(lab):
                v = self.values[lab]
                self.assertEqual(self.out['runs'][lab]['problems'], [])
                self.assertEqual(tuple(v['window']), win)
                self.assertEqual(round(v['draws_pf']), draws)
                self.assertEqual(round(v['fps'], 2), fps)
                self.assertEqual(round(v['low1_fps'], 1), low1)
                self.assertEqual(round(v['gpu_busy_ms'], 3), med)
                self.assertEqual(round(v['gpu_busy_mean_ms'], 3), mean)
                self.assertEqual(round(v['back_work_ms'], 3), back)
                self.assertEqual(round(v['watch_ms'], 3), watch)
                self.assertLessEqual(abs(v['probe_mhz'] - probe), 0.5)
        k = self.values['N1']['watch']
        self.assertEqual([round(k[h]['ms'], 3) for h in ('45384f519a8eacd3', '1e1964184a291f00', 'b712c7d7ddd57755', '75d4369f3b1f5868')],
                         [0.365, 0.176, 0.195, 0.361])

    def test_verdicts(self):
        v43 = self.out['verdicts']['A43 gpu_busy_ms']; v44 = self.out['verdicts']['A44 gpu_busy_ms']
        self.assertEqual((v43['verdict'], v44['verdict']), ('DROP', 'DROP'))
        self.assertEqual(v43['control_runs'], ['N1', 'N2']); self.assertEqual(v44['control_runs'], ['N2', 'N3'])
        self.assertAlmostEqual(v43['item_mean'] - v43['control_mean'], -0.062, delta=0.0006)
        self.assertAlmostEqual(v43['control_spread'], 0.141, delta=0.0006)
        self.assertAlmostEqual(v44['item_mean'] - v44['control_mean'], 0.037, delta=0.0006)
        back43 = A.st.mean(self.values[x]['back_work_ms'] for x in ('A43-1', 'A43-2')) - A.st.mean(self.values[x]['back_work_ms'] for x in ('N1', 'N2'))
        back44 = A.st.mean(self.values[x]['back_work_ms'] for x in ('A44-1', 'A44-2')) - A.st.mean(self.values[x]['back_work_ms'] for x in ('N2', 'N3'))
        self.assertAlmostEqual(back43, 0.127, delta=0.0006)
        self.assertAlmostEqual(back44, 0.102, delta=0.0006)
        self.assertTrue(v43['guards_tripped']); self.assertTrue(v44['guards_tripped'])

    def test_hitches(self):
        for lab, v in self.values.items():
            with self.subTest(lab):
                self.assertTrue(5.024 <= v['spk_fsr5_period'] <= 5.032, v['spk_fsr5_period'])
                self.assertTrue(24.0 <= v['spk_fsr5_ms'] <= 28.0)
                self.assertTrue(21 <= v['spk_fsr5_n'] <= 27)
                # Without the 5 s hitch the 1% low would be 32-35 fps, not 26-28.
                self.assertTrue(32.0 <= v['low1_ns'] <= 36.0)
                if lab == 'SPIKE':
                    self.assertEqual(v['spk_trace4_n'], 0)  # spike-hunt has no perf-trace lines
                else:
                    self.assertTrue(4.150 <= v['spk_trace4_period'] <= 4.165, v['spk_trace4_period'])
                    self.assertTrue(18 <= v['spk_trace4_n'] <= 28)

    def test_spike_hunt_stay(self):
        out = self.out['runs']['SPIKE']
        self.assertEqual(self.values['SPIKE']['arrival_s'], 69.0)
        self.assertTrue(any('frame rate only' in f for f in out['flags']))
        with open(os.path.join(R66, 'cc9a5cad-spike_hunt.txt'), encoding='utf-8', errors='replace') as f:
            R = A.parse(f)
        s, e = 71.0, round(A.end_of(R) - 3.0, 1)
        self.assertEqual(e, 189.8)
        m = A.metrics(R, s, e)
        self.assertEqual((round(m['fps'], 2), round(m['low1_fps'], 1)), (50.64, 27.3))


if __name__ == '__main__':
    unittest.main()
