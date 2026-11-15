"""From Ocarina's title to Link at his save: the boot script's state machine.

Each step waits for its expected screen (a reference thumbnail from `check`, and a draws band
from the log where the step gives one), then presses its button. Only the step's own button
and B (to back out of an unknown screen) are ever pressed; sticks and the D-pad never are, so
Link never moves. No step presses while the log shows the world's draw count, so a screen
mistaken for a menu while Link stands at the fountain gets no A. A press whose connection broke
is not sent again blindly: the screen is looked at first.
"""

import base64

import screens
from client import ClientError, ConnectionLost, ServerError

ALLOWED_BUTTONS = {"A", "B"}
# Controller hotkey names in qt-config.ini -> the debug server's button names.
HOTKEY_NAMES = {"Dpad_Up": "Up", "Dpad_Down": "Down", "Dpad_Left": "Left", "Dpad_Right": "Right",
                "Plus": "Plus", "Minus": "Minus", "A": "A", "B": "B", "X": "X", "Y": "Y", "L": "L", "R": "R",
                "ZL": "ZL", "ZR": "ZR", "L_Stick": "StickL", "R_Stick": "StickR"}


class NavError(Exception):
    """Navigation failed in this launch: quit and retry the run in its slot."""


class NavSetupError(Exception):
    """The boot script or its references can't be used: fix the setup, don't retry."""


class Look:
    def __init__(self, fps=None, presents=None, draws=None, thumb=None, png=None):
        self.fps, self.presents, self.draws, self.thumb, self.png = fps, presents, draws, thumb, png


def hotkey_sets(bindings):
    """Button sets of the owner's controller hotkeys that injected input can complete (a Home or
    Screenshot combination can't: injection has no Home or Capture button)."""
    out = []
    for name, seq in bindings.items():
        parts = [p for p in (seq or "").split("+") if p]
        if not parts or any(p in ("Home", "Screenshot") for p in parts):
            continue
        out.append((name, {HOTKEY_NAMES.get(p, p) for p in parts}))
    return out


def check_press(buttons, hotkeys):
    buttons = set(buttons)
    if not buttons <= ALLOWED_BUTTONS:
        raise NavSetupError(f"the boot script may press only {sorted(ALLOWED_BUTTONS)}, not {sorted(buttons)}")
    for name, combo in hotkeys:
        if combo <= buttons:
            raise NavSetupError(f"pressing {'+'.join(sorted(buttons))} would complete the hotkey {name}")


def check_script(script, hotkeys):
    steps = script.get("steps", [])
    if not steps or steps[-1].get("press"):
        raise NavSetupError("the boot script must end with the arrival step, which presses nothing")
    for step in steps:
        for key in step:
            if key in ("left_stick", "right_stick", "stick"):
                raise NavSetupError(f"step {step['name']}: sticks are never used (Link must not move)")
        if step.get("press"):
            check_press(step["press"], hotkeys)
    check_press([script.get("recover_press", "B")], hotkeys)
    check_press([script.get("demo_press", "B")], hotkeys)


class Navigator:
    def __init__(self, client, draws_source, clock, script, refs, hotkeys, ask=None, say=print):
        """draws_source(n) gives the newest n per-second draw lines (t, frames, draws a frame)."""
        self.client = client
        self.draws_source = draws_source
        self.clock = clock
        self.script = script
        self.steps = script["steps"]
        self.refs = dict(refs)
        self.ask = ask
        self.say = say
        self.base_threshold = script.get("thumb_threshold", 24)
        self.max_press_draws = script.get("max_press_draws", 1000)
        self.record = {"steps": [], "recoveries": [], "taught": []}
        self.last_presents = None
        self.presents_since = None
        check_script(script, hotkeys)
        if ask is None:
            missing = [s["name"] for s in self.steps if s["name"] not in self.refs]
            if missing:
                raise NavSetupError("no reference screens for " + ", ".join(missing) + ": run bench.bat check first")

    # -- looking -----------------------------------------------------------------------------

    def look(self, shot=True):
        status = self.client.call("status", {"fps_window_ms": 500})
        presents = status.get("presents")
        now = self.clock.now()
        if presents != self.last_presents:
            self.last_presents, self.presents_since = presents, now
        elif now - self.presents_since > self.script.get("stall_s", 20):
            raise NavError(f"presents stopped at {presents} for {now - self.presents_since:.0f} s")
        latest = self.draws_source(1)
        look = Look(fps=status.get("fps"), presents=presents, draws=latest[-1][2] if latest else None)
        if shot:
            try:
                shot_ = self.client.call("screenshot", {"height": 144, "timeout_s": 5})
                look.png = base64.b64decode(shot_["png_base64"])
                look.thumb = screens.thumb_hash(look.png)
            except (ServerError, KeyError, screens.PngError):
                pass
        return look

    def threshold(self, name):
        ref = self.refs.get(name)
        jitter = ref.get("jitter", 0) if ref else 0
        return max(self.base_threshold, 2 * jitter + 4)

    def bands_ok(self, step, look):
        if step.get("press") and look.draws is not None and look.draws >= self.max_press_draws:
            return False  # the world is drawing: never a menu step, whatever the thumbnail says
        lo_hi = step.get("draws")
        if lo_hi and look.draws is not None and not lo_hi[0] <= look.draws <= lo_hi[1]:
            return False
        fps = step.get("fps")
        if fps and (look.fps is None or not fps[0] <= look.fps <= fps[1]):
            return False
        if step.get("min_fps") and (look.fps is None or look.fps < step["min_fps"]):
            return False
        return True

    def matches(self, k, look):
        step = self.steps[k]
        ref = self.refs.get(step["name"])
        if ref is None or look.thumb is None:
            return False
        d = screens.distance(look.thumb, ref["hash"])
        return d <= self.threshold(step["name"]) and self.bands_ok(step, look)

    def identify(self, look):
        """The known step whose reference is nearest the screen, within its threshold."""
        best = None
        for k, step in enumerate(self.steps):
            ref = self.refs.get(step["name"])
            if ref is None or look.thumb is None:
                continue
            d = screens.distance(look.thumb, ref["hash"])
            if d <= self.threshold(step["name"]) and self.bands_ok(step, look) and (best is None or d < best[1]):
                best = (k, d)
        return best[0] if best else None

    def arrival_steady(self, step):
        lo, hi = step.get("draws", [2600, 3200])
        need = int(step.get("stable_s", 3))
        lines = self.draws_source(need)
        return len(lines) >= need and all(lo <= x[2] <= hi for x in lines)

    # -- pressing ----------------------------------------------------------------------------

    def press(self, buttons, fps):
        hold = self.script.get("hold_s", 0.12)
        frames = max(2, min(300, round(hold * (fps or 60))))
        self.client.call("input", {"buttons": list(buttons), "frames": frames})
        return frames

    def wait_change(self, k, before):
        """True once the screen left step k's screen."""
        deadline = self.clock.now() + self.script.get("change_timeout_s", 8)
        while self.clock.now() < deadline:
            self.clock.sleep(0.5)
            look = self.look()
            if look.thumb is not None and before.thumb is not None and \
                    screens.distance(look.thumb, before.thumb) > self.threshold(self.steps[k]["name"]):
                return True
            if look.thumb is None and before.thumb is not None:
                return True  # a fade or load: no frame within the screenshot's timeout
        return False

    def do_press(self, k, look):
        step = self.steps[k]
        repressed = False
        while True:
            try:
                frames = self.press(step["press"], look.fps)
            except ConnectionLost as error:
                # It may have been pressed: look before doing anything else.
                self.record["recoveries"].append(f"{step['name']}: {error}")
                self.clock.sleep(2.0)
                after = self.look()
                same = after.thumb is not None and screens.distance(after.thumb, look.thumb or after.thumb) \
                    <= self.threshold(step["name"])
                if same and not repressed:
                    repressed = True
                    continue
                return {"frames": None, "connection_lost": True}
            if self.wait_change(k, look):
                return {"frames": frames, "repressed": repressed}
            if repressed:
                raise NavError(f"{step['name']}: the screen didn't change after pressing twice")
            self.record["recoveries"].append(f"{step['name']}: no change, pressed once more")
            repressed = True
            look = self.look()
            if not self.matches(k, look):
                # The screen moved on after all (a slow fade): don't press into the next one.
                return {"frames": frames, "repressed": False, "late_change": True}

    # -- teaching (attended check only) ------------------------------------------------------

    def teach(self, k):
        step = self.steps[k]
        say = step.get("say", step["name"])
        while True:
            if step.get("press"):
                answer = self.ask(f"Step {k + 1} of {len(self.steps)}: is {say} showing? "
                                  f"[Enter] press {'+'.join(step['press'])}, [w] wait, [q] stop: ")
            else:
                answer = self.ask(f"Is {say}? [y] yes, [w] wait, [q] stop: ")
                answer = "" if answer.strip().lower() == "y" else answer
            answer = answer.strip().lower()
            if answer == "q":
                raise NavSetupError("stopped at the owner's request")
            if answer == "w":
                self.clock.sleep(2.0)
                continue
            if answer == "":
                break
        hashes = []
        look = None
        for i in range(3):
            look = self.look()
            if look.thumb:
                hashes.append(look.thumb)
            if i < 2:
                self.clock.sleep(1.0)
        if not hashes:
            raise NavError(f"{step['name']}: no screenshot")
        jitter = max((screens.distance(a, b) for a in hashes for b in hashes), default=0)
        self.refs[step["name"]] = {"hash": hashes[-1], "jitter": jitter, "draws": look.draws, "fps": look.fps}
        self.record["taught"].append(step["name"])
        return look

    # -- the state machine -------------------------------------------------------------------

    def run(self):
        k = 0
        demo_presses = b_presses = 0
        while k < len(self.steps):
            step = self.steps[k]
            name = step["name"]
            begin = self.clock.now()
            timeout = step.get("timeout_s", 30)
            stable = None
            unknown_since = None
            arrived = None
            while True:
                if self.ask is not None and name not in self.refs:
                    if step.get("press") is None:
                        while not self.arrival_steady(step) and self.clock.now() - begin < timeout:
                            self.clock.sleep(1.0)
                    arrived = self.teach(k)
                    break
                look = self.look()
                now = self.clock.now()
                if self.matches(k, look):
                    unknown_since = None
                    if stable is None or screens.distance(stable.thumb, look.thumb) > self.threshold(name):
                        stable = look
                        stable.at = now
                    elif now - stable.at >= step.get("stable_s", 1.5) and \
                            (step.get("press") or self.arrival_steady(step)):
                        arrived = look
                        break
                else:
                    stable = None
                    j = self.identify(look)
                    if j is not None and j != k:
                        self.record["recoveries"].append(f"{name}: the screen is {self.steps[j]['name']}")
                        k = j
                        break
                    unknown_since = unknown_since or now
                    if k == 0 and step.get("press") and look.draws is not None \
                            and look.draws >= self.script.get("demo_draws", 300) and now - unknown_since >= 2:
                        if demo_presses >= 3:
                            raise NavError("the title's demo didn't end after 3 presses")
                        demo_presses += 1
                        self.record["recoveries"].append("title: the demo is playing, one press")
                        self.press([self.script.get("demo_press", "B")], look.fps)
                        unknown_since = None
                    elif k > 0 and step.get("press") and now - unknown_since >= self.script.get("unknown_s", 15) \
                            and (look.draws is None or look.draws < self.max_press_draws):
                        if b_presses >= 3:
                            raise NavError(f"{name}: unknown screen after 3 presses of B")
                        b_presses += 1
                        self.record["recoveries"].append(f"{name}: unknown screen, pressed B")
                        self.press([self.script.get("recover_press", "B")], look.fps)
                        self.clock.sleep(1.0)
                        unknown_since = None
                if now - begin > timeout:
                    raise NavError(f"{name}: its screen didn't show within {timeout} s")
                self.clock.sleep(self.script.get("poll_s", 1.0))
            if arrived is None:
                continue  # jumped to another step
            entry = {"step": name, "at": self.clock.now(), "fps": arrived.fps, "draws": arrived.draws}
            if step.get("press"):
                entry.update(self.do_press(k, arrived))
            self.record["steps"].append(entry)
            if not step.get("press"):
                self.record["arrival"] = entry
                self.record["arrival_thumb"] = arrived.thumb
                self.record["arrival_png"] = arrived.png
                return self.record
            k += 1
        raise NavError("the boot script ended without the arrival step")
