"""Log checks, partly on the owner's real r66 logs. Those logs are never committed: the tests
that need them are skipped unless BENCH_R66_LOGS (or the session's upload folder) has them."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import benchplan  # noqa: E402
import logscan  # noqa: E402

LOGS = os.environ.get("BENCH_R66_LOGS", "/root/.claude/uploads/e57b64ae-d8d0-5bc0-9be5-b5fd2191474e")
SITTING1 = {"N1": "36c131bc-no_profile_1.txt", "A43-1": "6e9e0cdc-ab_43_1.txt", "A43-2": "d5ced9da-ab_43_2.txt",
            "N2": "1a19dd5b-no_profile_2.txt", "A44-1": "c471eeec-ab_44_1.txt", "A44-2": "d7724ef8-ab_44_2.txt",
            "N3": "1725639d-no_profile_3.txt"}
R64 = "d002f443-no_profile_2.txt"
with open(os.path.join(os.path.dirname(HERE), "plans", "r66-replicate-sitting1.json"), encoding="utf-8") as _stream:
    PLAN = json.load(_stream)
HAVE_LOGS = all(os.path.exists(os.path.join(LOGS, f)) for f in list(SITTING1.values()) + [R64])


def arm_of(label):
    return "N" if label.startswith("N") else label.split("-")[0]


def check(path, arm, plan=PLAN, window=None):
    spec = plan["arms"][arm]
    state = logscan.parse_file(path, spec.get("expect_lines", []))
    if window is None:
        a = logscan.arrival(state)
        window = (a + 2, a + 100)
    switches = dict(benchplan.expected_switches(spec))
    if arm == "N":
        switches.update(EDEN_NX2_NATIVE_FLOAT_ATOMICS="off", LICHII_TFB_BARRIER_LAZY="off")
    return state, logscan.validate_run(state, plan, arm, window, benchplan.settings_gate(plan), switches,
                                       spec.get("expect_lines", []))


@unittest.skipUnless(HAVE_LOGS, "the r66 logs aren't here")
class RealLogTest(unittest.TestCase):
    def test_sitting1_runs_are_valid_for_their_arms(self):
        for label, name in SITTING1.items():
            state, (problems, flags, values) = check(os.path.join(LOGS, name), arm_of(label))
            self.assertEqual(problems, [], label)
            self.assertEqual(state.program_id, "0400486024DEC000")
            self.assertAlmostEqual(values["draws_pf"], 2760, delta=60)
            self.assertAlmostEqual(values["probe_mhz"], 1780, delta=30)
            self.assertAlmostEqual(values["back_ghz"], 4.21, delta=0.05)
            self.assertAlmostEqual(values["pkg_w"], 50.7, delta=1.0)
            self.assertEqual(values["pipelines_queued"] + values["pipelines_built"], 0)

    def test_wrong_arm_is_caught_by_the_switch_line(self):
        _, (problems, _, _) = check(os.path.join(LOGS, SITTING1["N1"]), "A43")
        self.assertTrue(any("EDEN_NX2_NATIVE_FLOAT_ATOMICS off, not on" in p for p in problems), problems)
        self.assertTrue(any("Float atomic add: native" in p for p in problems), problems)
        _, (problems, _, _) = check(os.path.join(LOGS, SITTING1["A44-1"]), "N")
        self.assertTrue(any("LICHII_TFB_BARRIER_LAZY on, not off" in p for p in problems), problems)

    def test_another_build_is_refused(self):
        _, (problems, _, values) = check(os.path.join(LOGS, R64), "N")
        self.assertTrue(any(p.startswith("build is") and "r64" in p for p in problems), problems)
        self.assertGreater(values["probe_mhz"], 3000)

    def test_gpu_clock_band(self):
        plan = json.loads(json.dumps(PLAN))
        plan["gates"]["probe_mhz"] = [3000, 3300]
        _, (problems, _, _) = check(os.path.join(LOGS, SITTING1["N1"]), "N", plan)
        self.assertTrue(any(p.startswith("GPU clock probe") for p in problems), problems)
        plan["gates"]["probe_mhz"] = [1700, 1900]
        _, (problems, _, _) = check(os.path.join(LOGS, SITTING1["N1"]), "N", plan)
        self.assertEqual(problems, [])

    def test_settings_gate_on_a_real_dump(self):
        plan = json.loads(json.dumps(PLAN))
        plan["gates"]["settings"] = {"force_max_clock": False}
        _, (problems, _, _) = check(os.path.join(LOGS, SITTING1["N1"]), "N", plan)
        self.assertIn("setting force_max_clock is true, not false", problems)

    def test_a_window_with_pipeline_work_is_invalid(self):
        with open(os.path.join(LOGS, SITTING1["N1"]), encoding="utf-8", errors="replace") as stream:
            src = stream.read()
        state = logscan.parse_file(os.path.join(LOGS, SITTING1["N1"]))
        a = logscan.arrival(state)
        line = next(l for l in src.splitlines()
                    if "Async translate in the last 10 s" in l and float(logscan.LINE.match(l)[1]) > a + 30)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(src.replace(line, line.replace(": 0 queued", ": 12 queued")))
            _, (problems, _, _) = check(path, "N")
        self.assertTrue(any("shaders still compiling" in p for p in problems), problems)

    def test_sitting_bands(self):
        runs = {}
        for label, name in SITTING1.items():
            state, (_, _, values) = check(os.path.join(LOGS, name), arm_of(label))
            runs[label] = {"values": values, "settings": state.settings}
        flags = logscan.sitting_checks(runs, PLAN["gates"])
        self.assertTrue(all(not f for f in flags.values()), flags)
        runs["N2"]["values"] = dict(runs["N2"]["values"], pkg_w=runs["N2"]["values"]["pkg_w"] * 1.03)
        runs["N3"]["settings"] = dict(runs["N3"]["settings"], **{"Audio.volume": "50"})
        flags = logscan.sitting_checks(runs, PLAN["gates"])
        self.assertTrue(any("package W" in f for f in flags["N2"]))
        self.assertTrue(any("Audio.volume" in f for f in flags["N3"]))

    def test_incremental_feed_matches_one_pass(self):
        path = os.path.join(LOGS, SITTING1["N1"])
        whole = logscan.parse_file(path)
        with open(path, "rb") as stream:
            raw = stream.read().decode("utf-8", errors="replace")
        part = logscan.LogState()
        for i in range(0, len(raw), 777):
            part.feed(raw[i:i + 777])
        part.line(part.partial)
        for key in ("version", "program_id", "settings", "switches", "probes", "clock", "draws", "translate", "worst"):
            self.assertEqual(getattr(part, key), getattr(whole, key), key)
        self.assertEqual(len(part.frames), len(whole.frames))


class SyntheticLogTest(unittest.TestCase):
    def test_precache_lines(self):
        state = logscan.LogState()
        for msg in ("Shader precache: nothing to precache",
                    "Shader precache: compute programs already done for this driver",
                    "Precompiled pipelines: C:/x/precompiled.nx2prep was prepared for this driver already"):
            state.line(f"[   1.000000] Render.Vulkan <Info> a.cpp:1:PrecacheShaders: {msg}")
        self.assertEqual(state.precache, [])
        state.line("[   2.000000] Render.Vulkan <Info> a.cpp:1:operator(): Precompiled pipelines: 5000 of 120124 done, 4991 built")
        state.line("[   3.000000] Render.Vulkan <Info> a.cpp:1:PrecacheShaders: Shader precache: finished, 41216 programs")
        self.assertEqual(len(state.precache), 2)

    def test_switch_line(self):
        sw = logscan.parse_switches("TEST switches in effect: EDEN_NX2_HEAP_TRACE on (EDEN_NX2_HEAP_TRACE_SLOW_US 2000), "
                                    "EDEN_NX2_PREALLOC_MIB 4096 (4096 MiB made), pin_gpu_threads off (setting)")
        self.assertEqual(sw, {"EDEN_NX2_HEAP_TRACE": "on", "EDEN_NX2_PREALLOC_MIB": "4096", "pin_gpu_threads": "off"})


class IdentifyLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.p = logscan.log_paths(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text, mtime):
        with open(path, "w") as stream:
            stream.write(text)
        os.utime(path, (mtime, mtime))

    HEAD = ("[   0.000300] Common <Info> common/windows/hybrid_cpu.cpp:235:SetCurrentThreadCoreRole: Core placement\n"
            "[   0.001200] Frontend <Info> qt_common/qt_common.cpp:251:Init: Lichii Version: Lichii Development Build | "
            "tuned-r66-pgo-lto-bfe83e2de4-tuned-r66-pgo-lto\n")

    def test_the_new_log_is_found(self):
        self.write(self.p["old"], "previous run\n", 900)
        previous = logscan.digest(self.p["old"])
        self.write(self.p["log"], self.HEAD, 1001)
        path, state = logscan.identify_log(self.dir, 1000, previous, "bfe83e2de4")
        self.assertEqual(path, self.p["log"])
        self.assertIn("bfe83e2de4", state.version)

    def test_an_old_log_is_not_taken(self):
        self.write(self.p["log"], self.HEAD, 990)
        with self.assertRaises(logscan.LogError) as ctx:
            logscan.identify_log(self.dir, 1000, None)
        self.assertFalse(ctx.exception.final)

    def test_no_rotation(self):
        self.write(self.p["old"], "something else\n", 900)
        self.write(self.p["log"], self.HEAD, 1001)
        with self.assertRaises(logscan.LogError) as ctx:
            logscan.identify_log(self.dir, 1000, "0" * 64)
        self.assertTrue(ctx.exception.final)

    def test_second_log_means_the_log_was_held_open(self):
        self.write(self.p["log"], self.HEAD, 1001)
        self.write(self.p["second"], self.HEAD, 1002)
        with self.assertRaises(logscan.LogError) as ctx:
            logscan.identify_log(self.dir, 1000, None)
        self.assertIn("eden_log_2.txt", str(ctx.exception))
        # One left over from an earlier day doesn't count.
        os.utime(self.p["second"], (500, 500))
        logscan.identify_log(self.dir, 1000, None)

    def test_wrong_build_is_fatal(self):
        self.write(self.p["log"], self.HEAD.replace("bfe83e2de4", "cccc1ff9fc"), 1001)
        with self.assertRaises(logscan.LogError) as ctx:
            logscan.identify_log(self.dir, 1000, None, "bfe83e2de4")
        self.assertTrue(ctx.exception.fatal)

    def test_version_line_must_be_near_the_top(self):
        self.write(self.p["log"], "x\n" * 10 + self.HEAD, 1001)
        with self.assertRaises(logscan.LogError):
            logscan.identify_log(self.dir, 1000, None)

    def test_tail_reads_only_new_bytes(self):
        self.write(self.p["log"], self.HEAD, 1001)
        state = logscan.LogState()
        tail = logscan.Tail(self.p["log"], state, lambda p: open(p, "rb"))
        tail.poll()
        self.assertIsNotNone(state.version)
        with open(self.p["log"], "a") as stream:
            stream.write("[   5.000000] Render.Vulkan <Info> a.cpp:1:TickFrame: DKB draws through_frame=9 frames=50 att")
        tail.poll()
        self.assertEqual(state.draws, [])
        with open(self.p["log"], "a") as stream:
            stream.write("empts=140000 skipped=0\n")
        tail.poll()
        self.assertEqual(state.draws, [(5.0, 50, 2800.0)])

    def test_tail_keeps_a_character_split_between_reads(self):
        # A user name with an accent in a path: its two UTF-8 bytes can land in two reads.
        line = "[   1.000000] Frontend <Info> a.cpp:1:Init: Lichii Version: x | C:/Users/Jos\u00e9/lichii\n".encode()
        cut = line.index(b"\xc3") + 1
        with open(self.p["log"], "wb") as stream:
            stream.write(line[:cut])
        state = logscan.LogState()
        tail = logscan.Tail(self.p["log"], state, lambda p: open(p, "rb"))
        tail.poll()
        with open(self.p["log"], "ab") as stream:
            stream.write(line[cut:])
        tail.poll()
        self.assertEqual(state.version, "x | C:/Users/Jos\u00e9/lichii")


if __name__ == "__main__":
    unittest.main()
