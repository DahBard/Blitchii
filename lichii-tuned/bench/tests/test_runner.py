import json
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import benchplan  # noqa: E402
import fakes  # noqa: E402
import oot_bench  # noqa: E402
import winhost  # noqa: E402
from test_plan import NOPROFILE  # noqa: E402


def small_plan(**over):
    plan = {"name": "test A/A", "kind": "fountain", "build": "bfe83e2de4",
            "arms": {"N-a": {"base": "perf-trace-noprofile.bat", "env": {}},
                     "N-b": {"base": "perf-trace-noprofile.bat", "env": {}}},
            "warmup": ["N-a"], "design": {"pair": ["N-a", "N-b"], "blocks": 1},
            "settle_s": 20, "measure_s": 25, "cooldown_s": 5, "warmup_measure_s": 5,
            "gates": {"probe_mhz": "sitting", "draws": [2600, 3200]},
            "rules": [{"item": "N-b", "control": "N-a", "metric": "gpu_busy_ms", "better": "lower",
                       "min_effect": 0.1}]}
    plan.update(over)
    return plan


class FakeWorldCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.build = os.path.join(root, "build")
        os.makedirs(self.build)
        with open(os.path.join(self.build, "perf-trace-noprofile.bat"), "w") as stream:
            stream.write(NOPROFILE)
        self.world = fakes.World(os.path.join(root, "data", "log"))
        self.host = fakes.FakeHost(self.world)
        self.local = {"build_dir": self.build, "exe": "lichii.exe", "game_file": os.path.join(root, "oot.nsp"),
                      "log_dir": self.world.log_dir, "data_root": os.path.join(root, "data"),
                      "config_file": os.path.join(root, "data", "config", "qt-config.ini"),
                      "hotkeys": oot_bench.DEFAULT_HOTKEYS, "game_title": "Ocarina of Time"}
        self.session = os.path.join(root, "session")
        os.makedirs(os.path.join(self.session, "runs"))
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, plan, refs=None, ask=None):
        plan = benchplan.load_plan(plan, self.build)
        oot_bench.write_json(os.path.join(self.session, "plan.json"), plan)
        r = oot_bench.Runner(self.host, self.local, plan, self.session, client_factory=lambda: fakes.FakeClient(self.world),
                             refs=fakes.refs_for_flow() if refs is None else refs, say=self.lines.append, ask=ask)
        r.threaded = False
        return r


class SittingTest(FakeWorldCase):
    def test_a_whole_sitting(self):
        runner = self.runner(small_plan())
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertIsNone(stopped, self.lines)
        self.assertEqual([r["label"] for r in results], ["01-W-N-a", "02-N-a", "03-N-b", "04-N-b", "05-N-a"])
        for r in results:
            self.assertEqual(r["status"], "ok", (r["label"], r.get("reason"), r.get("problems")))
            self.assertEqual(r["quit"]["confirm_answered"], 1)
            self.assertFalse(r["quit"]["killed"])
            self.assertEqual(r["quit"]["exit_code"], 0)
            self.assertTrue(os.path.exists(os.path.join(self.session, "runs", r["label"], "eden_log.txt")))
        measured = results[1]
        self.assertAlmostEqual(measured["values"]["draws_pf"], 2800, delta=1)
        self.assertAlmostEqual(measured["server_fps"], 50, delta=0.5)
        self.assertEqual(len(measured["frame_times"]), 1)
        s, e = measured["window_log"]
        self.assertAlmostEqual(e - s, 25, delta=0.6)
        self.assertGreater(s, measured["arrival_log_t"] + 19.5)
        # Only A was pressed, and the render window was never closed.
        self.assertTrue(all(p[2] == ("A",) for p in self.world.presses))
        self.assertNotIn(2, self.world.closes)
        cmd = self.host.cmd
        self.assertEqual(cmd[1:3], ["-f", "-g"])
        self.assertEqual(self.world.env["EDEN_DKB_FRAME_TRACE"], "1")
        self.assertEqual(self.world.env["EDEN_NX2_DEBUG_SERVER"], "127.0.0.1:6545")
        self.assertEqual(self.world.env["LICHII_AB_ARM"], "N-a")
        text = oot_bench.finish(self.session, runner.plan, results, notes, stopped, [])
        self.assertIn("per block", text)
        self.assertIn("N-b: 2 runs", text)
        z = oot_bench.make_zip(self.session, os.path.join(self.tmp.name, "results"))
        names = zipfile.ZipFile(z).namelist()
        self.assertIn("session/runs/02-N-a/eden_log.txt", names)
        self.assertIn("session/summary.txt", names)
        self.assertIn("session/runs/02-N-a/shots/arrival.png", names)

    def test_the_closing_dialog_is_not_taken_for_the_confirm_box(self):
        # Lichii's frameless "Closing software..." window also carries the native title "Lichii".
        self.host.shutdown_dialog = True
        runner = self.runner(small_plan(warmup=[], design={"pair": ["N-a", "N-b"], "blocks": 1}))
        record = runner.run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertEqual(record["status"], "ok", record.get("reason"))
        self.assertEqual(record["quit"]["confirm_answered"], 1)
        self.assertEqual([k for k in self.host.keys if k[0] == 4], [])

    def test_wrong_switch_stops_the_sitting(self):
        self.world.switch_override = {"LICHII_TFB_BARRIER_LAZY": "on"}
        plan = small_plan(arms={"N": {"base": "perf-trace-noprofile.bat", "env": {},
                                      "switches": {"LICHII_TFB_BARRIER_LAZY": "off"}},
                                "A44": {"base": "perf-trace-noprofile.bat", "env": {"LICHII_TFB_BARRIER_LAZY": "1"}}},
                          warmup=[], design={"pair": ["N", "A44"], "blocks": 1},
                          rules=[{"item": "A44", "control": "N", "metric": "gpu_busy_ms", "better": "lower",
                                  "min_effect": 0.1}])
        runner = self.runner(plan)
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertIn("LICHII_TFB_BARRIER_LAZY on, not off", stopped)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "fatal")
        self.assertFalse(self.world.alive)

    def test_a_failed_navigation_is_retried_in_its_slot(self):
        self.world.nav_fail_launches = {2}
        script = oot_bench.read_json(os.path.join(os.path.dirname(HERE), "oot_boot.json"))
        script["steps"][0]["timeout_s"] = 40
        runner = self.runner(small_plan())
        runner.script = script
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertIsNone(stopped)
        self.assertEqual([(r["label"], r["status"], r["slot"], r["attempt"]) for r in results][:3],
                         [("01-W-N-a", "ok", 1, 1), ("02-N-a", "failed", 2, 1), ("03-N-a", "ok", 2, 2)])
        self.assertIn("navigation", results[1]["reason"])
        self.assertEqual(len(results), 6)

    def test_f3_aborts_and_closes_lichii(self):
        runner = self.runner(small_plan())
        self.world.f3_at = self.world.t + 60
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertTrue(stopped.startswith("aborted"), stopped)
        self.assertEqual(results[-1]["status"], "aborted")
        self.assertFalse(self.world.alive)
        self.assertTrue(os.path.exists(os.path.join(self.session, "runs", "01-W-N-a", "eden_log.txt")))

    def test_a_stop_during_the_cooldown_keeps_the_run(self):
        runner = self.runner(small_plan(cooldown_s=30))
        runner.plan["warmup_measure_s"] = 5
        # F3 held during the first cooldown: the warm-up's record must still be in the results.
        real_sleep = runner.clock.sleep

        def sleep(seconds):
            if seconds == 30 and self.world.f3_at is None:
                self.world.f3_at = self.world.t + 1
            real_sleep(seconds)
        runner.clock.sleep = sleep
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertTrue(stopped.startswith("aborted"), stopped)
        self.assertEqual([(r["label"], r["status"]) for r in results], [("01-W-N-a", "ok")])

    def test_without_the_confirm_box(self):
        self.world.confirm = False
        runner = self.runner(small_plan(warmup=[], design={"pair": ["N-a", "N-b"], "blocks": 1}))
        record = runner.run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertEqual(record["status"], "ok", record.get("reason"))
        self.assertEqual(record["quit"]["confirm_answered"], 0)

    def test_missing_references_stop_the_sitting_and_close_lichii(self):
        runner = self.runner(small_plan(), refs={})
        results, notes, stopped = oot_bench.run_schedule(runner.plan, runner.run_one, say=self.lines.append)
        self.assertTrue(stopped.startswith("stopped: boot script"), stopped)
        self.assertIn("bench.bat check", stopped)
        self.assertFalse(self.world.alive)

    def test_a_held_log_invalidates_the_run(self):
        runner = self.runner(small_plan())
        second = os.path.join(self.world.log_dir, "eden_log_2.txt")
        real_launch = self.host.launch

        def launch(cmd, env, cwd):
            p = real_launch(cmd, env, cwd)
            open(second, "w").close()
            self.world.mtimes[second] = self.world.t + 1
            return p
        self.host.launch = launch
        record = runner.run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertEqual(record["status"], "failed")
        self.assertIn("eden_log_2.txt", record["reason"])


class ProbeGateTest(FakeWorldCase):
    def gate_runner(self):
        # A probe line counts once its 10 s lie wholly after arrival, so the settle is 40 s here.
        runner = self.runner(small_plan(warmup=[], design={"pair": ["N-a", "N-b"], "blocks": 1}, settle_s=40))
        runner.probe_values = [3140]
        return runner

    def test_the_probe_covering_the_loading_screen_is_ignored(self):
        # r64: the first probe after arrival covered the load (3,299 MHz against ~3,140 at the
        # fountain). Here that line reads far off, and it must not stop the sitting.
        self.world.probe_at = lambda s: 3600 if s <= 50 else 3150
        record = self.gate_runner().run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertEqual(record["status"], "ok", record.get("reason"))

    def test_a_wrong_clock_state_stops_the_sitting(self):
        self.world.probe_mhz = 1790
        with self.assertRaises(oot_bench.Fatal) as caught:
            self.gate_runner().run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertIn("GPU clock probe 1790 MHz", str(caught.exception))
        self.assertFalse(self.world.alive)

    def test_a_small_difference_is_left_to_the_sitting_flags(self):
        self.world.probe_mhz = 3250  # +3.5%: over the 3% band, under the stop
        record = self.gate_runner().run_one({"arm": "N-a", "block": 0, "warmup": False, "slot": 1}, 1)
        self.assertEqual(record["status"], "ok", record.get("reason"))


class ScheduleRulesTest(unittest.TestCase):
    def plan(self):
        return benchplan.load_plan(small_plan(design={"pair": ["N-a", "N-b"], "blocks": 2}))

    def outcomes(self, fails):
        """run_one that fails the (slot, attempt) pairs in `fails`."""
        calls = []

        def run_one(slot, attempt):
            calls.append((slot["slot"], slot["block"], slot["arm"], attempt))
            ok = (slot["slot"], attempt) not in fails
            return {"label": f"{len(calls):02d}", "status": "ok" if ok else "failed", "reason": "x",
                    "block": slot["block"], "slot": slot["slot"], "arm": slot["arm"]}
        return calls, run_one

    def test_a_retry_in_the_same_slot(self):
        calls, run_one = self.outcomes({(3, 1)})
        results, notes, stopped = oot_bench.run_schedule(self.plan(), run_one, say=lambda s: None)
        self.assertIsNone(stopped)
        self.assertEqual([c[0] for c in calls], [1, 2, 3, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(notes, [])

    def test_a_slot_failing_twice_drops_its_block_and_appends_one(self):
        calls, run_one = self.outcomes({(3, 1), (3, 2)})
        results, notes, stopped = oot_bench.run_schedule(self.plan(), run_one, say=lambda s: None)
        self.assertIsNone(stopped)
        # Block 1's remaining slots are skipped; block 2 runs; a copy of block 1 is appended.
        self.assertEqual([c[:3] for c in calls if c[3] == 1][3:],
                         [(6, 1, "N-b"), (7, 1, "N-a"), (8, 1, "N-a"), (9, 1, "N-b"),
                          (10, 2, "N-a"), (11, 2, "N-b"), (12, 2, "N-b"), (13, 2, "N-a")])
        self.assertTrue(all(r.get("dropped") for r in results if r["block"] == 0))
        with tempfile.TemporaryDirectory() as tmp:
            for r in results:
                os.makedirs(os.path.join(tmp, "runs", r["label"]))
            oot_bench.finish(tmp, self.plan(), results, notes, stopped, [])
            first = oot_bench.read_json(os.path.join(tmp, "runs", "03", "run.json"))
            self.assertTrue(first["dropped"])
        self.assertIn("block 1 dropped", notes[0])

    def test_two_failed_slots_in_a_row_stop(self):
        calls, run_one = self.outcomes({(3, 1), (3, 2), (6, 1), (6, 2)})
        results, notes, stopped = oot_bench.run_schedule(self.plan(), run_one, say=lambda s: None)
        self.assertEqual(stopped, "stopped: two failed slots in a row")

    def test_extra_blocks_are_limited(self):
        plan = self.plan()
        plan["max_extra_blocks"] = 0
        calls, run_one = self.outcomes({(3, 1), (3, 2)})
        results, notes, stopped = oot_bench.run_schedule(plan, run_one, say=lambda s: None)
        self.assertIn("no more blocks", notes[0])
        self.assertEqual(max(c[0] for c in calls), 9)

    def test_fatal_stops_at_once(self):
        def run_one(slot, attempt):
            raise oot_bench.Fatal("power mode")
        results, notes, stopped = oot_bench.run_schedule(self.plan(), run_one, say=lambda s: None)
        self.assertEqual(stopped, "stopped: power mode")

    def test_ctrl_c_ends_the_schedule_with_what_it_has(self):
        calls = []

        def run_one(slot, attempt):
            calls.append(slot["slot"])
            if len(calls) == 3:
                raise KeyboardInterrupt
            return {"label": f"{len(calls):02d}", "status": "ok", "block": slot["block"], "slot": slot["slot"]}
        results, notes, stopped = oot_bench.run_schedule(self.plan(), run_one, say=lambda s: None)
        self.assertEqual(stopped, "aborted: Ctrl+C")
        self.assertEqual(len(results), 2)


class QuitTest(unittest.TestCase):
    def test_picks_the_main_window_not_the_render_window(self):
        wins = [{"hwnd": 2, "title": "Lichii tuned | r66-bfe83e2de4", "cls": "Qt6QWindowIcon", "visible": True},
                {"hwnd": 1, "title": "Lichii | 1 | clang | The Legend of Zelda: Ocarina of Time | AMD", "cls": "Qt6QWindowIcon"},
                {"hwnd": 5, "title": "IME", "cls": "IME", "visible": False}]
        self.assertEqual(winhost.pick_main_window(wins, "Ocarina of Time")["hwnd"], 1)
        self.assertEqual(winhost.pick_main_window(wins)["hwnd"], 1)
        self.assertIsNone(winhost.pick_confirm_dialog(wins))
        box = {"hwnd": 3, "title": "Lichii", "cls": "Qt6QWindowIcon", "visible": True}
        self.assertEqual(winhost.pick_confirm_dialog(wins + [box])["hwnd"], 3)
        self.assertIsNone(winhost.pick_confirm_dialog(wins + [dict(box, visible=False)]))

    def test_only_a_captioned_lichii_window_is_the_confirm_box(self):
        frameless = {"hwnd": 4, "title": "Lichii", "cls": "Qt6QWindowIcon", "visible": True, "style": 0x80000000}
        box = {"hwnd": 3, "title": "Lichii", "cls": "Qt6QWindowIcon", "visible": True, "style": 0x80C80000}
        self.assertIsNone(winhost.pick_confirm_dialog([frameless]))
        self.assertEqual(winhost.pick_confirm_dialog([frameless, box])["hwnd"], 3)

    def test_kill_after_the_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = fakes.World(tmp)
            host = fakes.FakeHost(world)
            proc = world.start({})
            world.advance(30)
            host.windows = lambda pid: []
            record = oot_bench.quit_lichii(host, proc, "Ocarina of Time", timeout_s=60)
            self.assertTrue(record["killed"])
            self.assertEqual(record["exit_code"], 1)

    def test_enter_after_three_unanswered_tries(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = fakes.World(tmp)
            host = fakes.FakeHost(world)
            proc = world.start({})
            world.advance(30)
            real = host.press_key

            def stubborn(win, vk, char=None):
                host.keys.append((win["hwnd"], vk))
                if vk == winhost.VK_RETURN:
                    real(win, 0x59)
            host.press_key = stubborn
            record = oot_bench.quit_lichii(host, proc, "Ocarina of Time")
            self.assertEqual([k for _, k in host.keys][:4], [0x59, 0x59, 0x59, winhost.VK_RETURN])
            self.assertFalse(record["killed"])

    def test_key_lparams(self):
        down, up = winhost.key_lparams(0x15)
        self.assertEqual(down, 0x150001)
        self.assertEqual(up, 0xC0150001)
        self.assertAlmostEqual(winhost.filetime_to_unix(116444736000000000), 0.0)


class ConfigTest(unittest.TestCase):
    INI = ("[UI]\nconfirmStop\\default=true\nconfirmStop=0\n"
           "Shortcuts\\Main%20Window\\Stop%20Emulation\\Controller_KeySeq=L+Plus+Minus\n"
           "Shortcuts\\Main%20Window\\Fullscreen\\Controller_KeySeq=Home+B\n"
           "Shortcuts\\Main%20Window\\Custom%20One\\Controller_KeySeq=A+Y\n"
           'Paths\\recentFiles="C:\\\\Games\\\\Ocarina of Time.nsp"\n'
           "[Controls]\nplayer_0_connected=true\n")

    def test_qt_ini(self):
        ini = oot_bench.parse_qt_ini(self.INI)
        self.assertEqual(ini["UI/confirmStop"], "0")
        self.assertEqual(ini["UI/Paths/recentFiles"], "C:\\Games\\Ocarina of Time.nsp")
        hotkeys = oot_bench.read_hotkeys(ini)
        self.assertEqual(hotkeys["Stop Emulation"], "L+Plus+Minus")
        self.assertEqual(hotkeys["Custom One"], "A+Y")
        self.assertEqual(hotkeys["Restart Emulation"], "R+Plus+Minus")

    def test_config_comparison_ignores_window_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "start"))
            with open(os.path.join(tmp, "start", "qt-config.ini"), "w") as stream:
                stream.write("[UI]\nfullscreen=false\nUILayout\\geometry=@ByteArray(1)\nconfirmStop=0\n")
            with open(os.path.join(tmp, "now.ini"), "w") as stream:
                stream.write("[UI]\nfullscreen=true\nUILayout\\geometry=@ByteArray(2)\nconfirmStop=2\n")
            lines = oot_bench.compare_configs(os.path.join(tmp, "start"), {"qt-config.ini": os.path.join(tmp, "now.ini")})
            self.assertEqual(lines, ["qt-config.ini: UI/confirmStop: '0' -> '2'"])


class ClockTest(unittest.TestCase):
    def test_f3_must_be_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = fakes.World(tmp)
            host = fakes.FakeHost(world)
            clock = oot_bench.Clock(host)
            presses = iter([True, False, True, False])
            host.f3_down = lambda: next(presses, False)
            clock.sleep(2.0)  # taps don't stop it
            host.f3_down = lambda: True
            with self.assertRaises(oot_bench.Aborted):
                clock.sleep(2.0)

    def test_control_file_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = fakes.World(tmp)
            path = os.path.join(tmp, "control.json")
            oot_bench.write_json(path, {"abort": True})
            with self.assertRaises(oot_bench.Aborted):
                oot_bench.Clock(fakes.FakeHost(world), path).sleep(10)


class ZipTest(unittest.TestCase):
    def test_only_results_go_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = os.path.join(tmp, "20261115-r66")
            for rel in ("plan.json", "summary.txt", "runs/01-N/eden_log.txt", "runs/01-N/run.json",
                        "runs/01-N/shots/end.png", "config-start/qt-config.ini", "runs/01-N/debug/x.bin", "local.json"):
                os.makedirs(os.path.dirname(os.path.join(s, rel)), exist_ok=True)
                open(os.path.join(s, rel), "w").close()
            names = sorted(zipfile.ZipFile(oot_bench.make_zip(s, os.path.join(tmp, "results"))).namelist())
            self.assertEqual(names, ["20261115-r66/plan.json", "20261115-r66/runs/01-N/eden_log.txt",
                                     "20261115-r66/runs/01-N/run.json", "20261115-r66/runs/01-N/shots/end.png",
                                     "20261115-r66/summary.txt"])


class CommandLineTest(unittest.TestCase):
    def test_validate_example_plans(self):
        for name in ("aa-fountain-r66.json", "r66-replicate-sitting1.json"):
            self.assertEqual(oot_bench.main(["validate", os.path.join(os.path.dirname(HERE), "plans", name)]), 0)

    def test_a_plan_without_rules_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            plan = small_plan()
            del plan["rules"]
            with open(path, "w") as stream:
                json.dump(plan, stream)
            self.assertEqual(oot_bench.main(["validate", path]), 2)


if __name__ == "__main__":
    unittest.main()
