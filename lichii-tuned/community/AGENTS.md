# Tuning a Lichii fork for one machine and one game

These instructions are for AI coding agents on a personal Lichii fork whose goal is to make one game run as fast and as smoothly as possible on one owner's machine. Append this file to upstream Lichii's root `AGENTS.md`, or keep it in a folder of its own and point to it from there. Leave upstream's text as it is, since this file points to its debugging tools. Upstream's file is written for its own maintainer: take its rules and tools, not its PC or its build script. "Upstream" here means upstream Lichii: `nx2` and its pull requests.

The examples come from a Windows build tuned for The Legend of Zelda: Ocarina of Time (Switch 2) on a laptop with a Core Ultra 7 255H and a Radeon RX 9070 XT over OCuLink. In them, `r65` is a build number and `ab-26` an A/B launcher. Replace the examples; keep the rules.

The loop: build, the owner plays and sends `eden_log.txt`, analyse, build again. This build's agents had no GPU, so nothing they changed ran on real hardware before the owner's first launch. If that is your case too, say so in every test plan.

## 1. Goal and the owner's constraints

- **Record the target first**: the CPU and its core types, the GPU and its link, RAM, the display, the driver, the OS, the game's version and the owner's settings. Here: 6 P-cores, 8 E-cores and 2 low-power E-cores without SMT; the GPU on PCIe 4.0 x4 with Resizable BAR off; 32 GB of RAM; one 4K 160 Hz FreeSync monitor; AMD driver 2.0.416; the game docked at 1X (2560x1440). These details decide what helps. On this hybrid CPU, pinning the GPU threads to cores of their own made frames 27-34% longer, so the build places threads by core type instead.
- **Keep an owner section** in the file your agent reads first: one dated rule per line, newest last, in the owner's words. Read it before every task. A newer rule wins; note what it replaces instead of deleting the old one.
- **When a rule reads two ways**, write both readings down and ask the owner. "Medium-intensity areas ... hold a steady 50 fps or more" became reading 1 (every medium 10 s window at a median of 50 or more) and reading 2 (also 1% lows of 45 or more). The build passed the first and failed the second, and both results were kept.
- **Recommend; the owner decides.** Give your confidence, then record the decision with its date and a quote.

This build's owner section, abridged:

- 2026-11-07: the goal is a constant 60 fps, the game rendering 2560x1440 and FSR 4 upscaling to 4K (later agreed: with 1% lows above 30).
- 2026-11-10: upstream Lichii comes first. Where our code and upstream's do the same thing, upstream's wins.
- 2026-11-14: "the native 1440p upscaled to 4k, no compromises on that": no lower internal resolution, render scale or output.
- 2026-11-14: "do whatever guarantees the most performance. if the shortcut works, and I haven't reported any issue with it, then keep it in without the upstream change."
- 2026-11-14: "Make sure to not entertain any optimizations for the gpu that would cost us performance on the back thread." That rules out upload reordering: ~0.1 ms of GPU time for as much back-thread time.
- 2026-11-14: "We also will not be using framegen in gameplay ideally." Test runs have frame generation off.

## 2. Source priority

- **Upstream first.** Merge upstream regularly. Where your code and upstream's do the same thing, take upstream's.
- **Port before you write.** Run upstream's "Before writing a fix" checklist against upstream Lichii itself and against its base: `nx2`, open and closed PRs, unmerged branches and issues. Read the diffs (`git fetch upstream refs/pull/<n>/head`), not just the titles. Port an existing fix with `git cherry-pick -x` so its author stays, and cite it. Write a new fix only when nothing addresses the root cause the log shows.
- **Keep a working shortcut** that an upstream change would replace at a cost in performance (the owner's rule). Revert that change on your base branch and record why. Example: with upstream #1486, the front GPU thread would lose its view of Ocarina's QMD pages at every list-fed dispatch and wait for the back thread. The build kept its own path instead (from upstream PR #805, on since round 18, no problems reported), and upstream later reverted #1486 too.
- **Sort each upstream merge first** into take, needs a test (goes in behind a switch), left out (with the reason) and already in. Of merge 5's 136 commits, 24 were taken, 5 needed a test, 8 were already in and 9 were left out; the rest changed nothing here. Most of the nine cost frame time, replaced a working shortcut, or were due to be rewritten by a port already planned.
- **A fork specialised in your game** comes next, at a bar the owner sets. Fritchii, Frizti's Ocarina fork, needed a large benefit on this setup or in Ocarina. From Fritchii v6 the owner's rule is "default to changing to fritchii's unless our way of doing it is specifically tuned to our Hardware specifications". CheezerWheezer's CheezyEmu supplied the AMD FSR 4 upscaler and frame generation. They were ported on their own branch from one pinned commit (inventory, port, three reviews, fixes), without CheezyEmu's experiment switches.
- **Other forks** get in only for a very good reason or when the owner names one. Port pieces; don't migrate to another fork's tree.
- **Vet everything that isn't upstream's.** A fork's numbers are its author's claims, on its author's hardware, until you reproduce them. Don't port what a preferred source is about to ship: upstream #1314 waited for Fritchii's own fix.

## 3. The change cycle

Each round makes one build:

1. **Survey** upstream, the forks you follow and the owner's newest logs. For a big question (GPU time, hitches), have agents investigate by area and write one synthesis, then have skeptics recheck each item. Build only what survives.
2. **Port or write** each candidate on its own branch. One agent implements it and another reviews it (section 6).
3. **Put each candidate behind its own switch, off by default**, so the default build plays like the last one ("with none set, r64 plays like r63"). Name switches by source: `EDEN_NX2_TEST_<PR>` for upstream ports, a fork's own names for its items (Fritchii's `EDEN_NX2_EXP<n>`), and a prefix of your own for yours (here `LICHII_*`). Prove that off is off: with r64's native float atomics off, the SPIR-V was byte-identical to r63's in a test harness.
4. **Ship one build** with every candidate and one launcher for each: the baseline launcher plus exactly one switch. Log a boot line that names every test switch and its state (here `TEST switches in effect:`), so a switch that didn't take effect shows.
5. **Write the decision rule down before the runs.** Example: "#1216 is dropped if ab-26 minus the baseline is <= -0.2 ms" (ab-26 turns #1216 off).
6. **Give the owner a test plan**: the launchers in order, where to play, what to send back, and the log lines and counters to watch.
7. **Measure** (section 4) and **decide each item**:
   - A proven item becomes the default and keeps an off value. In r65 that was the FSR 4 context built at the title screen, 10-bit FSR output, a 4 GB memory reserve and a 256 MiB staging ring.
   - A disproven item is dropped, along with its code.
   - An unclear item is split off and tested alone: r64's bundle of GPU items became ab-43 and ab-44.
   - A correct change that measures neutral may stay, marked "no performance claim".
8. **Clean up** in a later build: settled switches and their launchers go. r61 removed 35 launchers and kept 13.

Measure; don't extrapolate. Builds A and B were planned to save 1.5-3 ms of back-thread time a frame and saved 0.15-0.45 ms, because the work moved between profile zones instead of going away.

Experiment switches never go upstream. Offer a fix upstream only with the owner's approval, and offer it as unconditional behaviour. Diagnostics stay off unless their variable is set.

## 4. Testing protocol

The owner credits these testing methods to Frizti, the author of Fritchii. Fritchii's commit messages record the measurements behind its changes, taken with:

- same-session A/B tests: switches toggled live against a control switch at a fixed spot;
- balanced pairs, counted pair by pair ("Castle Town toggle, 4 balanced cycles: 41.4 -> 44.5 fps, every pair positive", Fritchii commit 6c8b491012);
- standing runs, route runs and pairs of fresh launches;
- 1% and 0.1% lows;
- a flash watcher with a known noise floor;
- verify runs that must miss nothing;
- switches merged "off until measured".

Fritchii toggles its switches live through the debug server. If your switches can't change at runtime, relaunch for each arm and alternate the arms, as this build did.

- **Benchmark at a fixed spot**: the game's heaviest (here the Market fountain, ~2,750 draws a frame). Let each run settle for 60 s and compare its last 100 s. If a run had no settle time, compare windows matched by when it reached the same content.
- **Hold the settings fixed**, and check them in every log. Here: a 120 fps lock (above the target, so the cap can't hide a gain), frame generation and HDR off, exclusive fullscreen, docked at 1X with FSR 4 to 4K, Force maximum clocks on, and the Windows power mode on Best performance with the laptop plugged in.
- **Time against an unprofiled baseline** that logs only frame times and cheap traces (here `perf-trace-noprofile`). The GPU profile costs ~15% at the fountain, and even the cheap traces cost ~1 fps. Profile to find where time goes, never to compare builds. Compare an arm only with its own build's baseline: a stale PGO profile alone moved the back thread by 0-0.4 ms.
- **Change one variable at a time.** Each A/B launcher is the baseline plus one switch. Split a bundle that confounds, and plan a run for any confound you can't split.
- **Alternate the arms in one sitting** (N A A N, repeated). One build measured 10.2 and 13.1 ms median GPU busy in two different sittings, while two runs in the same sitting differed by ~0.3 ms and 1.3 fps.
- **Repeat enough.** Noise was ~1.5-2% of frame time per run once drift and session state were taken out. In some sessions the game's heap maps and unmaps cost 164-296 us a call, in others 51-91 us. A 4% effect needs ~4 runs per arm, and a 2% effect 10-17. An arm helps only if every block agrees and its mean gain beats the baseline's spread.
- **Use route runs for smoothness**: the same save and the same route through several area loads (here A-B-C-B-A). Make a stutter-trace launcher the route's baseline. Its steady frames run 2-5% slower, so compare it only with itself. Count the frames over 33.4 ms and their time over 16.7 ms, per load.
- **Metrics**: median fps; the 1% low (1000 divided by the mean of the slowest 1% of frame times in ms); the 0.1% low; the limiting thread's work per frame; and GPU busy time. Find the limiting thread first. Here it was the GPU back thread, so each millisecond of GPU time saved bought only ~0.1 ms of frame time. Group windows by content, not by their own thread times (here by draws a frame: under 1500, 1500-2100, 2100-2700, over 2700).
- **Count gameplay only.** Leave out the title screen, menus, loads and a new build's first launch, which has a cold driver cache and runs the shader preparation. A title-screen video was once read as a steady 60 fps, and a decision made on it had to be revisited.
- **Log the machine's state in every run**: the power mode, CPU clocks and package power, and the GPU clock. Each of these skewed results here:
  - a wrong power cord held the CPU at 35 W for at least a whole round of runs;
  - "best power efficiency" put the E-cores at 1.1 GHz and slowed the game;
  - in some sittings the driver's on-demand GPU clock made every GPU program 1.2-1.7x slower, until Force maximum clocks held it at ~3.2 GHz for ~60 W more.
- **Use verify launchers**: they run each shortcut against the slow path while the owner plays, and their mismatch counts must stay 0. Never time with them.
- **Write each sitting down**: date, build, the arms in order, the numbers, the parser, the analyst, and a verdict per item (make it the default, drop it, or keep testing it alone). Have a second analyst recompute every number with their own code.

## 5. Building

- **Keep one patch over a named upstream base.** The base is an upstream commit plus reverts of what you left out, on its own branch. The patch is `git diff --binary <base> <work-branch>`. After every change, regenerate the patch, check it with `git apply --check` on a clean checkout, and confirm that the base plus the patch equals your tree. Set `*.patch -text` in `.gitattributes`: a runner's `core.autocrlf` once broke a build by converting the patch to CRLF.
- **To move the base**, merge upstream into the work branch, and build each new base branch on the last one. Plan big merges by area. Start with a trial merge and a coverage check that assigns every upstream commit, and every file changed on both sides, to an area. Then write a table of where upstream replaces your code and where yours stays. Resolve in parallel with a reviewer per area, and finish with checkpoint greps that must return their expected results. Turn on `rerere.enabled` and review every reused resolution. Turn on `merge.directoryRenames`: it carried this fork's changes across upstream's directory rename.
- **Build in CI for the owner's platform.** Here that is Windows with MSYS2 UCRT64 clang, lld, Ninja and ccache:
  - `-march=x86-64-v3 -mtune=<the owner's CPU> -ffp-contract=off` (no fused multiply-adds the source didn't ask for, so float results match official builds);
  - Lichii's PGO profile plus ThinLTO (a profile recorded in the game added nothing measurable on top);
  - `ninja -k 0`, so one run lists every error.

  Keep an unstripped symbols artifact. Download redistributable binaries in CI, pinned by SHA-256, instead of committing them. Name each build in the log and the title bar. Mark commits that only change notes `[skip ci]`.
- **Run upstream's unit tests** (`tests`) on every patch push. Use a separate workflow with the same toolchain, so build numbers don't move.
- **Syntax-check every changed file for Windows before you push.** A Linux compile skips `#ifdef _WIN32` code. Use `clang++ --target=x86_64-w64-mingw32 -fsyntax-only` with the CI's flags (here `-std=gnu++20 -Werror=all -Werror=extra -Werror=missing-declarations -Werror=shadow -Werror=unused`).
- **Write down the rules build failures teach.** Example: no inline `thread_local` of a non-trivial type in a header (one broke the ThinLTO link).

## 6. Review

- **Review every change adversarially before it ships**, with agents that didn't write it. Patterns that worked:
  - one implementer, two reviewers and a fixer;
  - a finder per item and two skeptics per finding;
  - verifiers who try to refute each finding;
  - a second agent for each merge area.

  Build B's deep review found a major defect that its own verify switch couldn't see: a delta draw could read undefined vertex input.
- **Fix the confirmed findings, then review the fixes.** r55's fixes went through four lenses, with a refuter for each. Record which findings were refuted and which are unverified, and which build has each fix.
- **Run the mechanical checks**: no conflict markers, a clean `git diff --check`, the checkpoint greps, the Windows syntax check and the unit tests.
- **Change a failing test's expectation** only after two independent traces find no defect.

## 7. Debugging

Start from upstream Lichii's `AGENTS.md`. Its "Debugging tools" table maps each symptom to a switch and its output, and its "Live debug server" and "RenderDoc" sections set the rules for touching a running game. Point to them rather than copying them, since they change with upstream. The tools used most here:

- **Host crash** (a `Host crash:` line and `eden_crash.dmp`): symbolize the offsets with the build's symbols artifact. r62 and r63 crashed at every quit. The stack (`VmaBlockVector::Free` from the image recycler's `Clear` in `Device::~Device`) showed images being freed after their memory pool was gone.
- **No crash line and no dump**: a hang, a kill or a fail-fast exit skips the crash handler. Use a LocalDumps minidump if one is set up, and run the pipeline build trace (`EDEN_NX2_PIPELINE_BUILD_TRACE`). The last `BEGIN` without an `END` names what the driver was building.
- **Stutter trace** (`EDEN_NX2_STUTTER_TRACE`): it showed that the freezes at area loads were the GPU back thread's own work. Driver memory allocation alone took 29-50% of it, and staging buffers, texture uploads and FSR 4's first context took more. r64's smoothness items followed from this and cut the time spent in slow frames on the route by 29-44%.
- **GPU profile** (`EDEN_NX2_GPU_PROFILE`): shows where the back thread's time goes and where the front waits for it. Don't time with it.
- **Old logs are data.** One small kernel's time, already in every timing log, sorted 83 old logs into the GPU's fast and slow sittings. When nothing shows a cause, add a trace of your own, off unless set, and keep it. Here that was a GPU clock probe, and a heap trace that caught the game's memory maps blocking inside Windows.
- **Follow the live debug server's rules even if you never use it**: never start or stop the emulator, send one request at a time while the owner plays, restore every shader you reject, and never inject RenderDoc without the owner's approval.

## 8. Working rules

- **Never start or stop the owner's emulator.** Ask for runs with a test plan.
- **Keep a decision log** for the next session (here `HANDOFF.md`): how builds are made, what each build contains, every test with its numbers and verdict, what was dropped and why, and the next steps. Correct a wrong conclusion in place ("CORRECTION (date): ..."). Keep a changelog for the owner too: what changed and how to switch each item off.
- **Commit and push at each checkpoint**, so work survives a cut-off. Run `git remote -v` before every push, and push to a remote by name: twice, a session restart reset `origin`, and private work went to the wrong repository.
- **Keep private data out of the repository** and out of anything you publish:
  - no personal names, user or machine names, e-mail addresses or local paths;
  - no raw logs: they carry the emulated console's serial numbers and the network adapter's name, so quote only the lines you need;
  - no frame dumps, RenderDoc captures or shader caches;
  - no update checker aimed at another repository.
- **Credit every source.** Cherry-pick with `-x` and keep the original author. Name the PR or fork in the commit message and the changelog. Give each borrowed feature a "Credit and source" note like the one in upstream's `docs/nx2/ocarina-dynamic-fps.md`. Credit methods as well as code (section 4).
- **Wait for the owner before any outward action**: offering a fix upstream, publishing, or pushing a build the owner asked you to hold.

## Credits

- Testing methods: Frizti, the author of Fritchii, credited at the owner's request (section 4).
- Change-cycle rules and debugging tools: upstream Lichii's `AGENTS.md`.
- Examples: the tuned build's notes, with code from upstream Lichii, Fritchii and CheezerWheezer's CheezyEmu.
