import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakes  # noqa: E402
import navigate  # noqa: E402
import screens  # noqa: E402

with open(os.path.join(os.path.dirname(HERE), "oot_boot.json"), encoding="utf-8") as _stream:
    SCRIPT = json.load(_stream)


def draws_of(world):
    def source(n):
        if world.screen is None:
            return []
        draws, fps, _, _ = fakes.SCREENS[world.screen]
        return [(world.t, fps, float(draws))] * n
    return source


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.world = fakes.World(os.path.join(self.tmp.name, "log"))
        self.world.start({})
        self.world.advance(6.0)
        self.client = fakes.FakeClient(self.world)
        self.clock = fakes.FakeClock(self.world)

    def tearDown(self):
        self.tmp.cleanup()

    def nav(self, refs=None, script=SCRIPT, hotkeys=(), ask=None):
        return navigate.Navigator(self.client, draws_of(self.world), self.clock, script,
                                  fakes.refs_for_flow() if refs is None else refs, list(hotkeys),
                                  ask=ask, say=lambda s: None)


class NavigationTest(Base):
    def test_title_to_save_with_four_presses(self):
        record = self.nav().run()
        self.assertEqual(self.world.screen, "fountain")
        presses = [p for p in self.world.presses]
        self.assertEqual([p[1] for p in presses], ["title", "menu2", "menu3", "menu4"])
        self.assertTrue(all(p[2] == ("A",) for p in presses))
        self.assertEqual(record["arrival"]["step"], "arrival")
        self.assertEqual(record["recoveries"], [])

    def test_never_presses_before_the_title(self):
        self.nav().run()
        self.assertGreaterEqual(self.world.presses[0][0], 15.0)

    def test_hold_follows_the_presents_rate(self):
        frames = []
        real = self.client.call

        def spy(method, params=None, timeout=None):
            if method == "input":
                frames.append(params["frames"])
            return real(method, params, timeout)
        self.client.call = spy
        self.nav().run()
        # 0.12 s at the menus' 330-420 presents a second.
        self.assertTrue(all(35 <= f <= 55 for f in frames), frames)

    def test_demo_on_the_title_is_left_with_one_press(self):
        refs = fakes.refs_for_flow()
        real_title = refs["press-1"]
        refs["press-1"] = {"hash": "0" * 64, "jitter": 0}
        nav = self.nav(refs=refs)
        self.world.go("demo")
        nav.refs["press-1"] = real_title
        record = nav.run()
        self.assertIn("title: the demo is playing, one press", record["recoveries"])
        self.assertEqual(self.world.presses[0][1:], ("demo", ("B",)))
        self.assertEqual(self.world.screen, "fountain")

    def test_lost_connection_after_the_press_is_not_resent(self):
        self.world.input_faults = [None, "lost_after"]
        record = self.nav().run()
        self.assertEqual([p[1] for p in self.world.presses], ["title", "menu2", "menu3", "menu4"])
        self.assertTrue(any("connection reset" in r for r in record["recoveries"]))

    def test_lost_connection_before_the_press_presses_once_more(self):
        self.world.input_faults = [None, "lost_before"]
        self.nav().run()
        self.assertEqual([p[1] for p in self.world.presses], ["title", "menu2", "menu3", "menu4"])

    def test_unknown_screen_backs_out_with_b(self):
        refs = fakes.refs_for_flow()
        refs.pop("press-3")
        refs["press-3"] = {"hash": "f" * 64, "jitter": 0}
        with self.assertRaises(navigate.NavError) as ctx:
            self.nav(refs=refs).run()
        self.assertIn("unknown screen after 3 presses of B", str(ctx.exception))
        self.assertIn(("B",), [p[2] for p in self.world.presses])
        # B returned to the title, which the machine recognised and pressed through again.
        self.assertGreater(sum(1 for p in self.world.presses if p[1] == "title"), 1)

    def test_a_world_screen_that_looks_like_a_menu_gets_no_press(self):
        # Link at the fountain, and a menu reference that matches the fountain's thumbnail: the
        # draws say it is the world, so the machine goes to arrival instead of pressing A.
        self.world.go("fountain")
        refs = fakes.refs_for_flow()
        refs["press-2"] = dict(refs["arrival"])
        record = self.nav(refs=refs).run()
        self.assertEqual(self.world.presses, [])
        self.assertEqual(record["arrival"]["step"], "arrival")

    def test_title_that_never_shows_fails_the_run(self):
        self.world.go("logo")
        self.world.screen_since = self.world.t + 10000
        script = dict(SCRIPT, steps=[dict(SCRIPT["steps"][0], timeout_s=30)] + SCRIPT["steps"][1:])
        with self.assertRaises(navigate.NavError):
            self.nav(script=script).run()
        self.assertEqual(self.world.presses, [])

    def test_stalled_presents_fail(self):
        self.world.go("logo")
        self.world.screen_since = self.world.t + 10000
        real = self.client.call

        def frozen(method, params=None, timeout=None):
            result = real(method, params, timeout)
            if method == "status":
                result["presents"] = 1234
            return result
        self.client.call = frozen
        with self.assertRaises(navigate.NavError) as ctx:
            self.nav().run()
        self.assertIn("presents stopped", str(ctx.exception))

    def test_unattended_needs_references(self):
        with self.assertRaises(navigate.NavSetupError):
            self.nav(refs={})

    def test_attended_check_teaches_references(self):
        answers = []

        def ask(prompt):
            answers.append(prompt)
            return "y" if prompt.startswith("Is ") else ""
        self.world.advance(16.0)
        nav = self.nav(refs={}, ask=ask)
        record = nav.run()
        self.assertEqual(record["taught"], ["press-1", "press-2", "press-3", "press-4", "arrival"])
        flow = fakes.refs_for_flow()
        for step, ref in nav.refs.items():
            self.assertEqual(screens.distance(ref["hash"], flow[step]["hash"]), 0, step)
        self.assertEqual(len(answers), 5)


class ScriptRulesTest(unittest.TestCase):
    def test_hotkeys_from_bindings(self):
        sets = navigate.hotkey_sets({"Stop Emulation": "L+Plus+Minus", "Fullscreen": "Home+B", "Capture": "Screenshot",
                                     "Custom": "A+Dpad_Up"})
        self.assertEqual(dict(sets), {"Stop Emulation": {"L", "Plus", "Minus"}, "Custom": {"A", "Up"}})

    def test_a_press_completing_a_hotkey_is_refused(self):
        with self.assertRaises(navigate.NavSetupError):
            navigate.check_script(SCRIPT, [("Mine", {"A"})])

    def test_default_hotkeys_allow_the_boot_script(self):
        navigate.check_script(SCRIPT, navigate.hotkey_sets({"Stop Emulation": "L+Plus+Minus",
                                                            "Restart Emulation": "R+Plus+Minus"}))

    def test_directions_and_sticks_are_refused(self):
        bad = dict(SCRIPT, steps=[dict(SCRIPT["steps"][0], press=["Down"])] + SCRIPT["steps"][1:])
        with self.assertRaises(navigate.NavSetupError):
            navigate.check_script(bad, [])
        stick = dict(SCRIPT, steps=[dict(SCRIPT["steps"][0], left_stick=[0, 1])] + SCRIPT["steps"][1:])
        with self.assertRaises(navigate.NavSetupError):
            navigate.check_script(stick, [])

    def test_script_must_end_with_arrival(self):
        with self.assertRaises(navigate.NavSetupError):
            navigate.check_script(dict(SCRIPT, steps=SCRIPT["steps"][:-1]), [])


class ScreensTest(unittest.TestCase):
    def test_png_round_trip_and_hash(self):
        a, b = fakes.screen_png("title"), fakes.screen_png("menu2")
        w, h, c, rows = screens.decode_png(a)
        self.assertEqual((w, h, c, len(rows)), (68, 32, 3, 32))
        self.assertEqual(screens.distance(screens.thumb_hash(a), screens.thumb_hash(a)), 0)
        self.assertGreater(screens.distance(screens.thumb_hash(a), screens.thumb_hash(b)), 48)

    def test_filtered_png(self):
        import struct
        import zlib
        pixels = [[bytes((x * 7 % 256, y * 13 % 256, (x + y) % 256)) for x in range(5)] for y in range(4)]
        rows = [b"".join(pixels[y]) for y in range(4)]

        def filt(kind, row, prev):
            out = bytearray()
            for i, v in enumerate(row):
                a = row[i - 3] if i >= 3 else 0
                b = prev[i]
                c = prev[i - 3] if i >= 3 else 0
                if kind == 1:
                    pred = a
                elif kind == 2:
                    pred = b
                elif kind == 3:
                    pred = (a + b) >> 1
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                out.append((v - pred) & 255)
            return bytes(out)
        prev = bytes(15)
        raw = b""
        for kind, row in zip((1, 2, 3, 4), rows):
            raw += bytes((kind,)) + filt(kind, row, prev)
            prev = row

        def chunk(k, body):
            return struct.pack(">I", len(body)) + k + body + struct.pack(">I", zlib.crc32(k + body))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 5, 4, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        self.assertEqual(screens.decode_png(png)[3], rows)


if __name__ == "__main__":
    unittest.main()
