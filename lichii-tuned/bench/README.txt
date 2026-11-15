Lichii Bench (Phase 0, r66)
===========================

Lichii Bench runs fresh-launch A/B sittings at Ocarina's Market fountain on its own: it starts
each arm, presses A through the menus to Link's save, waits, measures, quits Lichii, copies the
log and checks it, then starts the next arm. At the end it writes one zip to upload.

It only ever presses A (and B to back out of a screen it doesn't know). It never touches the
sticks or the D-pad, so Link never moves, and it presses nothing while the game shows the
world. It never writes to your saves; setup makes a copy of them.

Where the files go
------------------
Put bench.bat in the build folder (next to lichii.exe) and this folder as bench\ beside it:

    <build>\lichii.exe
    <build>\bench.bat
    <build>\bench\oot_bench.py ...

Python 3 is needed (3.13 is fine): bench.bat uses bench\python or C:\LichiiBench\python if one
is there, else the installed "py -3".

The bench's own data is in C:\LichiiBench: local.json (setup), refs\ (the menu screens check
records), sessions\ (one folder per sitting), results\ (the zips), save-backups\.

Once
----
1. In Lichii: "Pause emulation when in background" off (the default), Player 1 connected.
   "Confirm before stopping emulation" can stay on "Always ask": the bench answers the
   "Are you sure" box itself. If check says the box wasn't answered, set it to "Never ask"
   for sittings.
2. bench.bat setup
   It finds the Lichii data folder (%APPDATA%\lichii), asks for the Ocarina file, reads your
   controller hotkeys (the bench refuses any press that would complete one), copies the saves
   and writes C:\LichiiBench\local.json.
3. bench.bat check
   First a windowed launch you watch: at each menu the console asks whether the expected screen
   shows (click the console window to answer). Press Enter to let it press A; [w] waits, [q]
   stops. That records the menu screens. Then two fullscreen launches run on their own and must
   reach the fountain, pass the checks and quit cleanly. Upload the zip it names if one fails.
4. For a new build: bench.bat prepare once (the first launch's shader preparation, up to 75 min,
   unattended).

A sitting
---------
- Plug in, Do Not Disturb on, Windows power mode "Best performance", the usual volume, other GPU
  programs closed, the controller put down.
- Drag the plan (a .json file) onto bench.bat, read the summary, press Enter and walk away.
- To stop: hold F3 for about a second, or press Ctrl+C in the console. Lichii is closed and what
  ran so far is kept. To stop after the current run instead, set "stop_after_run": true in the
  session's control.json.
- At the end it prints a summary and opens Explorer at results\<session>.zip. Upload that file.

What it checks in every run
---------------------------
The build, the game, the settings (Force maximum clocks on, frame generation off, the 120 fps
limit, exclusive fullscreen, HDR off, the speed limit on), the arm's switches, AC power and the
power mode, no shader precache or preparation and no shaders compiling in the measured window,
the draws a frame at the fountain, the GPU clock probe, and that the log is this run's (not the
last one's, no eden_log_2.txt). Across the sitting: back-thread GHz and package watts within
1.5% of the sitting's median, and the same settings in every run. A run that fails is retried
once in its slot; if the retry fails too, its block is dropped and run again at the end. Two
failed slots in a row, or a problem a retry can't fix (wrong build, setting, switch, clock state
or power mode), stop the sitting.

The zip holds the logs, each run's run.json and thumbnails, the summary and the runner's log.
It never holds saves, configs, keys, nand or sdmc. The logs contain your Windows user name in
paths, so keep the zip private.

Other commands
--------------
bench.bat validate <plan>          check a plan without running it
bench.bat zip <session>            zip a session again
bench.bat restore-config <session> put back the configs copied at the start of that session
                                   (every launch rewrites Lichii's UI settings)
