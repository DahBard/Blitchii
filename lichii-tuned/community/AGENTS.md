# Tuning a Lichii fork for one machine and one game

For AI coding agents on a personal Lichii fork that aims to make one game run as fast and as smoothly as possible on one owner's machine. Append this file to upstream Lichii's root `AGENTS.md`, or keep it as a separate file that the root one links to, and leave upstream's text intact: this file points to its debugging tools. Take upstream's rules and tools, not its maintainer's PC or build script. Upstream here means upstream Lichii: `nx2` and its pull requests.

The examples come from a Windows build tuned for The Legend of Zelda: Ocarina of Time (Switch 2) on a Core Ultra 7 255H laptop with a Radeon RX 9070 XT over OCuLink (`r65` is one of its builds, `ab-26` one of its A/B launchers). Replace the examples; keep the rules.

The loop: build, the owner plays and sends `eden_log.txt`, analyse, build again. This build's agents had no GPU, so none of their changes ran on one before the owner's first launch. If that is your case, say so in every test plan. For Switch 2 games Lichii splits its GPU thread in two: the front parses the game's command lists, and the back thread runs the draws and the renderer behind them. Here the back thread limits the frame rate.

## 1. Goal and the owner's constraints

- **Record the target first**: CPU core types, the GPU and its link, RAM, display, driver, OS, the game's version and the owner's settings. Here: 6 P-, 8 E- and 2 low-power E-cores without SMT; the GPU on PCIe 4.0 x4 with Resizable BAR off; 32 GB; one 4K 160 Hz FreeSync monitor; AMD driver 2.0.416; the game docked at 1X (2560x1440). These decide what helps: on this hybrid CPU, pinning the GPU threads to their own cores made frames 27-34% longer than the build's placement by core type.
- **Keep an owner section** in the file your agent reads first: one dated rule per line, newest last, in the owner's words. Read it before every task. A newer rule wins; note what it replaces instead of deleting the old one.
- **When a rule reads two ways**, write both readings down and ask. Here the rule that medium-intensity areas hold a steady 50 fps or more could mean every medium 10 s window at a median of 50+ (the build passed) or also 1% lows of 45+ (it failed); both results were recorded.
- **Recommend; the owner decides.** State your confidence, then record the decision with its date and a quote.

This build's owner section, abridged:

- 2026-11-07: a constant 60 fps, the game rendering 2560x1440 and FSR 4 upscaling to 4K (later: with 1% lows above 30).
- 2026-11-10: upstream Lichii comes first; where our code and upstream's do the same thing, upstream's wins.
- 2026-11-14: "the native 1440p upscaled to 4k, no compromises on that": no lower internal resolution, render scale or output.
- 2026-11-14: "do whatever guarantees the most performance. if the shortcut works, and I haven't reported any issue with it, then keep it in without the upstream change." (an exception to the 2026-11-10 rule)
- 2026-11-14: "Make sure to not entertain any optimizations for the gpu that would cost us performance on the back thread" (so no upload reordering: ~0.1 ms of GPU time for as much back-thread time).
- 2026-11-14: "We also will not be using framegen in gameplay ideally."

## 2. Source priority

- **Upstream first.** Merge it regularly; where your code and upstream's do the same thing, take upstream's.
- **Port before you write.** Run upstream's "Before writing a fix" checklist against upstream Lichii and its own base (Eglantine): `nx2`, open and closed PRs, unmerged branches, issues. Read the diffs (`git fetch upstream refs/pull/<n>/head`), port with `git cherry-pick -x` so the author stays, and cite the PR. Write a new fix only when nothing addresses the root cause the log shows.
- **Keep a working shortcut** that an upstream change would replace at a performance cost (the owner's 2026-11-14 shortcut rule above): revert the change on your base branch and record why. With upstream #1486, at every compute dispatch Ocarina feeds from a GPU-written command list, the front thread would lose track of the values that work releases and wait for the back thread instead, so the build kept its own path (from upstream PR #805, on since round 18 with no problem reported); upstream later reverted #1486 too.
- **Sort each upstream merge first**: take, needs a test (behind a switch), left out (with the reason), already in. For merge 5, reviewers sorted the 136 upstream commits since the last merge (CI, docs and other-platform commits skipped) and left 9 out, mostly for costing frame time, replacing a working shortcut or facing a rewrite by a planned port; the rest came in.
- **Forks specialised in your game or hardware** come next, each at a bar the owner sets. Fritchii, Frizti's Ocarina fork: only a large benefit on this setup or in Ocarina; from v6, "default to changing to fritchii's unless our way of doing it is specifically tuned to our Hardware specifications". CheezerWheezer's AMD-focused CheezyEmu: its FSR, frame generation and Radeon work, if they meet the build's standard. Its AMD FSR 4 upscaler and frame generation were ported from one pinned commit through an inventory, three reviews and fixes. When the owner prefers a fork's coming fix, wait for it: upstream #1314 (the Castle Town guard house frame rate) waited for Fritchii's.
- **Other forks** only for a very good reason or when the owner names one. Port pieces; don't migrate to their trees.
- **Vet everything not from upstream.** A fork's numbers are its author's claims, on its author's hardware, until you reproduce them.

## 3. The change cycle

Each round makes one build:

1. **Survey** upstream, your forks and the owner's newest logs. For a big question (GPU time, hitches), have agents investigate by area and synthesise, then let skeptics recheck each item; build only what survives.
2. **Port or write** each candidate on its own branch: one agent implements, another reviews (section 6).
3. **Put each candidate behind its own switch, off by default**, so the default build plays like the last ("with none set, r64 plays like r63"). Name each switch so its source shows: here `EDEN_NX2_TEST_<PR>` for an upstream pull request, a fork's own names for its ports (Fritchii's `EDEN_NX2_EXP<n>`) and `LICHII_*` for many of the build's own. Prove off is off: with r64's native float atomics off, its SPIR-V was byte-identical to r63's in a test harness.
4. **Ship one build** with every candidate and one launcher (an arm) each: the baseline launcher plus exactly one switch. Log a boot line naming each test switch and its state (here `TEST switches in effect:`), so a switch that didn't take effect shows.
5. **Write the decision rule before the runs**: "#1216 is dropped if ab-26 minus the baseline is <= -0.2 ms" (ab-26 turns #1216 off: earlier rounds shipped many candidates on, each with a launcher that turned it off).
6. **Give the owner a test plan**: launchers in order, where to play, what to send back, which log lines and counters to watch.
7. **Measure** (section 4) and **decide each item**. Proven: it becomes the default with an off value (r65: the FSR 4 context built at the title screen, 10-bit FSR output, a 4 GB memory reserve, a 256 MiB staging ring). Disproven: drop it and its code. Unclear: split it off and test it alone (two items of r64's GPU bundle became ab-43 and ab-44). Correct but neutral: it may stay, marked "no performance claim".
8. **Clean up** later: settled switches and their launchers go (r61 removed 35 launchers and kept 13).

Measure; don't extrapolate. Builds A and B were planned to save 1.5-3 ms of back-thread time a frame and saved 0.15-0.45 ms: gains on fast draws came back as dearer full draws, and work moved between profile zones instead of going away.

Experiment switches never go upstream. Offer a fix upstream only with the owner's approval, as unconditional behaviour; diagnostics stay off unless their variable is set.

## 4. Testing protocol

This build's owner credits these testing methods to Frizti, the author of Fritchii, whose commit messages record the measurements behind its changes:

- same-session A/B tests: switches toggled live against a control at a fixed spot;
- balanced pairs, counted pair by pair ("Castle Town toggle, 4 balanced cycles: 41.4 -> 44.5 fps, every pair positive", Fritchii commit 6c8b491012);
- standing runs, route runs and pairs of fresh launches;
- 1% and 0.1% lows;
- a flash watcher with a known noise floor;
- verify runs that must miss nothing;
- switches merged "off until measured".

Fritchii toggles switches live through the debug server. Where yours can't change at runtime, relaunch per arm and alternate, as this build did.

- **Launchers and logs**: one launcher per arm. Each launch starts a new `eden_log.txt` and keeps only the previous one, so the owner copies it after every run, named after its launcher.
- **Fixed spot**: benchmark at the game's heaviest spot (here the Market fountain, ~2,750 draws a frame). Settle 60 s, then compare the last 100 s, or windows matched by arrival at the same content.
- **Fixed settings**, checked in every log: here a 120 fps lock (above the target, so the cap hides no gain), frame generation and HDR off, exclusive fullscreen, docked 1X with FSR 4 to 4K, Force maximum clocks (Graphics > Advanced) on, Windows power mode Best performance, plugged in.
- **Unprofiled baseline** (here `perf-trace-noprofile`: frame times and cheap traces). The GPU profile costs ~15% at the fountain and even the cheap traces ~1 fps: profile to find where time goes, never to compare builds. Compare an arm only with its own build's baseline; a stale PGO profile alone moved the back thread 0-0.4 ms.
- **One variable at a time**: each A/B launcher is the baseline plus one switch. Split bundles that confound; plan a run for any confound you can't split (every r64 route run had about twice r63's slow-frame time after the last load, and three new items ran in all of them, Force maximum clocks among them; one run with it off tells them apart).
- **Alternate arms in one sitting** (one play session), in the order baseline, arm, arm, baseline, and again. One build measured 10.2 and 13.1 ms median GPU busy in two sittings, while two runs in one sitting differed by ~0.3 ms and 1.3 fps.
- **Repeat enough.** Noise was ~1.5-2% of frame time per run once drift and each launch's memory-mapping cost were removed (the game's memory map and unmap calls cost 164-296 us each in some launches, 51-91 us in others). A 4% effect needs ~4 runs per arm, a 2% effect 10-17. An arm helps only if every block agrees and its mean gain beats the baseline's spread.
- **Route runs for smoothness**: same save, same route through several area loads (here A-B-C-B-A), with a stutter-trace launcher as the baseline; its steady frames run 2-5% slower, so compare it only with itself. Count frames over 33.4 ms and their time over 16.7 ms, per load.
- **Metrics**: median fps; the 1% low (1000 / mean of the slowest 1% of frame times in ms) and the 0.1% low; the limiting thread's work per frame; GPU busy. Find the limiting thread first: here the GPU back thread, so a GPU millisecond saved bought only ~0.1 ms of frame at the fountain. Group windows by content, not by their own thread times (here draws a frame: <1500, 1500-2100, 2100-2700, >2700).
- **Gameplay only**: no title screen, menus, loads or a new build's first launch (cold driver cache, shader preparation). A title-screen video was once read as a steady 60 fps, and a decision built on it had to be revisited.
- **Log the machine's state** every run: power mode, CPU clocks and package power, GPU clock. Each skewed results here: a wrong power cord held the CPU at 35 W for at least a round; "best power efficiency" put the E-cores at 1.1 GHz and slowed the game; in some sittings the driver's on-demand GPU clock made every GPU program 1.2-1.7x slower, until Force maximum clocks held ~3.2 GHz for ~60 W more.
- **Verify launchers** run each shortcut against the slow path while the owner plays; their mismatch counts must stay 0. Never time with them.
- **Write each sitting down**: date, build, arms in order, numbers, parser, analyst, a verdict per item (default, drop, keep testing alone). Have a second analyst recompute every number with their own code.

## 5. Building

- **One patch over a named base.** The base is an upstream commit plus reverts of what you left out, on its own branch; the patch is `git diff --binary <base> <work-branch>`. After each change regenerate it, `git apply --check` it on a clean checkout, and confirm base plus patch equals your tree. Set `*.patch -text` in `.gitattributes`: a runner's `core.autocrlf` once broke the patch.
- **Moving the base**: merge upstream into the work branch; build each new base branch on the last. Plan a big merge by area: a trial merge, a coverage check that assigns every upstream commit and every file both sides changed to an area, a table of where upstream replaces your code and where yours stays, parallel resolvers with a reviewer each, and checkpoint greps with expected results. Turn on `rerere.enabled` (review every reused resolution) and `merge.directoryRenames` (it carried this fork across upstream's directory rename).
- **CI for the owner's platform**, here Windows with MSYS2 UCRT64 clang, lld, Ninja and ccache: `-march=x86-64-v3 -mtune=<owner's CPU> -ffp-contract=off` (no fused multiply-adds the source didn't ask for, so floats match official builds), Lichii's PGO profile plus ThinLTO (a profile recorded in the game added nothing measurable), and `ninja -k 0` so one run lists every error. Keep an unstripped symbols artifact; download redistributables in CI pinned by SHA-256 instead of committing them; name each build in the log and title bar; mark notes-only commits `[skip ci]`.
- **Unit tests**: upstream's `tests` on every patch push, in a separate workflow with the same toolchain, so build numbers don't move.
- **Syntax-check changed files for Windows before pushing**; a Linux compile skips `#ifdef _WIN32` code. Use `clang++ --target=x86_64-w64-mingw32 -fsyntax-only` with the CI's flags (here `-std=gnu++20 -Werror=all -Werror=extra -Werror=missing-declarations -Werror=shadow -Werror=unused`).
- **Record rules from build failures**, e.g. no inline `thread_local` of a non-trivial type in a header (one broke the ThinLTO link).

## 6. Review

- **Review every change adversarially before it ships**, by agents that didn't write it: one implementer, two reviewers and a fixer; a finder per item and two skeptics per finding; verifiers who try to refute each finding; a second agent per merge area. Build B's deep review caught a major defect its verify switch couldn't see (after a fast-path draw skipped a cache refresh, a later draw could read undefined vertex input).
- **Fix confirmed findings, then re-review the fixes** (r55's: four lenses, a refuter each). Record refuted and unverified findings as such, and which build has each fix.
- **Run the mechanical checks**: no conflict markers, clean `git diff --check`, checkpoint greps, the Windows syntax check, unit tests.
- **Change a failing test's expectation** only after two independent traces find no defect.

## 7. Debugging

Start from upstream Lichii's `AGENTS.md`: its "Debugging tools" table maps each symptom to a switch and its output, and its "Live debug server" and "RenderDoc and the live server" sections set the rules for a running game. Point to them, don't copy them; they change with upstream. Used most here:

- **Host crash** (`Host crash:` line, `eden_crash.dmp`): symbolize with the build's symbols artifact. r62 and r63 crashed at every quit; the stack (`VmaBlockVector::Free` from the image recycler's `Clear` in `Device::~Device`) showed images freed after their memory pool was gone.
- **Gone with no crash line and no dump**: something skipped the handler, such as a kill, a fail-fast exit, or a stack overflow that takes a handler on the crashed thread down with it. If it happens while pipelines build, run the pipeline build trace (`EDEN_NX2_PIPELINE_BUILD_TRACE`): a `BEGIN` without an `END` names what the driver was building. r65 vanished three times at the same point of its boot build; in the traced launch, the open builds included the first task/mesh pipeline AMD's driver had to compile cold. Then make the next failure name its cause: r66 (untested when this was written) removes both suspects (the one merged change to the SPIR-V AMD's driver gets, and 1 MiB thread stacks), adds a launcher that brings the first back, and reports stack overflows from a separate thread.
- **Stutter trace** (`EDEN_NX2_STUTTER_TRACE`): area-load freezes were the GPU back thread's own work (driver memory allocation alone 29-50% of its time in each, plus staging buffers, texture uploads and FSR 4's first context). r64's smoothness items followed and cut the time in slow frames on the route by 29-44%.
- **GPU profile** (`EDEN_NX2_GPU_PROFILE`): where the back thread's time goes and where the front waits; not for timing.
- **Old logs are data**: one small kernel's time in every timing log sorted 83 old logs into the GPU's fast and slow sittings. When nothing shows a cause, add your own trace, off unless set, and keep it (here a GPU clock probe, and a heap trace that caught the game's memory maps blocking inside Windows).
- **The live debug server's rules apply even when you don't use it**: never start or stop the emulator, one request at a time while the owner plays, restore every rejected shader, no RenderDoc injection without approval.

## 8. Working rules

- **Never start or stop the owner's emulator**; ask for runs with a test plan.
- **Keep a decision log** for the next session (here `HANDOFF.md`): how builds are made, what each contains, every test with its numbers and verdict, what was tried and dropped and why, next steps. Correct wrong conclusions in place ("CORRECTION (date): ..."). Keep a changelog for the owner: what changed and how to switch each item off.
- **Commit and push at each checkpoint** so work survives a cut-off. Run `git remote -v` before every push and push to a named remote: here a session restart twice reset `origin` to another repository, and the check caught it before a push.
- **Keep private data out** of the repository and anything published: no personal, user or machine names, e-mail addresses or local paths; no raw logs (they carry local paths with the user name, the emulated console's serial numbers and the network adapter's name; quote the lines you need); no frame dumps, RenderDoc captures or shader caches; no update checker aimed at another repository.
- **Credit every source**: cherry-pick with `-x` and keep the author; name the PR or fork in the commit message and changelog; give borrowed features a "Credit and source" note like upstream's `docs/nx2/ocarina-dynamic-fps.md`. Credit methods as well as code (section 4).
- **Outward actions wait for the owner**: offering a fix upstream, publishing, pushing a build the owner asked to hold.

## Credits

- Testing methods (section 4): Frizti, the author of Fritchii.
- The port-before-you-write checklist, one build per round with A/B switches, the toggle rules and the debugging tools: upstream Lichii's `AGENTS.md`.
- Examples: the tuned build's notes, with code from upstream Lichii, Fritchii and CheezerWheezer's CheezyEmu.
