import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import benchplan  # noqa: E402
import fakes  # noqa: E402

PLANS = os.path.join(os.path.dirname(HERE), "plans")

# r66's perf-trace-noprofile.bat without its comment lines.
NOPROFILE = ("@echo off\r\nrem The default settings with only the frame, heap and clock traces\r\n"
             "set EDEN_DKB_FRAME_TRACE=1&& set EDEN_NX2_HEAP_TRACE=1&& set EDEN_NX2_CLOCK_TRACE=1&& "
             "set EDEN_NX2_GPU_CLOCK_PROBE=1&& set EDEN_NX2_WFI_STATS=1&& "
             "set EDEN_NX2_GPU_WORK_WATCH=45384f519a8eacd3,1e1964184a291f00,b712c7d7ddd57755,75d4369f3b1f5868\r\n"
             'start "" "%~dp0lichii.exe" %*\r\n')
# r66's crash-trace.bat: its echo line is one the bench can't read.
CRASH_TRACE = ('@echo off\r\nset "EDEN_NX2_PIPELINE_BUILD_TRACE=%~dp0pipeline-builds.txt"\r\n'
               'start "" /wait "%~dp0lichii.exe" %*\r\n>>"%~dp0pipeline-builds.txt" echo EXITCODE %ERRORLEVEL%\r\n')


def load(name):
    with open(os.path.join(PLANS, name), encoding="utf-8") as stream:
        return json.load(stream)


class LauncherTest(unittest.TestCase):
    def test_reads_the_noprofile_launcher(self):
        env, exe = benchplan.parse_launcher(NOPROFILE, "C:\\b", "perf-trace-noprofile.bat")
        self.assertEqual(exe, "lichii.exe")
        self.assertEqual(env["EDEN_DKB_FRAME_TRACE"], "1")
        self.assertEqual(env["EDEN_NX2_GPU_WORK_WATCH"].split(",")[0], "45384f519a8eacd3")
        self.assertEqual(len(env), 6)

    def test_quoted_set_and_dp0(self):
        env, _ = benchplan.parse_launcher('set "EDEN_NX2_PIPELINE_BUILD_TRACE=%~dp0p.txt"\r\nstart "" "%~dp0lichii.exe" %*\r\n',
                                          "C:\\Build Folder", "x.bat")
        self.assertEqual(env["EDEN_NX2_PIPELINE_BUILD_TRACE"], "C:\\Build Folder\\p.txt")

    def test_refuses_other_lines(self):
        with self.assertRaises(benchplan.PlanError) as ctx:
            benchplan.parse_launcher(CRASH_TRACE, "C:\\b", "crash-trace.bat")
        self.assertIn("crash-trace.bat line 4", str(ctx.exception))
        with self.assertRaises(benchplan.PlanError):
            benchplan.parse_launcher('set A=%PATH%\r\nstart "" "%~dp0lichii.exe" %*', "C:\\b")
        with self.assertRaises(benchplan.PlanError):
            benchplan.parse_launcher('set EDEN_NX2_DEBUG_SERVER=1.2.3.4:1\r\nstart "" "%~dp0lichii.exe" %*', "C:\\b")
        with self.assertRaises(benchplan.PlanError):
            benchplan.parse_launcher("set A=1\r\n", "C:\\b")

    def test_scrubbed_environment(self):
        env = benchplan.scrubbed_environ({"PATH": "x", "EDEN_NX2_KSPIN": "0", "lichii_tfb_barrier_lazy": "1", "TEMP": "t"})
        self.assertEqual(env, {"PATH": "x", "TEMP": "t"})


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.build = self.tmp.name
        with open(os.path.join(self.build, "perf-trace-noprofile.bat"), "w") as stream:
            stream.write(NOPROFILE)
        with open(os.path.join(self.build, "trace-other.bat"), "w") as stream:
            stream.write(fakes.PERF_TRACE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_plans_load(self):
        for name in ("aa-fountain-r66.json", "r66-replicate-sitting1.json"):
            plan = benchplan.load_plan(load(name), self.build)
            self.assertEqual(plan["build"], "bfe83e2de4")

    def test_aa_schedule(self):
        slots = benchplan.schedule(load("aa-fountain-r66.json"))
        self.assertEqual([s["arm"] for s in slots], ["N-a", "N-a", "N-b", "N-b", "N-a", "N-b", "N-a", "N-a", "N-b"])
        self.assertEqual([s["warmup"] for s in slots], [True] + [False] * 8)
        self.assertEqual([s["block"] for s in slots], [None, 0, 0, 0, 0, 1, 1, 1, 1])
        self.assertEqual([s["slot"] for s in slots], list(range(1, 10)))

    def test_replication_schedule(self):
        plan = load("r66-replicate-sitting1.json")
        slots = benchplan.schedule(plan)
        self.assertEqual(len(slots), 18)
        self.assertEqual([s["arm"] for s in slots[:2]], ["N", "A43"])
        measured = [s["arm"] for s in slots if not s["warmup"]]
        self.assertEqual(measured.count("N"), 8)
        self.assertEqual(measured.count("A43"), 4)
        self.assertEqual(measured.count("A44"), 4)
        for b in range(4):
            arms = [s["arm"] for s in slots if s["block"] == b]
            self.assertEqual(arms[0], arms[3])
            self.assertEqual(arms[1], arms[2])
        self.assertAlmostEqual(benchplan.estimate_minutes(benchplan.load_plan(plan, self.build)), 85, delta=10)

    def test_balanced_blocks_alternate(self):
        self.assertEqual(benchplan.balanced_blocks("A", "B", 3),
                         [["A", "B", "B", "A"], ["B", "A", "A", "B"], ["A", "B", "B", "A"]])

    def test_expected_switches(self):
        plan = load("r66-replicate-sitting1.json")
        self.assertEqual(benchplan.expected_switches(plan["arms"]["A43"]), {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": "on"})
        self.assertEqual(benchplan.expected_switches({"env": {"EDEN_NX2_KSPIN": "0", "EDEN_NX2_PREALLOC_MIB": "2048"}}),
                         {"EDEN_NX2_KSPIN": "off", "EDEN_NX2_PREALLOC_MIB": "2048"})

    def refused(self, plan, text):
        with self.assertRaises(benchplan.PlanError) as ctx:
            benchplan.load_plan(plan, self.build)
        self.assertIn(text, str(ctx.exception))

    def test_refuses_a_plan_without_rules(self):
        plan = load("aa-fountain-r66.json")
        plan.pop("rules")
        self.refused(plan, "no decision rules")
        plan["rules"] = []
        self.refused(plan, "no decision rules")

    def test_refuses_arms_differing_in_trace_variables(self):
        plan = load("aa-fountain-r66.json")
        plan["arms"]["N-b"]["base"] = "trace-other.bat"
        self.refused(plan, "differ in trace variables: EDEN_NX2_GPU_WORK_WATCH")
        plan = load("aa-fountain-r66.json")
        plan["arms"]["N-b"]["env"] = {"EDEN_NX2_GPU_PROFILE": "1"}
        self.refused(plan, "EDEN_NX2_GPU_PROFILE")

    def test_same_trace_variable_in_every_arm_is_fine(self):
        plan = load("aa-fountain-r66.json")
        for arm in plan["arms"].values():
            arm["env"] = {"EDEN_NX2_BUFFER_TRACE": "1"}
        benchplan.load_plan(plan, self.build)

    def test_refuses_a_change_the_log_doesnt_show(self):
        plan = load("r66-replicate-sitting1.json")
        plan["arms"]["A44"]["env"] = {"LICHII_WFI_DEFER": "1"}
        self.refused(plan, "LICHII_WFI_DEFER doesn't show in the TEST switches line")
        plan["arms"]["A44"]["expect_lines"] = ["WFI defer on"]
        benchplan.load_plan(plan, self.build)

    def test_refuses_bad_rules_and_arms(self):
        plan = load("r66-replicate-sitting1.json")
        bad = copy.deepcopy(plan)
        bad["rules"][0]["item"] = "A45"
        self.refused(bad, "item 'A45' is not an arm")
        bad = copy.deepcopy(plan)
        bad["rules"][0]["metric"] = "speed"
        self.refused(bad, "metric 'speed' unknown")
        bad = copy.deepcopy(plan)
        bad["rules"][0]["min_effect"] = 0
        self.refused(bad, "min_effect")
        bad = copy.deepcopy(plan)
        bad["blocks"] = [["N", "N", "N", "N"]]
        self.refused(bad, "arm A43 has no measured run")
        bad = copy.deepcopy(plan)
        bad["arms"]["N"]["env"] = {"EDEN_NX2_DEBUG_SERVER": "x"}
        self.refused(bad, "the bench sets it itself")
        bad = copy.deepcopy(plan)
        bad["kind"] = "live"
        self.refused(bad, "fresh-launch fountain sittings only")
        bad = copy.deepcopy(plan)
        bad["gates"]["probe_mhz"] = [3200, 3000]
        self.refused(bad, "probe_mhz")

    def test_settings_gate(self):
        gate = benchplan.settings_gate(load("r66-replicate-sitting1.json"))
        self.assertEqual(gate["force_max_clock"], "true")
        self.assertEqual(gate["nx2_frame_rate_limit"], "120")
        self.assertEqual(gate["hdr_mode"], "Off")


if __name__ == "__main__":
    unittest.main()
