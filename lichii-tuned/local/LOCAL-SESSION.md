# Lichii tuned: the local session (start here)

This file moves the work from the cloud session (2026-11-07 to 2026-11-16) to a Claude Code
session that runs on the owner's laptop. Read it in full, then CLAUDE.md, AGENTS.md and
lichii-tuned/HANDOFF.md (the r64-r67 entries and the Lichii Bench entries at the end of its
latest-round section are the current state), lichii-tuned/ab-automation-plan.md and
lichii-tuned/bench/README.txt. CLAUDE.md holds every owner rule with its date; it still applies
in full.

## Why a local session

The owner's new way of working (2026-11-16): "What I want is for claude to automatically continue
an iterative process of testing on a particular goal that I set. When the goal is reached, claude
can stop the tests and notify me, either with successful results, or if the goal is deemed
impossible, with alternative paths forward. I want to compile builds on my laptop's hardware
instead of github, and only push to github when a stable build is completed, at my say so."

A cloud session can't reach this laptop (Remote Control lists its folders as not_available), so it
can't build here or run the bench. This session can: it edits the source, builds it with the
laptop's toolchain, runs Lichii Bench, analyses the results and decides the next step, in a loop,
without anything leaving the laptop until the owner says "push".

## The laptop

- Core Ultra 7 255H (6 P-, 8 E-, 2 LP E-cores, no SMT), 32 GB, Windows 11; RX 9070 XT over
  OCuLink (PCIe 4.0 x4, Resizable BAR off), AMD driver 2.0.416; one 4K 160 Hz FreeSync monitor.
- The game: The Legend of Zelda: Ocarina of Time (0400486024DEC000), docked, 1440p rendered,
  FSR 4 to 4K, frame generation off in play, Force maximum clocks on (the GPU at ~3.15 GHz;
  a Radeon Developer Panel "Stable" clock once held it at ~1.8 GHz: check the probe line).
- Installed: Python 3.13, Git, GitHub CLI (signed in as DahBard; scopes repo, workflow, gist,
  read:org), the Claude desktop app. To install: MSYS2 (below).
- Folders (suggested; ask the owner for the real paths and write them here):
  - `C:\lx\blitchii`: a clone of DahBard/Blitchii, branch `tuned` (docs, CI, patch, bundle, bench).
  - `C:\lx\lichii-src`: the source, branch `tuned4`, as a worktree of that clone. Code changes go
    here.
  - the owner's "Forked Lichii Builds": one folder per build (CI's r66, r67; local builds l001 ...).
  - `C:\LichiiBench`: the bench's data (setup, refs, sessions, results).

## One-time setup

1. MSYS2 from https://www.msys2.org (default C:\msys64). In the "MSYS2 UCRT64" shell:
   ```
   pacman -Syu
   pacman -S --needed git make autoconf libtool automake-wrapper patch jq pactoys ninja
   pacboy -S --needed toolchain:p clang:p lld:p llvm:p compiler-rt:p cmake:p python-pip:p qt6-base:p qt6-charts:p qt6-tools:p qt6-translations:p qt6-svg:p openssl:p boost:p fmt:p lz4:p nlohmann-json:p zlib:p zstd:p libiconv:p vulkan-memory-allocator:p vulkan-devel:p glslang:p enet:p opus:p libusb:p unordered_dense:p jbigkit:p xz:p libdeflate:p bzip2:p lerc:p graphite2:p libwebp:p libtiff:p brotli:p ccache:p
   ```
   (the list CI installs; lichii-tuned.yml's "Set up MSYS2" step is the reference).
2. The repository and the source:
   ```
   git clone --branch tuned https://github.com/DahBard/Blitchii.git C:/lx/blitchii
   cd C:/lx/blitchii
   git fetch lichii-tuned/tuned4.bundle tuned4:tuned4
   git worktree add C:/lx/lichii-src tuned4
   ```
   tuned4 holds every source commit since the upstream base `r22-up-5` (0db03a262f, reachable
   from `tuned`). Check: `git -C C:/lx/lichii-src log --oneline -1` is 089853e566 (r67) or later.
3. The pre-push guard: copy this into C:/lx/blitchii/.git/hooks/pre-push (the old public fork
   DahBard/Lichii must never receive anything):
   ```
   #!/bin/sh
   case "$2" in *DahBard/Lichii|*DahBard/Lichii.git|*dahbard/lichii|*dahbard/lichii.git)
     echo "pre-push: refusing the old public fork" >&2; exit 1 ;; esac
   exit 0
   ```
4. The first build (CMake downloads its CPM packages once; ccache makes later builds fast):
   ```
   bash /c/lx/blitchii/lichii-tuned/local/build-local.sh /c/lx/lichii-src "/c/<builds folder>" l001
   ```
   It mirrors CI's pgo-lto build and packaging (PGO with Lichii's profile plus the owner's
   recorded ones, ThinLTO, -march=x86-64-v3 -mtune=arrowlake -ffp-contract=off, symbols kept in
   <build>\symbols, AMD's FidelityFX DLLs pinned by SHA-256) into <builds>\l001. It was written in
   the cloud and has not run yet: fix it here if the first run fails, and commit the fix.
5. Lichii Bench: copy bench.bat and bench\ (from C:/lx/blitchii/lichii-tuned/bench) into the build
   folder you test, or run it from the clone. `bench.bat setup` and `bench.bat check` are done
   for r66 already (refs in C:\LichiiBench\refs). If the autopilot was installed
   (`bench.bat install-autostart`), remove it with `bench.bat uninstall-autostart`: this session
   drives the bench now, and two drivers would fight over Lichii.
6. Permissions: let the owner approve, once, an allowlist in C:/lx/blitchii/.claude/settings.json
   (and the same for C:/lx/lichii-src) for the loop's routine commands, so it doesn't stop for
   prompts: `git` (not `git push`), the MSYS2 bash running build-local.sh, `python -I` on
   bench/analyze.py and the bench scripts, `bench.bat run|validate|zip|prepare`, and reading the
   builds folder and C:\LichiiBench. Never enable bypass permissions mode. `git push` always asks.

## The goal loop

The owner writes a goal in `C:/lx/blitchii/lichii-tuned/local/GOAL.md` (template:
GOAL.example.md). Then, in this session: "work on the goal". Each iteration:

1. Read GOAL.md, the iteration log (GOAL-LOG.md beside it) and HANDOFF's newest entries.
2. Pick the next candidate: upstream first (port before writing; AGENTS.md "Before writing a fix"),
   then Fritchii/CheezyEmu at the owner's bars, then new code. Put each candidate behind its own
   switch, off by default (the change cycle in community/AGENTS.md and HANDOFF).
3. Implement it on tuned4 in C:/lx/lichii-src (commit locally), review it adversarially (a second
   agent that tries to refute it), build with build-local.sh under the next label.
4. Write a bench plan with the decision rule BEFORE the runs (bench/plans/*.json for the format;
   arms differ only in the candidate's switch; balanced blocks; matched windows; the fountain for
   frame time and GPU busy, the route only once route replay exists), run it with
   `bench.bat run <plan>`, analyse the zip with `python -I bench/analyze.py --zip <zip>`.
5. Record the result in GOAL-LOG.md and HANDOFF (numbers, verdict). ADOPT: make it the default
   (keep an off switch); DROP: remove its code; INCONCLUSIVE: more runs if the rule says how many,
   else drop.
6. Check the goal's stop conditions. Reached: stop and notify the owner with the results.
   Impossible (the budget is spent, every candidate in the plan is dropped, or a measurement shows
   the limit is elsewhere): stop and notify with the evidence and the alternative paths forward.
   Otherwise continue.

Notify the owner with a push notification (the PushNotification tool, if this session has it) and
a short report at the top of GOAL-LOG.md. Usage is limited per 5-hour window: commit at every
checkpoint and keep GOAL-LOG.md current, so the loop resumes where it stopped (the /loop skill
with self-paced wakeups can carry it across windows).

Stable builds and pushing: only when the owner says "push" for a build. Then, in C:/lx/blitchii:
regenerate the patch (`git diff --binary 0db03a262f tuned4 > lichii-tuned/lichii-tuned.patch`) and
the bundle (`git bundle create lichii-tuned/tuned4.bundle refs/heads/tuned4 ^r22-up-5`), check that
the patch applied to r22-up-5 gives tuned4's tree, update HANDOFF and CLAUDE.md, commit, check
`git remote -v` (origin must be DahBard/Blitchii), `git push origin tuned`. CI then builds rN and
runs the tests as before. Docs-only commits carry [skip ci].

## Rules that matter most in the loop (all of CLAUDE.md still applies)

- Performance first, native 1440p to 4K FSR 4, no GPU change that costs the back thread, no frame
  generation in play, upstream first, keep a working shortcut over an upstream change that costs
  performance (2026-11-14).
- Measure, don't extrapolate: unprofiled baselines, balanced A/B blocks, matched windows, rules
  written before the runs, the GPU clock and power state checked in every run. The bench enforces
  most of this; don't weaken its checks to make a run pass.
- Never touch the owner's saves or Lichii settings; only the bench starts Lichii, and it stops when
  the owner uses the PC. Never start or stop Lichii by hand.
- No personal data, no model names, no references to the old public fork in committed files.
- Commits end with the attribution lines in CLAUDE.md's history (Co-Authored-By and the session
  line the harness gives you).

## Current state (2026-11-16)

- r67 (CI run #7, tuned 2b9784cbba, tuned4 089853e566): r66 + native float atomics and the lazy TFB
  barrier removed, Force maximum clocks recommended, the frame trace's 4.157 s hitch fixed,
  title-screen launchers for the 5.027 s hitch, Fritchii's live switches with the debug server's
  switch and mark methods (credit Frizti). Unit tests 1,525/1,528 (3 skipped).
- Lichii Bench v1.1 works: the first automated sitting (A/A on r66, 9 runs unattended) found no
  difference where there is none, with ~0.8% per-run noise; r66 at the fountain = r64 (~55.4 fps,
  GPU busy ~12.8 ms, back work ~13.7 ms, 1% low ~36). Open bench fixes: the "screen changed
  during the window" flag fires on NPCs at the fountain (relax it), and analyze.py's window should
  follow the runner's window instead of its own arrival rule.
- The frame is held by the GPU back thread (~14 ms of work a frame at the fountain); heavy scenes
  miss 60 fps and the 30 fps 1% low target. The ~5.03 s hitch (one ~45 ms frame, locked to FSR's
  context made at the title) is the main 1%-low cost; the title-*.bat launchers in r67 tell its
  cause apart.
- Backlog: the 5 s hitch (title test, then a fix); r64's routing helpers against upstream's #1681
  form at the fountain (the 16 MiB stacks fixed the crash, so upstream's form may come back);
  #1571 (inconclusive, ~57 runs a side for a 5% effect); route replay for smoothness tests (plan
  Phase 2); Fritchii v6 (multithreaded back thread, the pot room fix) once the owner says where it
  is hosted; upstream merges.
