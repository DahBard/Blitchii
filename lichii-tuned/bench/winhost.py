"""The Windows-only parts of Lichii Bench, behind one small class.

Everything that needs the Win32 API (windows of a process, posted messages, the shared-delete
log open, power status, the keep-awake request, EcoQoS, F3, minidumps, and for the autopilot the
owner's last input, controller activity, a busy or locked desktop and the one-instance lock) is
here. The functions
that decide something (which window is the main one, which is the confirm box) are plain
functions, tested on Linux; the runner's tests use a fake host instead of WinHost.
"""

import os
import shutil
import socket
import subprocess
import time

WM_CLOSE = 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
VK_RETURN, VK_F3, VK_Y = 0x0D, 0x72, 0x59
WS_CAPTION = 0x00C00000
GWL_STYLE = -16
CONFIRM_TITLE = "Lichii"  # MainWindow::ConfirmClose: question(this, tr("Lichii"), ...)
INVALID_HANDLE = (1 << 64) - 1


def is_qt_window(win):
    return "QWindow" in win.get("cls", "")


def pick_main_window(windows, game_title=None):
    """The QMainWindow of a running game: its title has the game's name (UpdateWindowTitle). The
    render window, a separate top-level window in fullscreen, is titled `Lichii <branch> | ...`
    without it; closing that one only stops the game, so it is never picked."""
    qt = [w for w in windows if is_qt_window(w) and w.get("title")]
    if game_title:
        named = [w for w in qt if game_title in w["title"]]
        if named:
            return named[0]
    candidates = [w for w in qt if w["title"] != CONFIRM_TITLE and w["title"].count(" | ") >= 2]
    return candidates[0] if candidates else None


def pick_confirm_dialog(windows):
    """The "Are you sure you want to close Lichii?" box: a visible Qt top-level window titled
    exactly "Lichii" with a caption. Qt gives every untitled window the application name
    "Lichii" as its native title, so the frameless "Closing software..." dialog and tooltips
    carry it too; they have no caption (WS_CAPTION) and are left alone."""
    for w in windows:
        if not (is_qt_window(w) and w.get("visible") and w.get("title") == CONFIRM_TITLE):
            continue
        style = w.get("style")
        if style is None or style & WS_CAPTION == WS_CAPTION:
            return w
    return None


def key_lparams(scan):
    down = 1 | (scan << 16)
    return down, down | (1 << 30) | (1 << 31)


def filetime_to_unix(value):
    return value / 1e7 - 11644473600.0


# SHQueryUserNotificationState: the states in which no launch may start.
BUSY_STATES = {1: "the PC is locked or a screen saver runs", 2: "a full-screen program runs",
               3: "a full-screen Direct3D program runs", 4: "presentation mode is on"}


def idle_from_ticks(now_tick, last_tick):
    """Seconds since the last input from GetTickCount and LASTINPUTINFO.dwTime (32-bit, wraps
    every 49.7 days)."""
    return ((now_tick - last_tick) & 0xFFFFFFFF) / 1000.0


def pad_active(buttons, left_trigger, right_trigger, sticks, stick_limit=16000, trigger_limit=40):
    """A controller in use: a button down, a trigger pressed, or a stick well past its centre
    (half travel, so a resting stick's drift doesn't count)."""
    return bool(buttons) or left_trigger > trigger_limit or right_trigger > trigger_limit or \
        any(abs(v) > stick_limit for v in sticks)


def lock_exclusive(stream):
    """Takes a lock on an open file that ends with the process (a crash or reboot frees it).
    False if another process holds it."""
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def port_open(address):
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


class WinHost:
    """The real host. Created only on Windows."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ct, self.wt = ctypes, wt
        self.k32 = k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u32 = u32 = ctypes.WinDLL("user32", use_last_error=True)
        # Every handle-taking call gets its argtypes: without them ctypes passes a Python int as
        # a C int, and a 64-bit handle such as GetCurrentProcess()'s -1 raises ArgumentError.
        self.enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        sigs = [
            (k32.CreateFileW, wt.HANDLE, [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]),
            (k32.CloseHandle, wt.BOOL, [wt.HANDLE]),
            (k32.OpenProcess, wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
            (k32.GetCurrentProcess, wt.HANDLE, []),
            (k32.GetProcessTimes, wt.BOOL, [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4),
            (k32.SetPriorityClass, wt.BOOL, [wt.HANDLE, wt.DWORD]),
            (k32.SetProcessInformation, wt.BOOL, [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]),
            (k32.SetThreadExecutionState, wt.DWORD, [wt.DWORD]),
            (u32.EnumWindows, wt.BOOL, [self.enum_proc, wt.LPARAM]),
            (u32.GetWindowThreadProcessId, wt.DWORD, [wt.HWND, ctypes.POINTER(wt.DWORD)]),
            (u32.GetWindowTextW, ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            (u32.GetClassNameW, ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            (u32.IsWindowVisible, wt.BOOL, [wt.HWND]),
            (u32.GetWindowLongW, wt.LONG, [wt.HWND, ctypes.c_int]),
            (u32.PostMessageW, wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
            (u32.MapVirtualKeyW, wt.UINT, [wt.UINT, wt.UINT]),
            (u32.GetAsyncKeyState, wt.SHORT, [ctypes.c_int]),
            (k32.GetTickCount, wt.DWORD, []),
            (u32.OpenInputDesktop, wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
            (u32.CloseDesktop, wt.BOOL, [wt.HANDLE]),
        ]
        for fn, restype, argtypes in sigs:
            fn.restype, fn.argtypes = restype, argtypes
        self.pads = None        # XInputGetState, loaded on first use
        self.pad_connected = {}
        self.pad_checked = 0.0

    # -- time --------------------------------------------------------------------------------
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    # -- processes ---------------------------------------------------------------------------
    def launch(self, cmd, env, cwd):
        # NORMAL_PRIORITY_CLASS: without it a child of the below-normal runner (lower_priority)
        # would get below-normal priority too.
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x00000020)

    def creation_time(self, pid):
        wt = self.wt
        handle = self.k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wt.FILETIME() for _ in range(4)]
            if not self.k32.GetProcessTimes(handle, *[self.ct.byref(t) for t in times]):
                return None
            return filetime_to_unix((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            self.k32.CloseHandle(handle)

    def running(self, exe_name):
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        return exe_name.lower() in out.lower()

    port_open = staticmethod(port_open)

    def minidump(self, pid, path):
        """A thread-stack dump of a hung Lichii (dbghelp, MiniDumpWithThreadInfo)."""
        wt = self.wt
        dbghelp = self.ct.WinDLL("dbghelp")
        dbghelp.MiniDumpWriteDump.restype = wt.BOOL
        dbghelp.MiniDumpWriteDump.argtypes = [wt.HANDLE, wt.DWORD, wt.HANDLE, wt.DWORD, wt.LPVOID, wt.LPVOID,
                                              wt.LPVOID]
        process = self.k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
        if not process:
            return False
        out = self.k32.CreateFileW(str(path), 0x40000000, 0, None, 2, 0x80, None)
        try:
            if out in (None, INVALID_HANDLE):
                return False
            try:
                return bool(dbghelp.MiniDumpWriteDump(process, pid, out, 0x1000, None, None, None))
            finally:
                self.k32.CloseHandle(out)
        finally:
            self.k32.CloseHandle(process)

    # -- windows -----------------------------------------------------------------------------
    def windows(self, pid):
        ct, wt = self.ct, self.wt
        found = []

        def each(hwnd, _):
            owner = wt.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, ct.byref(owner))
            if owner.value == pid:
                title = ct.create_unicode_buffer(512)
                cls = ct.create_unicode_buffer(256)
                self.u32.GetWindowTextW(hwnd, title, 512)
                self.u32.GetClassNameW(hwnd, cls, 256)
                found.append({"hwnd": hwnd, "title": title.value, "cls": cls.value,
                              "visible": bool(self.u32.IsWindowVisible(hwnd)),
                              "style": self.u32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF})
            return True
        self.u32.EnumWindows(self.enum_proc(each), 0)
        return found

    def close_window(self, win):
        self.u32.PostMessageW(win["hwnd"], WM_CLOSE, 0, 0)

    def press_key(self, win, vk, char=None):
        scan = self.u32.MapVirtualKeyW(vk, 0)
        down, up = key_lparams(scan)
        self.u32.PostMessageW(win["hwnd"], WM_KEYDOWN, vk, down)
        if char:
            self.u32.PostMessageW(win["hwnd"], WM_CHAR, ord(char), down)
        self.u32.PostMessageW(win["hwnd"], WM_KEYUP, vk, up)

    # -- files -------------------------------------------------------------------------------
    def open_shared(self, path):
        """Read-only, with FILE_SHARE_READ | WRITE | DELETE, so Lichii can still rename the log."""
        import msvcrt
        handle = self.k32.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
        if handle in (None, INVALID_HANDLE):
            error = self.ct.get_last_error()
            if error in (2, 3):
                raise FileNotFoundError(path)
            raise OSError(f"can't open {path}: error {error}")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        return os.fdopen(fd, "rb")

    # -- machine -----------------------------------------------------------------------------
    def ac_power(self):
        class SPS(self.ct.Structure):
            _fields_ = [("ACLineStatus", self.ct.c_ubyte), ("BatteryFlag", self.ct.c_ubyte),
                        ("BatteryLifePercent", self.ct.c_ubyte), ("SystemStatusFlag", self.ct.c_ubyte),
                        ("BatteryLifeTime", self.wt.DWORD), ("BatteryFullLifeTime", self.wt.DWORD)]
        status = SPS()
        if not self.k32.GetSystemPowerStatus(self.ct.byref(status)):
            return None
        return {0: False, 1: True}.get(status.ACLineStatus)

    def reboot_pending(self):
        import winreg
        keys = [r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"]
        for key in keys:
            try:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key))
                return True
            except OSError:
                pass
        return False

    def keep_awake(self, on):
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, or ES_CONTINUOUS alone to end it.
        self.k32.SetThreadExecutionState(0x80000003 if on else 0x80000000)

    def lower_priority(self):
        """Below-normal priority with EcoQoS, so Windows keeps the runner on efficient cores."""
        process = self.k32.GetCurrentProcess()
        self.k32.SetPriorityClass(process, 0x4000)

        class Throttle(self.ct.Structure):
            _fields_ = [("Version", self.wt.ULONG), ("ControlMask", self.wt.ULONG), ("StateMask", self.wt.ULONG)]
        state = Throttle(1, 1, 1)
        self.k32.SetProcessInformation(process, 4, self.ct.byref(state), self.ct.sizeof(state))

    def f3_down(self):
        return bool(self.u32.GetAsyncKeyState(VK_F3) & 0x8000)

    # -- the owner (autopilot) ------------------------------------------------------------------
    def input_idle_s(self):
        """Seconds since the last keyboard or mouse input in this session (GetLastInputInfo). The
        bench's own input goes through the debug server and its key presses are posted messages,
        so neither counts."""
        class LastInput(self.ct.Structure):
            _fields_ = [("cbSize", self.wt.UINT), ("dwTime", self.wt.DWORD)]
        info = LastInput(self.ct.sizeof(LastInput), 0)
        if not self.u32.GetLastInputInfo(self.ct.byref(info)):
            return None
        return idle_from_ticks(self.k32.GetTickCount(), info.dwTime)

    def controller_active(self):
        """True while a connected XInput controller is in use. Unconnected slots are looked at
        again every 10 s only (a call on an empty slot is slow)."""
        ct, wt = self.ct, self.wt
        if self.pads is None:
            class Gamepad(ct.Structure):
                _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", ct.c_ubyte), ("bRightTrigger", ct.c_ubyte),
                            ("sThumbLX", ct.c_short), ("sThumbLY", ct.c_short), ("sThumbRX", ct.c_short),
                            ("sThumbRY", ct.c_short)]

            class State(ct.Structure):
                _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", Gamepad)]
            self.pad_state = State
            self.pads = False
            for name in ("XInput1_4", "XInput9_1_0"):
                try:
                    dll = ct.WinDLL(name)
                except OSError:
                    continue
                dll.XInputGetState.restype = wt.DWORD
                dll.XInputGetState.argtypes = [wt.DWORD, ct.POINTER(State)]
                self.pads = dll.XInputGetState
                break
        if not self.pads:
            return False
        now = time.monotonic()
        recheck = now - self.pad_checked >= 10.0
        if recheck:
            self.pad_checked = now
        active = False
        for index in range(4):
            if not recheck and not self.pad_connected.get(index):
                continue
            state = self.pad_state()
            connected = self.pads(index, ct.byref(state)) == 0
            self.pad_connected[index] = connected
            if connected:
                g = state.Gamepad
                active = active or pad_active(g.wButtons, g.bLeftTrigger, g.bRightTrigger,
                                              (g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY))
        return active

    def user_busy(self):
        """Why no launch may start now (locked, a full-screen program, presentation mode), or None."""
        desk = self.u32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desk:
            return "the PC is locked"
        self.u32.CloseDesktop(desk)
        state = self.ct.c_int(0)
        try:
            shell32 = self.ct.WinDLL("shell32")
            if shell32.SHQueryUserNotificationState(self.ct.byref(state)) != 0:
                return None
        except (OSError, AttributeError):
            return None
        return BUSY_STATES.get(state.value)

    def disk_free(self, path):
        return shutil.disk_usage(path).free

    def beep(self):
        import winsound
        winsound.MessageBeep()

    def start_file(self, path):
        os.startfile(path)

    def show_file(self, path):
        subprocess.Popen(["explorer", "/select,", str(path)])
