# Automated A/B testing for lichii-tuned: plan (final)

Owner's requests (2026-11-15): "devise a way to automatically do ab testing with scripts/macros. If
there is some program I can launch on my system that allows claude to tap in and play the game in
limited ways and extract log files, this would be ideal", and "also make a plan to make testing
quicker and more efficient, such as the way fritchii does it".

This revision takes in an adversarial review checked against the r66 tree (`3694d1a62a`). The
points that didn't hold, or held only in part, are listed at the end with the reasons.

## Summary

- **The program is Lichii itself.** Every r65/r66 package already contains upstream's live debug
  server. It is off unless `EDEN_NX2_DEBUG_SERVER` is set. It can press buttons and move sticks
  (`input`, `input_sequence`), take screenshots, report frame times and read the game's memory. It
  listens on 127.0.0.1 only. A small runner on the laptop, **Lichii Bench** (`bench.bat`), uses it
  to launch each arm, get to the Market fountain, measure, quit, copy the log and move on to the
  next arm. At the end it writes one zip.
- **Phase 0 (no new build, works with r66):** fresh-launch A/B sittings run unattended. A sitting
  that takes the owner about an hour today takes him about 5 minutes: he starts it, then uploads
  one zip. The sitting itself takes about 50 minutes, and he doesn't need to be at the PC.
- **Phase 1 (r67, small):** Fritchii's live switches (Frizti's numbered slots and the `switch`
  method), with switch changes tied to the client that made them. A positive control (a switch
  with a known cost) and a null switch prove that the live mode works. Plus `mark`, a boot line
  listing the environment, a TurboMode boot line and a pipeline-build counter. A switch that can
  change live is then compared in one launch, Frizti's way: flip it every 30 s in balanced order
  while Link stands at the fountain. A 1% effect takes about 16 minutes instead of 8-16 launches.
- **Phase 2 (r68 on):** recorded routes (Frizti's `input_record` and `input_replay`) for the
  five-load route, time-based input, an owner-input guard, more live switches, queued sittings in
  time windows the owner sets, and Claude driving the bench directly if Remote Control offers a
  folder.
- **Phase 3 (optional):** Frizti's flash watch as the visual check on unattended routes, Ocarina
  loading counters, and finding Link's position in memory.
- **The analysis becomes one maintained tool.** `lichii-tuned/bench/analyze.py` (today's
  prototype `ab_analyze.py`) reads the zip and the plan, and applies decision rules written into
  the plan before the runs. That replaces the per-sitting scripts (`r64-sitting/scripts` alone
  holds 50).
- **Before any automated number is trusted:** an A/A sitting, then a sitting that repeats r66
  sitting 1 and must reach the same verdicts. For live switches: a live A/A, and a positive
  control that live mode must recover at two sizes.

| | Today | Phase 0 (r66) | Phase 1 (r67) live | Phase 2 |
|---|---|---|---|---|
| Owner's time per sitting | ~60 min | ~5 min | ~3 min | ~1 min, or none with Remote Control |
| Wall clock, 8 measured fresh launches | ~60 min | ~50 min, unattended | same | queued in the owner's windows |
| Wall clock, one live item (1% effect) | n/a (8-16 launches) | n/a | ~16 min in one launch | same |
| File handling | copy, rename, upload 7-8 logs | one zip | one zip | none (Remote Control) |
| Analysis | new scripts per sitting | analyze.py | analyze.py | analyze.py |
| Routes | by hand | by hand | by hand | replayed |

## Measurement rules that stay as they are

These are the owner's method. Automation must not weaken any of them; the runner and the analyzer
enforce them.

1. **Balanced schedules.** Fresh launches: warm-up launches W that aren't counted (one per arm
   that changes pipelines, see rule 7), then A B B A | B A A B blocks. Live flips: ABBA BAAB units,
   which cancel linear and quadratic drift exactly (A at segments 0, 3, 5, 6; B at 1, 2, 4, 7).
   Fresh-launch order alternates the starting arm from block to block, as upstream's
   `cache_bench.py` does. A failed run is retried once in the same slot. If the retry fails too,
   the whole block is dropped and a new complete block is appended, and the summary says so. A run
   is never moved to the end on its own, since out of its slot it no longer cancels drift.
2. **Matched windows.** Fresh launches: the same offset from arrival in every run (settle 60 s,
   measure 100 s, today's protocol), cut to the shortest common stay. Live: 30 s segments (5 s
   settle discarded, 25 s measured), and only per-second log lines that lie wholly inside a
   segment. Segments for switches that change package power are at least 90 s (rule 8).
3. **The periodic hitches are measured, not assumed.** r66 sitting 1 found the hitch every
   5.026-5.030 s (one 45 ms frame, locked to the FSR context, the main reason the 1% low sits at
   26-28), and perf-trace-noprofile adds its own +7-10 ms frame every 4.157 s. The old 10.03 s
   premise is gone. The analyzer:
   - estimates each run's spike periods and phase from the per-present times;
   - reports the mean frame time with spike frames removed as the precise metric, and spikes per
     window (count and size) as a separate metric, so an item that changes the hitch itself still
     shows;
   - cuts live measured windows to whole 5.027 s periods where it can (25.1 s = 5 periods); the two
     periods have no practical common multiple, so spike removal does the rest;
   - treats "spike frames per window" as a validity check: a window with more spike frames than
     its periods allow (plus one) is flagged.
4. **Decision rules written before the runs.** Every plan file carries its rules: metric, which
   direction is better, minimum effect, guards, and how many blocks or units are run. The runner
   refuses a plan without rules. The number of units is fixed in advance. An early stop is allowed
   only where the plan's own rule says so (see "Decision rules").
5. **Unprofiled baselines, identical traces.** Frame-rate arms use perf-trace-noprofile's
   variables (frame, heap and clock traces, GPU clock probe, WFI counts, the work watch), never
   `EDEN_NX2_GPU_PROFILE`. Zone sessions (profile on in both arms) are separate sittings and are
   never mixed with fps arms. Traces are the same in every arm of a comparison and never flipped
   live; the runner refuses a plan whose arms differ in any trace variable.
6. **Same conditions.** Force maximum clocks on, frame generation off, the 120 fps lock, HDR off,
   exclusive fullscreen, AC power, Windows power mode Best performance, and the same audio volume
   or mute in all arms. The runner checks these in the log's settings dump and Clock trace lines,
   and refuses runs that differ. Each window's back-thread GHz and package W (from the Clock trace)
   must lie within ±1.5% of the sitting's median, or the window is flagged.
7. **Warm caches per arm.** Arms that change SPIR-V or pipelines (ab-43 native atomics,
   `EDEN_AMD_FP16`, ab-47 routing) start with a cold AMD driver cache on their first launch, and a
   driver-cache miss doesn't show as a precache in the log. So each such arm gets its own W launch
   (W:A, W:B), and each window is gated on pipeline work inside it: on r66, `Async translate in the
   last 10 s` must show 0 queued and `Worst frame in the last 10 s` 0 pipelines built; from r67, a
   build counter that includes driver time (Phase 1).
8. **Carry-over of CPU power.** PL1's averaging window is tens of seconds, so a live switch that
   changes package power (spins, `EDEN_NX2_ARB_SPIN`, KSPIN-class switches, the burn control) bleeds
   into the next 30 s segment and is biased toward zero. Those switches get segments of 90 s or
   more, or stay fresh-launch. Per-segment back-thread GHz and package W are logged and reported.
9. **The log is the source of numbers.** Verdicts come from the same per-second log lines the
   manual sittings use (`TickFrame: DKB ...`, GPU busy, back CPU, Clock trace, GPU clock probe).
   The debug server's `frame_times` only gives the quick summary the runner prints.
10. **Automated runs are compared with automated runs.** Turning the server on also turns on the
    log ring (`SetRingCapacity` at nx2_debug_tools.cpp:970): every log line is formatted twice and
    takes one more mutex. That is about 25 lines a second in a perf-trace run, so the cost should be
    tiny, but two manual runs can't measure anything under ~1%, and the plan doesn't claim they
    do. Manual and automated runs are never mixed in one comparison.

**Open measurement problem, to settle first.** Every r66 run's GPU clock probe read about
1,790 MHz, with Force maximum clocks on or off, while every r64 run with it on read
3,127-3,190 MHz. Fountain GPU busy went from 13.1 ms (r64) to 15.2 ms (r66), and r66's fountain
was GPU-bound, a state the owner doesn't play in. TurboMode starts only when
`Device::ShouldBoostClocks()` allows it (renderer_vulkan.cpp:227), and it logs nothing either way.
The refusal for a debugging tool is unlikely: the tool check covers only RenderDoc, Nsight Graphics
and "Radeon GPU Profiler", and `CollectToolingInfo` logs `Attached debugging tool: <name>` for each
tool, a line no r66 log has. The probe band is part of every plan's gates, so Phase 0 catches a
wrong clock state in the first minute; r67 adds a boot line that says whether TurboMode runs or
why it was skipped. GPU-time A/Bs are not trusted until the probe reads the band the plan expects,
and the A/A noise floor is measured again once it does (see "How we check it").

## What the debug server gives us today (r66)

Researched in r66's tree (`src/core/debugger/nx2_debug_server.cpp`, `nx2_debug_tools.cpp`,
`tools/nx2/debug_mcp.py`):

- **It's in every build.** It starts at the end of `System::Impl::Load`, before the logos, and
  stops first at shutdown. Only `EDEN_NX2_DEBUG_SERVER=127.0.0.1:6545` in the launch environment
  turns it on. No launcher of ours sets it today.
- **It's loopback only, with no authentication.** One thread answers one request at a time. A long
  request blocks the others: `input_sequence` up to 900 s, `frame_times` up to 60 s.
  `frame_times` sleeps without checking whether the client is gone (nx2_debug_tools.cpp:213-234),
  so an aborted runner can't end it early. A second Lichii can't bind the port until the first has
  exited.
- **Input.** Buttons A B X Y L R ZL ZR Plus Minus D-pad StickL StickR, and sticks as [x, y] in
  -1..1. Input goes to Player 1 (or Handheld) as the emulated controller sees it, works without
  window focus, and is ORed with the owner's buttons; an injected stick replaces his. There is no
  Home or Capture button. Steps are counted in guest presents. Input is released on every exit
  path, including the client disconnecting (checked every 2 ms), so a runner that dies or is
  aborted leaves no button held. Switches, `reject_shaders`, `log_filter` and `speed_limit` are
  *not* reverted on disconnect.
- **Signals.** `status` gives the build, program id, total presents and fps over up to 5 s.
  `frame_times` gives present-interval stats from a 4,096-present ring (core.cpp:93), so a call
  longer than 4,096 / fps seconds is truncated (check its `truncated` field). `screenshot` gives a
  PNG of the next present (one GPU sync). `log_tail` reads the log ring.
- **What's missing for unattended A/B:** a log marker, live switches (every switch of ours is read
  once into a static), a boot line that lists the environment (the `TEST switches in effect` line
  lists only some switches), input timed in seconds (Ocarina moves by elapsed time under Dynamic
  FPS, so present-timed walks drift with the frame rate), and a guard against the owner's input.
- **Hazards.**
  - `log_tail` copies every newer ring line under the ring's mutex (`ReadRing`, logging.cpp:554;
    with `after`=0 that is the whole 20,000-line ring). `RingBackend::Write` takes the same mutex
    on the thread that logs, including the renderer's per-second TickFrame line, so a `log_tail`
    can hitch a frame. The runner doesn't use it (see "One fresh-launch run").
  - The client (`debug_mcp.Emulator.call`, debug_mcp.py:298-330) reconnects and sends the request
    again after any OSError. For `input` that means a second press, and later a second `switch`.
  - Minimized, the host present, screenshots and the renderer's frame ticks stop while the game
    keeps presenting: measurements are invalid.
  - Hotkeys read `GetNpadButtons()`, which includes injected buttons (hotkeys.cpp:202,
    emulated_controller.cpp:1822). The defaults L+Plus+Minus and R+Plus+Minus are Stop and Restart;
    the owner may have rebound others. Home combinations can't be completed by injection: the
    hotkey also needs the Home bits, and `GetHomeButtons()` doesn't include injected input.
  - The file select has copy and erase.
  - Log rotation (logging.cpp:248-279): at start `eden_log.txt` is renamed to
    `eden_log.txt.old.txt`. If the file is open anywhere, the rename fails and Lichii writes
    `eden_log_2.txt` instead, and if that one is open too, it appends to it. Python's `open()` on
    Windows doesn't pass FILE_SHARE_DELETE, so a runner holding the log open across a launch
    either reads the previous run's log or makes Lichii write `eden_log_2.txt`.

## Phase 0: Lichii Bench v0 on r66 (no build)

### What it delivers to the owner

Unattended fresh-launch sittings at the Market fountain. The owner starts one window, walks away,
and uploads one zip. Each run is checked against the plan's conditions (settings, switches, GPU
clock band, CPU clock and power band, pipeline work), so a bad run is found in its first minute,
not after the hour. It also runs a new build's first launch (the precache and preparation, ~40 min
for r66) unattended.

### Code and script changes

All new files are ours. Upstream's `tools/nx2/` stays untouched.

- **`dist/lichii-tuned/bench/oot_bench.py`**: the runner, Python 3 standard library only.
  - It uses `Emulator` from a copy of `tools/nx2/debug_mcp.py`, which CI copies into `bench\` at
    packaging time, so the client always matches the build's server. The runner wraps it so that
    only read-only methods (`status`, `frame_times`, `screenshot`) are ever retried. After a
    connection error during `input` or `input_sequence` (and later `switch`), it doesn't resend: it
    checks the screen state again and continues from there, or fails the run.
  - It reuses `cache_bench.py`'s patterns (`ensure_closed`, `guard`, the alternating order,
    resume), copied alongside the same way. It doesn't reuse `log_time` (a `log_tail` without
    `after`) or `boot_and_measure`'s mtime-only log copy.
  - Windows-specific parts (CreateFileW, process and window handles, power status, QoS) sit behind
    one small module, so the rest is tested on Linux.
  - Commands:
    - `setup` (one time)
    - `check` (a smoke test: windowed once with the owner watching, then fullscreen unattended)
    - `teach` (fallback only, windowed; see "Navigation")
    - `prepare` (a new build's first launch)
    - `run <plan>` (an attended start)
    - `resume <session>`
    - `zip <session>`
    - `backup-save`, `restore-config`
- **`dist/lichii-tuned/bench/screens.py`**: a PNG decoder (zlib) and a difference hash of a
  256x144 thumbnail, to compare screens with references.
- **`dist/lichii-tuned/bench.bat`**: one line,
  `"%~dp0bench\python\python.exe" -I "%~dp0bench\oot_bench.py" %*`, with a fallback to an
  installed `py -3`.
- **Arms are a base launcher plus an environment difference.** A plan names each arm as
  `{"base": "perf-trace-noprofile.bat", "env": {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": "1"}}`. The
  launchers stay the one source of the trace variables for manual play and for the bench.
  - The runner parses the base's `set NAME=VALUE&&` lines. It also handles the quoted form
    `set "NAME=VALUE"` and `%~dp0` (the build folder), and refuses anything else with a message
    naming the line.
  - It refuses a plan whose arms differ in any trace variable (rule 5).
  - It starts each launch from a scrubbed environment: every inherited `EDEN_*` and `LICHII_*`
    variable is removed first. The launchers have no `setlocal`, so a console that ran one keeps
    its variables, and `cache_bench`'s `run_env` would pass them on to every arm.
  - It adds `EDEN_NX2_DEBUG_SERVER=127.0.0.1:6545`, `EDEN_NX2_DEBUG_DIR=<run>\debug` and, from r67,
    `LICHII_AB_ARM=<arm>` and `LICHII_AB_RUN=<session>/<NN>`.
  - Until r67's environment line (Phase 1), Phase 0 accepts only arms whose change shows in the
    `TEST switches in effect` line or in another known log line (`EDEN_AMD_FP16` has its own).
    `EXP<n>`, `EDEN_NX2_FSR_VERSION`, `EDEN_NX2_DLSS_REPLACEMENT`, `LICHII_WFI_DEFER` and
    `LICHII_REPORT_DRAIN` don't, so they wait for r67.
- **`dist/lichii-tuned/bench/plans/`**: example plans. Each sitting's plan is written by the agent
  that prepares it and given to the owner, or included in the build.
- **`dist/lichii-tuned/bench/oot_boot.json`**: the boot script (see "Navigation").
- **`lichii-tuned/bench/analyze.py`** (from the prototype `scratchpad/ab-plan/ab_analyze.py`),
  cloud side, standard library only.
  - It reads a session zip and its plan, parses each log once, and builds fountain, route and live
    windows.
  - It checks validity: the build commit, the arm's switches, expected lines, no host crash, no
    `eden_log_2.txt`, the power mode, the probe band, the CPU clock and power band, the draws band,
    pipeline work in the window, and spike frames per window.
  - It estimates the spike periods and phase, and prints per-run and per-arm tables (with and
    without spike frames) and the verdicts by the plan's rules.
  - The prototype already reproduces r66 sitting 1 (ab-43 and ab-44: DROP, as `decide.json`) and
    sitting 2 (ab-45: INCONCLUSIVE, about 113 runs per arm needed, as `decide66`).
  - Its tests use generated logs only. The synthetic logs in `scratchpad/ab-plan/synth/` contain the
    owner's log content and are never committed. The logs carry the Windows username unless
    `censor_username` is set; that's fine in this private session, but logs stay out of anything
    published.
- **CI**, mindful of minutes (Windows runners cost twice as many on a private repository):
  - `lichii-tuned.yml`'s packaging step copies `tools/nx2/debug_mcp.py` and `cache_bench.py` into
    `bench\`.
  - `lichii-tuned-tests.yml` runs `python -I -m unittest` on ubuntu for
    `lichii-tuned/tests/test_oot_bench.py` (the runner against a fake debug server: navigation,
    timeouts, abort, the no-resend rule, the quit sequence with a fake window layer, log
    identification and the .bat parser) and `test_analyze.py`.
  - A Windows job runs only the tests of the ctypes module, with a path filter on `bench/`.
- **Python.** A pinned embeddable Python (`python-3.13.x-embed-amd64`, ~11 MB) is published once as
  a release asset (`bench-python-3.13.x`, SHA-256 checked by CI, like the `pgo-*` releases). The
  owner unzips it into `bench\python` once; later packages also look in `C:\LichiiBench\python`.
- **For r66, which has no `bench\` folder:** a `bench-kit-v0` release asset holds `bench.bat` and
  `bench\`, unzipped into the r66 folder. From r67 on, `bench\` is in every package.
- **Docs.** A Lichii Bench section in `README-tuned.txt` and HANDOFF. Our AGENTS.md copy gets the
  runner's rules (Phase 2's list).

**The data folder is `C:\LichiiBench`** (outside OneDrive, no spaces):

- `local.json`: the OoT file, the Lichii data root, the save folder, the owner's hotkey bindings.
- `refs\`: the boot script's reference thumbnails from `check`.
- `sessions\<date>-<plan>\`:
  - `plan.json`;
  - `status.json` (live progress);
  - `control.json` (pause, stop after this run, abort);
  - `runner.log`;
  - `config-start\` (copies of `qt-config.ini` and the OoT per-game config at session start);
  - `runs\NN-<arm>\`, each with `eden_log.txt`, `run.json` and `shots\`.
- `results\<session>.zip`: logs, `run.json` files, thumbnails and `summary.txt`. Never saves, keys,
  configs, nand or sdmc.
- `save-backups\`: read-only copies of the save folder.

### Navigation

- **The boot script** is a fixed list of OoT steps (a few presses from the title to the fountain),
  written here from the owner's description of his boot path (question 4). Each press needs its
  expected screen first: a draw band from the log's `DKB draws` lines (present in every arm, since
  all arms share the perf-trace base) and a thumbnail.
- **`check` confirms it.** The first `check` runs windowed (no `-f`) with the owner watching; it
  saves each step's thumbnail as the reference. The next `check` runs fullscreen and unattended,
  and its thumbnails must match. If the script doesn't fit his menus, `teach` (windowed, since a
  console prompt would take focus from exclusive fullscreen and minimize it) records the steps
  instead.
- **The title's attract demo.** The runner presses well before the title's idle timer. If the demo
  starts anyway (the thumbnail no longer matches the title), one press returns to the title and the
  step waits for the title thumbnail again.
- **Per-arm tolerance.** Arms that change the image (trace-no-fsr, FSR or DLSS variants) get a
  wider thumbnail threshold set in the plan, or their own references from their W launch.
- **Holds.** r66 counts input in presents, and the menus run at 300-650 presents/s, so each hold
  given in seconds becomes `frames = round(hold_s x measured presents/s)` before each `input`.
- **Hotkeys.** At setup the runner reads the owner's actual hotkey bindings (`Controller_KeySeq`)
  from his config and refuses any step that completes one, not only the two default combinations.

### One fresh-launch run, as the runner does it

1. **Preflight.**
   - No `lichii.exe` running, port 6545 free, no handle of the runner's open on any log.
   - On AC power (`GetSystemPowerStatus`), enough disk space, no pending reboot (Windows Update).
   - The PC kept awake through the whole sitting, cooldowns included
     (`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)`; Lichii's
     own request covers only the time a game runs).
   - The runner runs at below-normal priority with EcoQoS, so Windows keeps it on the efficient
     cores, without a hard affinity mask (Lichii itself places HostTiming on the E-cores).
2. **Launch.** `lichii.exe -f -g "<OoT file>"`, with the arm's environment. The launch time and the
   process's creation time are recorded.
3. **Find this run's log.**
   - The runner opens `eden_log.txt` only once its mtime is newer than the launch, and only with
     CreateFileW and FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE. It never holds a log
     handle across a launch. (Creation times are not used: NTFS tunneling can give a new file the
     creation time of the one just renamed.)
   - Rotation must have happened: `eden_log.txt.old.txt` must now equal the previous run's copied
     log. From r67, the environment line must show this run's `LICHII_AB_RUN`.
   - The first lines must be the version line of the plan's build, then this run's `Booting game`.
   - The runner also checks `eden_log_2.txt`. If it was written after the launch, the run is
     invalid.
4. **Boot checks, from the disk log** (flushed once a second).
   - Within 60 s the log must show `Booting game`, the version line and the settings dump. The
     settings must match the plan (`force_max_clock`, frame generation off,
     `nx2_frame_rate_limit 120`, exclusive fullscreen, HDR off, the speed limit on).
   - The `TEST switches in effect` line (or the arm's other known line) must show the arm's change.
   - It connects to the server, retrying. `status` must show program id 0400486024DEC000 and the
     build.
5. **Navigate** with the boot script.
   - Title: 2 draws a frame at >150 presents/s.
   - Each menu step with its expected screen.
   - The loading screen: 5-6 draws at >300 presents/s, with no input sent while it shows.
   - The fountain: 2,600-3,200 draws at 30-70 presents/s for 3 s, and the arrival thumbnail must
     match its reference.
6. **Settle 60 s** with no input. Gates read from the disk log, never through `log_tail`:
   - the newest `GPU clock probe in 10 s` and `Clock trace` lines inside the plan's bands;
   - the power mode Best performance;
   - no pipeline work (rule 7);
   - otherwise the sitting stops, and the summary says why.
   - Thumbnails: at arrival, and once more at least 2 s before the window starts.
7. **Measure 100 s.**
   - The only server calls inside the window are `frame_times` calls of 25 s or less. The handler
     only sleeps and reads the present ring's atomics at the end, and 25 s stays under the ring's
     4,096 presents up to ~160 fps. A truncated result marks the window.
   - The window's log uptimes come from the process's creation time, cross-checked against the
     newest disk-log line before and after. The analyzer uses only whole per-second lines inside,
     so ±1 s is enough.
   - The end thumbnail is taken at least 2 s after the window. It also proves the window wasn't
     minimized, since screenshots fail when it is.
   - If the two thumbnails differ beyond the threshold, or draws drift more than 3%, the run is
     flagged (an NPC walked into view, a text box, a camera move).
8. **Quit.**
   - WM_CLOSE goes to the QMainWindow of the launched PID, found by its window class and title. The
     render window's own close only stops the game and leaves Lichii open (bootmanager.cpp:238,
     main_window.cpp:2418); in fullscreen it is a separate top-level window, so the runner skips it.
   - The runner re-sends WM_CLOSE every few seconds until the PID exits, up to 60 s. Re-sending is
     safe: `close_pending` makes the shutdown run once.
   - A confirm box appears only if the game holds the exit lock, under the default "Ask based on
     game" (`ConfirmClose`, main_window.cpp:6038). `check` finds out whether OoT does. Only if a
     box shows does the owner need to set "Confirm before stopping" to Never.
   - After 60 s the runner kills the PID and marks the run (a kill loses at most 1 s of the log's
     lines below Error). The exit code goes into `run.json`, with crash-trace.bat's codes.
9. **Collect.**
   - It copies `eden_log.txt` (and `eden_crash.dmp` if present) to `runs\NN-<arm>\`. This is what
     ends the owner's copying and renaming.
   - It writes `run.json`: arm, environment difference, timings, window uptimes, `frame_times`
     results, thumbnails, exit code, validity flags.
10. **Cool down** for the plan's fixed time (60 s by default, the same before every run), then the
    next run.

**Configs and play time.** `-f` changes the fullscreen action, and every exit writes the UI
settings (main_window.cpp:581), so the configs are rewritten each run anyway, and a kill can land
during that write. Every run also adds OoT play time. At the end the runner compares both configs
with the start copies key by key and reports any change besides the fullscreen and window keys,
or a file it can't parse. `bench.bat restore-config` puts the start copies back; it never does so
on its own.

**Hangs and failures.**

- **What counts as hung:**
  - no first present within the boot timeout;
  - presents not advancing for 20 s outside a fade (`status` about once a second, outside windows);
  - the server silent for 30 s;
  - an expected screen not reached in time;
  - during `prepare`, no new disk-log line and flat process CPU time for 5 min (not the server).
- **What the runner does:**
  1. releases input (the socket closes);
  2. takes a thumbnail if the server answers;
  3. writes a thread-stack minidump through dbghelp;
  4. sends WM_CLOSE, waits 30 s, then terminates the PID;
  5. copies the log.
- **Recovery, in order:**
  1. Check the screen, then press the step again once if it is still the step's screen.
  2. On an unknown screen, press B up to 3 times, 1 s apart.
  3. Otherwise quit, relaunch and retry the run in the same slot (rule 1).
- Two failed slots in a row stop the sitting.
- **On abort:** an abort caused by a failure also quits Lichii, so the owner never comes back to a
  game left in an experimental state. Only an abort the owner starts (F3, Ctrl+C, closing the
  console) leaves Lichii running.

**`prepare` for a new build's first launch.**

- A 75-minute first-present timeout and the disk-log and CPU-time watchdog.
- Then 60 s at the fountain, a clean quit, and the plan's W launches.
- A measured run whose log shows a precache or preparation is invalid.

### What the owner sets up once (~30 min, ~10 of it at the PC)

1. Unzip `bench-kit-v0` into the r66 folder and `bench-python` into `bench\python`.
2. In Lichii: check that "Pause emulation when in background" is off (the default) and that
   Player 1 is connected as a Pro Controller. "Confirm before stopping" stays as it is unless
   `check` shows a box at quit.
3. `bench.bat setup`.
   - It asks for the OoT file (or reads it from the game list config).
   - It reads his hotkey bindings and writes `local.json`.
   - It makes a read-only backup of the save folder. The runner never writes to his saves; a
     restore happens only when he asks.
   - It checks AC power and the power mode.
4. `bench.bat check`: once windowed while he watches (~5 min; the boot script and its reference
   thumbnails), then twice fullscreen, unattended (~10 min). Each run must reach the fountain, pass
   the gates and quit cleanly.
5. Windows: Do Not Disturb on during sittings, plugged in, Best performance, the same volume as in
   every sitting. Leave the controller untouched, or unplug it if its stick drifts. The runner never
   changes Windows, driver or Adrenalin settings.

### A sitting end to end

1. **Here.** The agent preparing a build writes the sitting's plan: arms (base launcher plus
   environment), schedule, windows, gates and decision rules. It goes in the build
   (`dist/lichii-tuned/bench/plans/`) or into chat as one file. The replication sitting on r66, for
   example:
   ```json
   {"name": "r66 fountain: replicate sitting 1", "kind": "fountain", "build": "bfe83e2de4",
    "arms": {"N":   {"base": "perf-trace-noprofile.bat", "env": {}},
             "A43": {"base": "perf-trace-noprofile.bat", "env": {"EDEN_NX2_NATIVE_FLOAT_ATOMICS": "1"}},
             "A44": {"base": "perf-trace-noprofile.bat", "env": {"LICHII_TFB_BARRIER_LAZY": "1"}}},
    "warmup": ["N", "A43"],
    "blocks": [["N", "A43", "A43", "N"], ["A44", "N", "N", "A44"],
               ["A43", "N", "N", "A43"], ["N", "A44", "A44", "N"]],
    "settle_s": 60, "measure_s": 100, "cooldown_s": 60,
    "gates": {"probe_mhz": "sitting", "cpu_band_pct": 1.5, "draws": [2600, 3200],
              "settings": {"force_max_clock": true}},
    "rules": [{"item": "A43", "control": "N", "metric": "gpu_busy_ms", "better": "lower",
               "min_effect": 0.10, "guards": [{"metric": "back_work_ms", "max_worse": 0.10}]},
              {"item": "A44", "control": "N", "metric": "gpu_busy_ms", "better": "lower",
               "min_effect": 0.05, "guards": [{"metric": "back_work_ms", "max_worse": 0.10}]}]}
   ```
   (Both switches are off by default in r66 and show in the `TEST switches in effect` line.
   `"probe_mhz": "sitting"` means a band around the sitting's own median until the clock question
   is settled.)
2. **Owner, ~2 min.**
   - Plug in, Do Not Disturb, close other GPU users, put the controller down.
   - Drag the plan onto `bench.bat` and read the plan summary.
   - Press Enter and walk away. The console shows `LICHII BENCH RUNNING`, run 3/18, arm A43, and
     the next step.
3. **Runner, ~4.7 min a run:** ~28 s boot, ~25 s of menus, ~10 s loading, 60 s settle, 100 s
   measured, ~10 s quit, 60 s cooldown. A W launch plus 8 measured runs take ~50 min; the sitting
   above (2 W + 16) takes ~85 min.
4. **At the end:**
   - It beeps, prints `summary.txt` (per arm: fps, p99, spikes and the probe; per block: B minus A)
     and opens Explorer at `results\<session>.zip` (~2-4 MB for 8 runs).
   - The owner drops that one file into the chat, ~1 min.
5. **Here:** `python3 -I lichii-tuned/bench/analyze.py <plan> --zip <zip>` gives the validity table
   and the verdicts by the plan's rules, in minutes.
6. **Abort at any time:** hold F3 for ~1 s (polled with `GetAsyncKeyState`, so it works in
   fullscreen; no Lichii default uses F3, and setup checks his bindings), press Ctrl+C in the
   console, or close it. The socket closes, which releases input at once. Lichii is left running for
   him (an abort he starts).

### What it saves

- **Owner's time:** ~60 min becomes ~5 min. No copying, renaming or relaunching; no playing to the
  spot.
- **Wall clock:** about the same as today per run, but nobody has to be at the PC. Sittings of
  12-16 launches (1-1.3 h) for ~1% effects become practical; HANDOFF's designs for 2% effects need
  10-17 runs per arm.
- **Fewer wasted runs:** a GPU clock problem like r66's, a wrong setting, a missing switch, a cold
  driver cache or a CPU power dip is found in the run where it happens.
- **Analysis:** one maintained tool and pre-written rules instead of new scripts per sitting.

### Risks and how they're handled

| Risk | Handling |
|---|---|
| A blind press on the file select (copy, erase) | The boot script is confirmed by a windowed `check` the owner watches. Each press needs the expected screen first. No directions are pressed on an unknown screen. The save folder is backed up before each sitting. |
| A press sent twice after a connection error | Only read-only methods are retried; after an input error the runner re-reads the screen instead. Unit-tested against the fake server. |
| Controller hotkeys | The owner's actual bindings are loaded; a step that completes one is refused. Home combinations can't be completed by injection. |
| The owner's input mixes with the runner's | Instructions in the console. Thumbnail and draws checks around each window. Phase 2 adds a counter of physical input that invalidates a window. |
| Present-timed input at 300-650 presents/s in menus | Holds are computed from the measured presents per second. Phase 2 adds time-based input. |
| The runner reads the previous run's log, or makes Lichii write `eden_log_2.txt` | No handle across a launch; open with full sharing after the mtime moves; rotation and first lines checked; `eden_log_2.txt` invalidates the run. |
| `log_tail` hitching a frame | Not used. Gates and brackets come from the disk log. |
| A minimized window or focus loss (notifications) | Do Not Disturb. The end-of-window thumbnail fails when minimized, which invalidates the run. |
| GPU clock, CPU clock or power not as planned | The probe band, the ±1.5% CPU GHz and W band, and the power mode gate every window. |
| A cold driver cache in one arm | A W launch per pipeline-changing arm; windows gated on pipeline work. |
| The precache or preparation runs in a measured run | `prepare` first. A log with a precache is invalid. ab-47 re-runs the compute precache at each alternation, so it isn't run as an alternating fountain A/B until that is fixed (see "What stays fresh-launch only"). |
| A confirm box blocks the quit | `check` finds out whether OoT holds the exit lock; "Never" only if it does. WM_CLOSE re-sent to the main window, then a kill that is marked. |
| A kill during the config write, play time added | Configs copied at start and compared at the end; `restore-config` on request. |
| A hang or crash | Watchdogs, a minidump, the log copy, a retry in the same slot, a stop after two failed slots; a failure abort quits Lichii. |
| The runner's own load on the game | No server call within 2 s of a window except `frame_times` inside it. Outside windows, `status` about once a second and 144-360 px thumbnails at transitions. The runner at below-normal priority with EcoQoS. Automated runs are compared only with automated runs. |
| Port 6545 still bound by the last Lichii | Wait for the PID to exit and the port to close (`ensure_closed`). |
| Drift after arrival (draws fall ~5% in 2 min, GPU busy ~2.3% per 100 s) | The same window offset in every run and balanced order, as today. |
| Sleep or a Windows Update restart in a cooldown | The runner keeps the PC awake for the whole sitting and refuses to start with a reboot pending. |

### How we check it before trusting its numbers

1. **CI.** The runner's unit tests against a fake debug server, and the analyzer's tests on
   generated logs, on ubuntu; the ctypes module's tests on Windows. The analyzer reproduces r66
   sittings 1 and 2.
2. **The `check` runs** at setup.
3. **A/A sitting (~50 min):** W, then 8 x N (labelled N-a and N-b in ABBA BAAB order). The analyzer
   must find no effect. On r66's ~1.8 GHz this only shows that the runner works and how noisy it is
   in that state; the noise floor that new items are judged against is measured again once the
   probe reads the play band.
4. **Replication sitting (the plan above):** repeat r66 sitting 1 automated.
   - The verdicts must match sitting 1's: ab-43 and ab-44 DROP.
   - Two manual N runs (the owner launches perf-trace-noprofile.bat and stands at the fountain as
     today) are a gross check only: they catch a runner or server cost of a few percent, nothing
     under ~1%.
   - The clock state must be the same as in sitting 1, or the analyzer compares only the
     differences.
5. Only then are new items decided by automated sittings.

## Phase 1: r67, live switches and the lines the bench needs

The pieces with the most time saved for the least code. They're debug-server tools or diagnostic
lines, off unless `EDEN_NX2_DEBUG_SERVER` (or the named variable) is set, as AGENTS.md asks of
diagnostics. Size: roughly 250-350 lines of C++ plus ~200 lines of tests, sized properly when r67
is planned.

### What it delivers to the owner

- **Fritchii's live A/B.** One launch, Link standing at the fountain. A switch is flipped every
  30 s (90 s for power-changing switches) in ABBA BAAB units, with a null flip to measure the noise
  floor. Several switches can run one after another in the same launch.
- **Self-describing logs:** every window and every flip is marked in `eden_log.txt`, and a boot
  line lists every `EDEN_*` and `LICHII_*` variable of the launch, so any arm can be checked.
- **The clock question answered** by a TurboMode boot line.

### Code changes (r67)

1. **Live switches, ported from Frizti's 93487071d5** (the numbered slots), with the `switch`
   method from 6bec8231a2 (git author "thibaut", cherry-picked into Fritchii from a281376cc8), by
   hand.
   - **Where:** `src/video_core/nx2_debug_control.h`. Ours is upstream's 107-line file without
     Fritchii's named switches, hence the hand port.
   - **What comes over:** `g_experiments[64]` (`std::atomic<int>`), `LoadExperiments()`,
     `Experiment(n)`, `FindSwitch()` and `SwitchValue()`, with Fritchii's names and file, so the
     Fritchii v6 merge (which brings the multithreading) lands on the same code. Not Fritchii's
     default table (EXP8=15, 9=3, ...): our ported EXP sites keep their own defaults until v6 is
     reviewed. Our switches are added by name, not as new slots, since new slots of ours could
     collide with v6's.
   - **The handler,** in `src/core/debugger/nx2_debug_tools.cpp`: `switch {name?, value?}` returns
     the previous and new values. Without a value it reads; without a name it lists every live
     switch and its value; boot-only names are refused with "boot-only: relaunch".
   - **How the value is applied:** as Fritchii does, the handler stores straight into the atomic
     (frz_debug_tools.cpp:466-476), and readers load it where they already read the switch. A
     frame-end queue is added only for a switch whose review shows it must change at a frame
     boundary, and that queue is reviewed on its own.
   - **Changes belong to the client.** The server remembers each switch's value before a client
     first changed it, and when that client disconnects it puts the values back and logs
     `Live switch NAME: b -> a (client gone)`. So an aborted or crashed runner never leaves the
     owner playing an experimental setting. A reconnecting runner sees its own switches reverted;
     the segment that was running is invalid, and the analyzer drops it by that line.
   - **Every flip is logged:** `Live switch NAME: a -> b at present N`.
   - **Switches registered in r67:**
     - `LICHII_AB_NULL`: registered, logged, read by nothing. The null flip for A/A units.
     - `LICHII_AB_BURN_US`, the positive control: at the frame end in TickFrame the thread spins
       N µs (0 = off, a single relaxed load). Its expected effect is known: about N µs a frame of
       back work, and of frame time when the back thread is the limit. A GPU twin (a dispatch of
       known length) is optional.
     - At most one real switch, chosen when r67's contents are set, from Phase 2's list or a new
       r67 item built switchable from the start. `LICHII_TFB_BARRIER_LAZY` is not it: r66 sitting
       1 decided it (DROP) and its switch leaves r67, like native atomics.
   - **PGO in normal play.** A converted switch must not change the control flow of a hot function
     in the shipped build, since clang then drops that function's profile, and pgo-train waits
     until after the multithreading merge. Converted readers keep their CFG (a member or static
     read becomes an atomic load at the same place, no new branch). CI reports the
     `-Wprofile-instr-out-of-date` and "control flow change" warnings for the touched files and
     fails on any increase over the previous build. A converted switch must also cost nothing when
     off: Frizti reverted EXP47 because its bookkeeping ran with the switch off.
2. **`mark {text}`**: `LOG_INFO(Debug, "Debug server mark: {} (present {})")`, returning the
   present count and uptime. ~12 lines. One log line, no ring read. The runner marks every window
   and segment; windows start 2 s after the opening mark and end 2 s before the closing one.
3. **The environment line:** one boot line listing every `EDEN_*` and `LICHII_*` variable in the
   process environment, with its value. ~25 lines, read once at boot. The runner's
   `LICHII_AB_ARM` and `LICHII_AB_RUN` show in it, so the arm and the run are checked from the log
   itself.
4. **The TurboMode line:** `Force maximum clocks: running` or `skipped (<reason>)`: the setting
   off, a driver not on the validated list, the Steam Deck, or the debugging tool named.
   ~10 lines.
5. **A pipeline-build counter** in an existing unprofiled 10 s line (`Async translate in the last
   10 s` or `Worst frame in the last 10 s`): pipelines built in the 10 s, by Lichii and by the
   driver, and their wall time. ~30 lines. Gates rule 7 from r67 on.
6. **`frame_times` checks `client_gone`**, so an aborted call ends at once. ~3 lines.
7. **Optional, our patch:** `EDEN_NX2_DEBUG_RING=0` keeps the log ring off while the server runs
   (`log_tail` then returns nothing), so bench runs don't pay the second formatting. ~5 lines.
   Only if the bench never needs `log_tail`.
8. **Adapter, tests and docs:**
   - `tools/nx2/debug_mcp.py` gets tool entries for `switch` and `mark`;
     `tools/nx2/test_debug_mcp.py` tests them.
   - `src/tests/core/nx2_debug_server.cpp` gets the registry tests (environment value, set, list,
     refusal of boot-only names, the log line, revert on disconnect), `mark` and the `frame_times`
     abort.
   - `docs/nx2/debug-mcp.md` and our AGENTS.md method list are updated.
9. **Runner and analyzer:**
   - A `live` kind in plans. The runner flips with `switch`, waits out the settle, marks each
     segment, measures with `frame_times` in calls of 25 s or less, and restores the starting
     values at the end (the server's revert covers aborts).
   - `analyze.py`'s live mode reads the `Live switch` and `Debug server mark` lines (the prototype
     has it, validated on synthetic logs), with per-segment back-thread GHz and package W.

**Deferred** from the earlier draft: `quit` (WM_CLOSE to the main window does it), time-based
input and the owner-input guard (needed for routes, so Phase 2), `draws_per_frame` in `status`
(without the frame trace it needs per-draw counting, which costs frame time in normal play; all
arms carry the perf-trace base instead), the per-variant precache stamp for ab-47 (it changes the
precache system, which isn't test tooling) and factorial sittings.

**Upstream.** `mark`, the `frame_times` abort, and later time-based input and the guard, are plain
debugging tools that upstream would likely accept: their phase 2 plans pause and resume through
frontend callbacks, and #778 already took Fritchii's `speed_limit`. Offer them as pull requests
once they've run here. Upstream's own phase 2 registry ("the getter becomes a relaxed atomic load")
is the same mechanism: if upstream lands one, theirs wins and ours adapts to it, keeping Fritchii's
names. The experiment slots and converted switches stay in our patch (AGENTS.md: experiment
switches never merge to nx2).

### What the owner sets up once

Nothing new beyond Phase 0.

### A live sitting end to end

1. **Here.** A `live` plan, for example:
   - the base arm `perf-trace-noprofile.bat`;
   - 120 s settle after arrival (draws fall ~5% in the first 2 minutes);
   - `LICHII_AB_NULL`: 4 ABBA BAAB units (16 pairs, 16 min);
   - `LICHII_AB_BURN_US` at two sizes, each 2 units with 30 s segments and 1 unit with 90 s
     segments (measures carry-over directly);
   - the rules.
2. **Owner, ~2 min:** as in Phase 0.
3. **Runner, one launch:**
   - ~3 min to arrive, 2 min settle;
   - the null units, then the control. Each segment: `switch`, settle, `mark`, `frame_times` in
     25 s calls, `mark`;
   - restore, quit, copy the log, zip.
4. **Here:** analyze.py's live verdict: mean paired gain, 95% CI, pairs that agree, guards, spikes
   per segment, and pairs still needed.

### What it saves

- **A live comparison:** ~16 min, in one launch, for a 1% effect in frame time (about 7 pairs at
  30 s segments for 80% power). GPU busy needs about 12 pairs, so 16 pairs covers both.
  Power-changing switches at 90 s segments take about three times as long.
- **Fresh launches for the same effect:** about 14-16 runs per arm (2.5 h or more). The live design
  removes the launch-to-launch component (0.9-2.5% of GPU busy between launches): the per-launch
  GPU clock, the memory map, the arrival point.
- **Owner's time:** ~3 min a sitting.

### Risks and how they're handled

| Risk | Handling |
|---|---|
| A switch that is unsafe to flip mid-run (lost wakes, crashes, stale pipelines) | Only reviewed switches are registered. Boot-only names are refused. The classes come from the r66 review (below). |
| State built under the old value carries over (pools, caches) | The review names a reset at the flip (a generation bump, a pool clear, as Frizti's EXP32 and EXP42). Those switches get 60 s segments with a 10 s settle, or stay fresh-launch. |
| CPU power carries over between segments | Power-changing switches get segments of 90 s or more, or stay fresh-launch; per-segment GHz and W are reported. The burn control measures the carry-over. |
| A live mode that loses part of an effect (carry-over, too short a settle) | The positive control must be recovered within its CI at two sizes, live and by fresh launch. Agreeing with a null effect proves nothing. |
| An aborted runner leaves a switch flipped | The server reverts a client's switches when it disconnects, and logs it. A failure abort quits Lichii. |
| A flip that doesn't take effect | Each converted switch shows its effect in a counter or a log line. The analyzer checks it per segment. |
| PGO in normal play | Converted readers keep their CFG; CI fails on more profile-mismatch warnings in the touched files. |
| Drift within the session | ABBA BAAB cancels linear and quadratic drift. The null units measure what's left. |
| Optional stopping inflates false positives | The number of units is fixed in the plan. An early stop only by the plan's own rule. |
| Fritchii v6 conflicts | Fritchii's names and file are kept, and its default table isn't. v6's review reconciles the defaults. |

### How we check it before trusting its numbers

1. **Unit tests in CI** (registry, handler, revert on disconnect, `mark`, the `frame_times` abort).
2. **A live A/A** (`LICHII_AB_NULL`, 4 units) in the first r67 sitting. It must give no effect, with
   a 95% CI inside ±1% frame time. Its pair SD and CI half-width become that build's noise floor.
3. **The positive control.** `LICHII_AB_BURN_US` at two sizes (for example 200 and 500 µs, about 1.4%
   and 3.5% of a 14.5 ms back frame), run live and in one short fresh-launch block per size. Live
   must recover the injected back-work delta within its CI at both sizes, and agree with the fresh
   launches. Frame time is expected to follow only when the back thread is the limit, which r66's
   GPU-bound ~1.8 GHz state isn't. If live recovers less than the injected delta, live mode isn't
   trusted until the cause (settle, carry-over) is found.

## Phase 2: routes, more live switches, Claude driving the bench (r68 on)

### What it delivers

- **The five-load route (A-B-C-B-A) replayed unattended.** Smoothness items are decided by the
  existing hitch budget, per load: first visits and revisits.
- **More switches live.**
- **Queued sittings in time windows the owner sets, and build-against-build sittings after
  merges.**
- **Claude starting, watching and collecting sittings directly**, if Remote Control offers a
  folder.

### Code and script changes

- **Frizti's `input_record` and `input_replay`** (19201e42c0 and b13ec311b6; +555/-25 over 8 files,
  plus `EmulatedController::HasInputInjector`).
  - A `git merge-tree` onto r66 showed three trivial conflicts (an include, the MCP tool list and
    its test list).
  - Our fixes: store `seconds` in the file, so time replay works without an explicit
    `frame_rate`; keep every path inside the dump folder (Fritchii accepts absolute paths); stop
    the replay clock while nothing is presented, or replay one leg per load, so a load of another
    length doesn't put the route out of step.
- **Time-based input**, ~20 lines: `timing: "time"` on `input` and `input_sequence`, so steps are
  given in seconds. This is Frizti's design (b13ec311b6: "Ocarina of Time moves by elapsed time, so
  a replay that advanced one step frame per present drifted off the recorded path").
- **Owner-input guard**, ~40 lines: `owner_input` in `status` (a count of npad samples whose
  physical, non-injected buttons or sticks left neutral, from a raw getter in
  `EmulatedController`), and in `RunInput` a release and an `owner input` error when the physical
  input leaves neutral.
- **Route scenes.**
  - The owner walks the route once with `input_record` (at a sitting's start, ~5 min).
  - The runner splits it at the fades into legs: fountain to A, A-B, B-C, C-B, B-A.
  - Each leg starts after its fade ends and the spawn thumbnail matches, and the next fade must
    come within the recorded time ±3 s.
  - Three baseline replays validate the route; a leg that drifts is recorded again.
  - A shuttle scene (A to B and back, repeated) gives live tests of revisit loads.
- **More live conversions,** one review each, from the r66 switch classes:
  - `EDEN_NX2_ARB_SPIN` and the other bounded spins (live, 90 s segments: they change package
    power);
  - `EDEN_NX2_EXP42` and `EXP43` (live; Frizti flipped both live);
  - `EDEN_NX2_TEST_1571` (60 s segments, carry-over);
  - `LICHII_WFI_DEFER` and `LICHII_REPORT_DRAIN` (at frame end or after a drain);
  - `LICHII_FSR_SLIM` through the existing output-format rebuild (5 s settle);
  - and whatever new GPU and smoothness items are built switchable from the start.
- **Better live precision:**
  - `mark` also carries cumulative counters: renderer frame, back and front thread cycles
    (`QueryThreadCycleTime`, whose handles the clock trace already holds), and the GPU busy sum and
    sample count;
  - GPU timing sampled every frame while a live plan runs; today `trace_gpu_rate` samples once a
    second, which is most of the per-pair GPU-busy noise;
  - the 10-second reporters cut their windows at a mark.
- **`serve`:** the runner watches `C:\LichiiBench\queue\`.
  - It runs only plans the owner approved, and only inside a time window he set when he started
    `serve` (for example tonight 01:00-06:00), or after a keypress at the PC. It never starts
    because the PC looks idle: fullscreen Lichii and game audio could interrupt a video or a call.
  - `status.json` and `control.json` let Claude watch, pause or stop.
- **Factorial sittings:** each fresh launch also runs a live unit set, which answers a boot-only
  and a live question in one sitting.
- **Optional:**
  - `LICHII_BENCH_USER=<name>`, a session-only profile choice, for a separate Bench profile with a
    golden save restored under a SHA-256 manifest (this also keeps bench play time and config
    writes out of his profile);
  - a `setting` method for a short allow-list (the frame limit, `force_max_clock`) flipped live
    without saving;
  - `quit`, if a case turns up that WM_CLOSE can't handle.

### How results reach this session

In order of preference:

1. **Remote Control.**
   - The owner shares `C:\LichiiBench` in the Claude desktop app (Settings > Claude Code > Remote
     Control), if that page offers adding a folder; it isn't available to every account yet, and
     none is shared now.
   - This session then lists and requests the folder; the owner approves each attach.
   - Claude queues plans for a `serve` window the owner started, or starts `bench.bat run` after
     the owner's go-ahead in chat for that sitting. It reads `status.json`, logs and thumbnails
     directly, and writes `control.json` to pause or stop. No upload.
2. **Claude Code on the laptop,** in the desktop app, run in `C:\LichiiBench`, with a
   `BENCH-AGENT.md` holding these rules and the current plan.
3. **The zip,** as in Phase 0.

### Rules for Claude, added to our AGENTS.md

- A sitting starts only after the owner's go-ahead, or inside a `serve` window he set.
- One client on the debug server, one request at a time; no `log_tail` without `after`.
- Nothing runs while the owner plays.
- Switches and rejected shaders are restored at the end (the server also reverts switches on
  disconnect).
- No input outside the plan's steps.

### Checks before trusting route numbers

- Three baseline replays with every spawn thumbnail matching.
- A route A/A (S against S, four runs each) with the hitch budget showing no effect.
- One replayed sitting against a manual route sitting from r66 sitting 2 (S and ab-45), which must
  agree within the 20% S1/S2 spread already seen.

## Phase 3 (optional)

- **Frizti's flash watch** (cafd28d7dc, 1d2293c968; ~1,450 lines, hooks placed by hand in our
  vk_rasterizer.cpp).
  - It gives unattended routes a flash count, which thumbnails can't, measured against a
    camera-motion baseline as Frizti does ("175 flash reports (camera noise ~270)").
  - It is for verify runs only, with both arms set the same: watching passes ends the render pass
    at every framebuffer change. `EDEN_NX2_FLASH_WATCH_PASSES=0` (the presented image only) is the
    lighter mode.
- **Ocarina loading counters in `status`** (~30 lines): the native zstd totals already kept in
  `svc_nx2_zstd.cpp`, and the resource request manager's pending count (+0x1E0 of the poll SVC's
  object, from #717).
- **Link's position** through a scan kept on the server (~250 lines), plus a pointer path from
  main's static data; about a day of agent work with the owner moving Link on request. It only pays
  off for walks with position feedback.
- **`cache_bench.py`'s cold and warm modes** for pipeline-store items (ab-46), with the AMD driver
  cache directory added to `driver_cache_dirs`.

## What stays fresh-launch only

| Kind | Examples | Design |
|---|---|---|
| SPIR-V changes | `EDEN_NX2_NATIVE_FLOAT_ATOMICS` (ab-43), `LICHII_UPSTREAM_GLOBAL_ROUTING` (ab-47), `EDEN_AMD_FP16`, translator merges | Fresh launches with a W launch per arm. Pipeline keys don't include them, so a live flip would reuse stale pipelines. ab-47 also re-runs the compute precache at each alternation, so the gate refuses those runs; it is decided by blocks of launches per arm, or after a precache change made as its own item. |
| Made at boot or device creation | `EDEN_NX2_AMD_PIPELINE_BINARIES` (ab-46), `EDEN_NX2_PREALLOC_MIB`, `EDEN_NX2_STAGING_RING_MIB`, TurboMode, `EDEN_NX2_SPLIT`, pinning, `LICHII_FSR_EARLY_CONTEXT`, the JIT options, the Ocarina patches | Fresh launches; ab-46 with cache_bench's cold and warm design |
| Hazards when flipped | `EDEN_NX2_KSPIN`, `EDEN_NX2_CACHE_LOCK_WAIT` (a lost wake hangs a guest thread), `EDEN_NX2_CORE_LOCK`, `EDEN_NX2_CONTEXT_GUARD`, `LICHII_FAKE_OCCLUSION` (Fritchii: "switching the counter query mode mid-game crashed") | Fresh launches; never registered |
| The GPU profile itself | `EDEN_NX2_GPU_PROFILE` | Separate sittings: fps arms unprofiled, zone arms profiled |
| One-time costs | first-visit shader builds, first loads, cold caches | Fresh launches or route first visits |
| Build against build | after a merge, PGO variants | Fresh-launch blocks with one arm per build folder and a W launch per build; the per-build stamps keep up to four builds, so alternating doesn't prepare again |

## Decision rules and schedules (reference)

**Fresh-launch rule** (the owner's r66 form). ADOPT needs all of:

- the minimum gain;
- a gain larger than the control arm's spread;
- every item run better than every control run;
- no guard tripped.

DROP if there's no gain or a guard tripped. Otherwise INCONCLUSIVE, with the runs per arm needed.

**Live rule.** ADOPT needs all of:

- the mean paired gain over the plan's minimum effect;
- a 95% CI that excludes zero in the better direction;
- at least three quarters of the pairs agreeing;
- a gain larger than the null item's 95% CI half-width (not a multiple of the null's d, which is
  unstable when d is near 0);
- no guard tripped (for example back CPU no worse than 0.10 ms a frame).

DROP if the CI excludes the minimum effect. Otherwise INCONCLUSIVE, with the pairs needed.

**Early stop, only when the plan says so.** After 2 of 4 units, the plan may stop if all 8 pairs
agree and the gain is at least 3x the minimum effect. That is for Frizti-size effects (2-8%).

**How many pairs.**

- Frizti's "4 balanced cycles", "each won all 4 pairs" (a one-sided sign test of 1/16) suits his
  2-8% effects. Ours are 0.25-1%.
- At 30 s segments the r66 data gives per-pair SDs of 0.9% (frame time), 1.2% (GPU busy) and 2.0%
  (back CPU), measured in r66's GPU-bound 1.8 GHz state.
- So 16 pairs is the default per item. 0.5% effects need ~25-48 pairs. Back CPU needs Phase 2's
  cycle counters for usable precision.
- These come from 11 short runs. The A/A sittings refine them per build, and they are measured
  again in the play clock state.

**Default schedules.**

- **Fresh fountain:** one W per pipeline-changing arm (one W otherwise) + A B B A | B A A B
  (9-10 launches, ~50 min). Add blocks for smaller effects.
- **Live fountain:** 120 s settle, then 4 units of ABBA BAAB with 30 s segments (5 s settle
  discarded, 25.1 s = 5 hitch periods measured); 60 s segments with a 10 s settle for cache-policy
  switches; 90 s or more for power-changing switches. One null item per build's first sitting. One
  switch at a time, back to baseline before the next.
- **Route:** W + S M M S M S S M (~52 min). Compare per load, first visits and revisits, by the
  hitch budget.
- **Shuttle (live):** one flip per round trip in ABBA BAAB order. Only revisit loads are compared.

## Order of work

1. **Now (Phase 0):**
   - write `oot_bench.py` (with the no-resend wrapper, the log identification and the ctypes
     module), `screens.py`, `bench.bat`, the .bat parser, the boot script and the tests;
   - move the analyzer into `lichii-tuned/bench/analyze.py`, with the spike estimate and the new
     validity checks, and generated-log tests;
   - CI: tests on ubuntu and the path-filtered Windows job; the `bench-kit-v0` and `bench-python`
     release assets;
   - HANDOFF and README sections.
2. **The owner's setup and `check`,** then the A/A sitting and the replication sitting on r66, with
   the probe band set to the sitting's own median until the clock question is settled.
3. **r67 (Phase 1)** with the items above, then a first r67 sitting in one launch: the live A/A
   and the positive control (~45-60 min), plus its short fresh-launch blocks.
4. **Phase 2 as needed:** the route recording when the next smoothness items are ready, live
   conversions as items come, Remote Control if offered.

## Questions for the owner

1. Is Python installed on the laptop? If not, the `bench-python` asset is the one extra download.
2. Is Player 1 a Pro Controller, and can the controller stay untouched (or unplugged) during
   sittings?
3. Has Lichii ever asked "Are you sure you want to close Lichii?" when you quit Ocarina? If it
   has, "Confirm before stopping: Never" is needed during sittings; `check` will show it either way.
4. From the title screen, which buttons take you to Link at the fountain (for example: A at the
   title, A on file 1, A to confirm)? Does loading always put Link at the fountain, facing the same
   way? Does Ocarina autosave? The save folder is backed up either way, and the runner never writes
   to it.
5. What is the path of the Ocarina file for `-g`? `setup` can read it from the game list instead.
6. Have you changed any controller hotkeys in Lichii? `setup` reads them, but it helps to know.
7. Does Remote Control's settings page offer sharing a folder on your account?
8. Should GPU A/Bs wait until Force maximum clocks reads ~3.1 GHz again, as in r64? r66 read
   ~1.79 GHz with it on.

## Credit

- **Frizti (Fritchii):**
  - the method this plan follows: a standing spot, live flips in balanced cycles, per-pair
    verdicts, a control flip for the noise floor, verify modes that must report 0 before a default
    changes, and fresh launches for what can't be flipped;
  - the numbered live slots (93487071d5);
  - `input_record` and time-based `input_replay` (19201e42c0, b13ec311b6);
  - the flash watch (cafd28d7dc, 1d2293c968);
  - booting through the menus with the speed limit off (5b562f8077).
- **The `switch` method** entered Fritchii in 6bec8231a2 (git author "thibaut", from a281376cc8).
- **Upstream Lichii:**
  - the debug server and its input tools (#111, 27e10e043e);
  - `cache_bench.py` (#975, Zortos), whose launch, alternation, guard and restore patterns the
    runner reuses;
  - `speed_limit`, imported from Fritchii in #778.

## Review points not taken, or taken in part

- **11, the TurboMode explanation.** The boot line is kept, but not the claim that
  `ShouldBoostClocks()`'s debugging-tool check fits the Radeon Developer Panel explanation. The
  check covers only RenderDoc, Nsight Graphics and "Radeon GPU Profiler" from VK_EXT_tooling_info,
  and `CollectToolingInfo` (vulkan_device.cpp:2747-2754) logs `Attached debugging tool: <name>`
  for each; the r66 log (ab_44_1, c471eeec) has no such line, so TurboMode most likely ran.
- **13, the `lazy_xfb_barrier` example.** The CFG rule is taken; the example is moot. r66 sitting
  1 decided `LICHII_TFB_BARRIER_LAZY` (DROP) and its switch leaves r67, so it is not converted.
  The positive control takes its place as the live validation.
- **15, "kept on E-cores".** Not taken as a hard affinity. r66 runs with `pin_gpu_threads` off,
  and Lichii's core placement puts HostTiming on the E-cores (8 logical processors) itself
  (hybrid_cpu.cpp:235 in the r66 log), so a runner held there would share them with HostTiming.
  Below-normal priority with EcoQoS stays.
- **19, the Home defaults.** Loading the owner's real bindings is taken. The Home+... defaults are
  not a risk: injected input has no Home button, and the hotkey check also needs
  `GetHomeButtons()`, which doesn't include injected input (emulated_controller.cpp:1802-1806).
- **5, a common multiple of the periods.** 5.027 s and 4.157 s have no practical common multiple.
  Windows are cut to whole 5.027 s periods where possible, and spike removal does the rest.
- **10, hashing the configs.** Taken as a key-by-key comparison instead: every exit rewrites
  `qt-config.ini`, and `-f` changes the fullscreen state, so a plain hash would flag every run.
- **2, "no server call within ~2 s of a window".** Taken, with two stated exceptions:
  `frame_times` inside the window (its handler only sleeps and reads the present ring's atomics),
  and r67's `mark`, a single log line; windows start 2 s after the opening mark.
