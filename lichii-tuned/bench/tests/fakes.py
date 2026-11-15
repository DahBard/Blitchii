"""A fake Lichii for the bench's tests: the game's screens, the debug server, the disk log it
writes as fake time passes, and the Windows host (process, windows, the confirm box)."""

import base64
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import screens  # noqa: E402
from client import ConnectionLost, NotListening, ServerError  # noqa: E402

BUILD = "Lichii Development Build | tuned-r66-pgo-lto-bfe83e2de4-tuned-r66-pgo-lto"
GAME = "The Legend of Zelda: Ocarina of Time"

# name: draws a frame, guest presents a second, what A does, (seconds, where it goes by itself)
SCREENS = {
    "logo": (2, 400, None, (15, "title")),
    "title": (2, 420, "menu2", (60, "demo")),
    "demo": (2500, 50, "title", None),
    "menu2": (20, 330, "menu3", None),
    "menu3": (126, 330, "menu4", None),
    "menu4": (56, 360, "loading", None),
    "loading": (5, 550, None, (8, "fountain")),
    "fountain": (2800, 50, None, None),
}
FLOW = {"press-1": "title", "press-2": "menu2", "press-3": "menu3", "press-4": "menu4", "arrival": "fountain"}

PERF_TRACE = ("@echo off\r\nrem test launcher\r\n"
              "set EDEN_DKB_FRAME_TRACE=1&& set EDEN_NX2_HEAP_TRACE=1&& set EDEN_NX2_CLOCK_TRACE=1&& "
              "set EDEN_NX2_GPU_CLOCK_PROBE=1&& set EDEN_NX2_WFI_STATS=1\r\n"
              'start "" "%~dp0lichii.exe" %*\r\n')


def screen_png(name, w=68, h=32):
    rng = random.Random(name)
    rows = []
    blocks = [[rng.randrange(256) for _ in range(17)] for _ in range(8)]
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = blocks[y * 8 // h][x * 17 // w]
            row += bytes((v, v, v))
        rows.append(row)
    return screens.encode_png(w, h, rows)


def refs_for_flow():
    return {step: {"hash": screens.thumb_hash(screen_png(name)), "jitter": 0} for step, name in FLOW.items()}


class FakeProcess:
    def __init__(self, world, pid):
        self.world = world
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.world.killed = True
        self.world.exit(1)


class World:
    def __init__(self, log_dir, start=1_000_000.0):
        self.t = start
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.mtimes = {}
        self.process = None
        self.killed = False
        self.f3_at = None
        self.input_faults = []   # per input call: None, "lost_after" (pressed), "lost_before"
        self.presses = []
        self.closes = []
        self.dialog = False
        self.confirm = True       # Lichii asks "Are you sure" (the owner's default)
        self.shutting = None
        self.env = {}
        self.switch_override = {}
        self.boot_delay = 5.0
        self.launches = 0
        self.nav_fail_launches = set()  # launches whose title never shows
        self.probe_mhz = 1790
        self.probe_at = None      # probe_at(uptime) -> MHz, to model a clock that differs while loading

    # -- time and the game ------------------------------------------------------------------

    def advance(self, seconds):
        end = self.t + seconds
        while self.t < end:
            step = min(0.25, end - self.t)
            self.t += step
            if self.process and self.process.returncode is None:
                self.tick(step)

    def tick(self, dt):
        up = self.t - self.created
        if up < self.boot_delay:
            self.write_log(up)
            return
        if self.screen is None:
            self.screen, self.screen_since = "logo", self.t
        draws, fps, _, auto = SCREENS[self.screen]
        self.presents += fps * dt
        if auto and self.t - self.screen_since >= auto[0]:
            if not (self.screen == "logo" and self.launches in self.nav_fail_launches):
                self.go(auto[1])
        if self.shutting is not None and self.t - self.shutting >= 5:
            self.log_line(up, "Frontend", "lichii/main_window.cpp:6074:closeEvent", "Closing frontend after emulation cleanup")
            self.exit(0)
            return
        self.write_log(up)

    def go(self, name):
        self.screen, self.screen_since = name, self.t

    # -- the process ------------------------------------------------------------------------

    def start(self, env):
        self.launches += 1
        log = os.path.join(self.log_dir, "eden_log.txt")
        if os.path.exists(log):
            os.replace(log, log + ".old.txt")
            self.mtimes[log + ".old.txt"] = self.mtimes.get(log, self.t)
        open(log, "w").close()
        self.mtimes[log] = self.t
        self.env = env
        self.created = self.t
        self.screen = None
        self.presents = 0.0
        self.written_s = -1
        self.dialog = False
        self.shutting = None
        self.killed = False
        self.process = FakeProcess(self, 4000 + self.launches)
        return self.process

    def exit(self, code):
        if self.process and self.process.returncode is None:
            self.process.returncode = code

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None

    # -- the log ----------------------------------------------------------------------------

    def log_line(self, t, cls, where, msg):
        path = os.path.join(self.log_dir, "eden_log.txt")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"[{t:12.6f}] {cls} <Info> {where}: {msg}\n")
        self.mtimes[path] = self.t

    def write_log(self, up):
        whole = int(up)
        while self.written_s < whole:
            self.written_s += 1
            self.second(self.written_s)

    def second(self, s):
        t = float(s) + 0.5
        if s == 0:
            self.log_line(0.001, "Frontend", "qt_common/qt_common.cpp:251:Init", f"Lichii Version: {BUILD}")
            settings = {"Renderer.force_max_clock": "true", "Renderer.frame_gen": "false",
                        "Renderer.nx2_frame_rate_limit": "120", "Renderer.fullscreen_mode": "Exclusive",
                        "Renderer.hdr_mode": "Off", "Core.use_speed_limit": "true", "Audio.volume": "100"}
            for k, v in settings.items():
                self.log_line(0.2, "Config", "common/settings.cpp:187:LogSettings", f"-- {k}: {v}")
            atomics = "on" if self.env.get("EDEN_NX2_NATIVE_FLOAT_ATOMICS") == "1" else "off"
            lazy = "on" if self.env.get("LICHII_TFB_BARRIER_LAZY") == "1" else "off"
            switches = {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": atomics, "LICHII_TFB_BARRIER_LAZY": lazy}
            switches.update(self.switch_override)
            self.log_line(0.3, "HW.GPU", "video_core/video_core.cpp:157:LogTestSwitches",
                          "TEST switches in effect: " + ", ".join(f"{k} {v}" for k, v in switches.items()))
            self.log_line(0.4, "HW.GPU", "common/clock_trace.cpp:1368:LogPowerMode",
                          'Windows power mode "best performance" (user-configured "best performance"), on AC power, power plan "Balanced"')
            self.log_line(0.5, "Frontend", "lichii/main_window.cpp:2498:BootGame",
                          f"Booting game: 0400486024DEC000 | {GAME} (64-bit) | ")
            if self.env.get("EDEN_NX2_NATIVE_FLOAT_ATOMICS") == "1":
                self.log_line(0.6, "Render.Vulkan", "video_core/renderer_vulkan/vk_pipeline_cache.cpp:852:PipelineCache",
                              "Float atomic add: native (device supports it natively, EDEN_NX2_NATIVE_FLOAT_ATOMICS on)")
            self.log_line(0.7, "Render.Vulkan", "video_core/renderer_vulkan/vk_pipeline_cache.cpp:2472:PrecacheShaders",
                          "Shader precache: nothing to precache")
            return
        if self.screen is None:
            return
        draws, fps, _, _ = SCREENS[self.screen]
        frames = max(1, int(fps))
        self.log_line(t, "Render.Vulkan", "video_core/renderer_vulkan/vk_rasterizer.cpp:4832:TickFrame",
                      f"DKB draws through_frame={s * 50} frames={frames} attempts={frames * draws} skipped=0")
        n = min(frames, 60)
        ms = 1000.0 / fps
        self.log_line(t, "Render.Vulkan", "./video_core/frame_time_stats.h:56:Note",
                      f"DKB frame_ms n={n} " + " ".join(f"{ms:.1f}" for _ in range(n)))
        self.log_line(t, "Render.Vulkan", "video_core/renderer_vulkan/vk_scheduler.cpp:1175:TraceGpuSubmission",
                      f"DKB gpu_frame renderer_frame={s} last_submission_tick=1 gpu_frame_span_ms=18.0 "
                      f"gpu_submission_sum_ms=15.1 gpu_busy_ms=15.100 measured_submissions=3 dropped_submissions=0 "
                      f"valid_bits=64 period_ns=10 gpu_upload_sum_ms=0.003 draws_timed=0")
        if s % 10 == 0:
            mhz = self.probe_at(s) if self.probe_at else self.probe_mhz
            self.log_line(t, "Render.Vulkan", "video_core/renderer_vulkan/vk_gpu_clock_probe.cpp:411:Report",
                          f"GPU clock probe in 10 s: median {mhz} MHz ({mhz - 10}-{mhz + 5}), loop 27.0 us")
            self.log_line(t, "HW.GPU", "common/clock_trace.cpp:1161:LogWindow",
                          "Clock trace per second (EDEN_NX2_CLOCK_TRACE, 10.0 s): back ghz=4.22 cpu_ms=755.6 poll_ms=28.8 "
                          "work_ms=726.8 p=100% | power RAPL_Package0_PKG=50.5W | power_mode=\"best performance\" "
                          "user_mode=\"best performance\" ac=on battery_saver=off")
            self.log_line(t, "Render.Vulkan", "video_core/renderer_vulkan/vk_pipeline_cache.cpp:3312:ReportWorstFrame",
                          "Worst frame in the last 10 s: 27.7 ms (0 of 500 frames over 33 ms); in it 0 pipelines built on the GPU thread")
            self.log_line(t, "Render.Vulkan", "video_core/renderer_vulkan/vk_pipeline_cache.cpp:4918:TickTranslations",
                          "Async translate in the last 10 s: 0 queued (0.0 ms)")

    # -- what the debug server and Windows see ------------------------------------------------

    def serving(self):
        return self.alive and self.t - self.created >= 3.0

    def press(self, buttons):
        self.presses.append((round(self.t - self.created, 2), self.screen, tuple(buttons)))
        nxt = SCREENS[self.screen][2] if self.screen else None
        if "A" in buttons and nxt:
            self.go(nxt)
        elif "B" in buttons and self.screen == "demo":
            self.go("title")
        elif "B" in buttons and self.screen in ("menu2", "menu3", "menu4"):
            self.go("title")


class FakeClient:
    def __init__(self, world):
        self.world = world
        self.calls = []

    def close(self):
        pass

    def call(self, method, params=None, timeout=None):
        w = self.world
        params = params or {}
        self.calls.append(method)
        if not w.serving():
            raise NotListening("nothing listens")
        if method == "status":
            before = w.presents
            w.advance(params.get("fps_window_ms", 500) / 1000.0)
            win = params.get("fps_window_ms", 500) / 1000.0
            return {"program_id": "0400486024DEC000", "title": GAME, "build": BUILD, "presents": int(w.presents),
                    "fps": (w.presents - before) / win if win else 0.0}
        if method == "screenshot":
            if w.screen is None:
                raise ServerError("screenshot: no frame was presented within 5 s")
            return {"width": 68, "height": 32, "png_base64": base64.b64encode(screen_png(w.screen)).decode()}
        if method == "frame_times":
            fps = SCREENS[w.screen][1]
            w.advance(params["seconds"])
            return {"frames": int(fps * params["seconds"]), "mean_ms": 1000.0 / fps, "fps": fps, "p50_ms": 1000.0 / fps,
                    "p99_ms": 1000.0 / fps, "max_ms": 1000.0 / fps, "stutters": 0, "truncated": False}
        if method == "input":
            fault = w.input_faults.pop(0) if w.input_faults else None
            if fault == "lost_before":
                raise ConnectionLost("input: connection reset")
            w.press(params.get("buttons", []))
            w.advance(params["frames"] / max(1, SCREENS[w.screen][1]))
            if fault == "lost_after":
                raise ConnectionLost("input: connection reset")
            return {"frames": params["frames"]}
        raise ServerError(f"unknown method {method}")


class FakeClock:
    def __init__(self, world):
        self.world = world

    def now(self):
        return self.world.t

    def sleep(self, seconds):
        self.world.advance(seconds)


class Stat:
    def __init__(self, mtime):
        self.st_mtime = mtime


class FakeHost:
    def __init__(self, world):
        self.world = world
        self.keys = []
        self.shutdown_dialog = False  # show Lichii's frameless "Closing software..." window

    def now(self):
        return self.world.t

    def sleep(self, seconds):
        self.world.advance(seconds)

    def launch(self, cmd, env, cwd):
        self.cmd, self.cwd = cmd, cwd
        return self.world.start(env)

    def creation_time(self, pid):
        return self.world.created

    def running(self, exe):
        return self.world.alive

    def port_open(self, address):
        return self.world.serving()

    def windows(self, pid):
        w = self.world
        if not w.alive:
            return []
        caption = 0x00CF0000  # WS_OVERLAPPEDWINDOW
        out = [{"hwnd": 1, "title": f"Lichii | tuned | clang | {GAME} | 1.0 | AMD", "cls": "Qt6110QWindowIcon",
                "visible": True, "style": caption},
               {"hwnd": 2, "title": "Lichii tuned | r66-bfe83e2de4", "cls": "Qt6110QWindowIcon", "visible": True,
                "style": 0x80000000}]
        if w.dialog:
            out.append({"hwnd": 3, "title": "Lichii", "cls": "Qt6110QWindowIcon", "visible": True,
                        "style": 0x80000000 | 0x00C80000})
        if self.shutdown_dialog and w.shutting is not None:
            # Untitled windows get the application name as their native title, without a caption.
            out.append({"hwnd": 4, "title": "Lichii", "cls": "Qt6110QWindowIcon", "visible": True,
                        "style": 0x80000000})
        return out

    def close_window(self, win):
        w = self.world
        w.closes.append(win["hwnd"])
        if win["hwnd"] == 2:
            w.render_closed = True
        elif win["hwnd"] == 1 and w.shutting is None:
            if w.confirm:
                w.dialog = True
            else:
                w.shutting = w.t

    def press_key(self, win, vk, char=None):
        self.keys.append((win["hwnd"], vk))
        w = self.world
        if win["hwnd"] == 3 and w.dialog and vk == 0x59:
            w.dialog = False
            w.shutting = w.t
            w.log_line(w.t - w.created, "Frontend", "lichii/main_window.cpp:2574:OnShutdownBegin",
                       "Shutdown requested; waiting for emulation thread")

    def open_shared(self, path):
        return open(path, "rb")

    def stat(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return Stat(self.world.mtimes.get(path, 0.0))

    def f3_down(self):
        return self.world.f3_at is not None and self.world.t >= self.world.f3_at

    def ac_power(self):
        return True

    def reboot_pending(self):
        return False

    def disk_free(self, path):
        return 100 << 30

    def keep_awake(self, on):
        pass

    def lower_priority(self):
        pass

    def beep(self):
        pass

    def show_file(self, path):
        pass

    def minidump(self, pid, path):
        return False
