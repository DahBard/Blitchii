# Lichii tuned for one laptop and Ocarina of Time

This is my personal build of upstream Lichii, tuned for one machine (a Core Ultra 7 255H laptop with an RX 9070 XT over OCuLink) and one game, The Legend of Zelda: Ocarina of Time (Switch 2). I've shared numbers from it before; this page shows what's behind them: the results and how they were measured, the settings anyone can copy, what's in the build and who made each part, what I left out, and how I test.

Much of the gain isn't mine. It comes from upstream Lichii and its pull request authors, from Frizti's Fritchii, from CheezerWheezer's CheezyEmu and from a few other forks, and every item below says where it came from. "rNN" is my build number, and the newest measurements are from r64.

## Results

**Setup.** Core Ultra 7 255H (6 P-cores, 8 E-cores, 2 low-power E-cores), 32 GB of RAM, Windows 11. RX 9070 XT (16 GB) over OCuLink (PCIe 4.0 x4), AMD Vulkan driver 2.0.416. One 3840x2160 monitor at 160 Hz with FreeSync, plugged into the 9070 XT.

**Test conditions.** Docked at Resolution 1X, so the game renders 2560x1440, and AMD FSR 4.1.2 upscales it to 3840x2160 (preset Native, no camera jitter). Frame generation off, HDR off, Exclusive fullscreen, VSync Immediate, Lichii's Switch 2 frame rate limit at 120. Force maximum clocks on. Windows power mode Best performance, plugged in. The spot: standing in front of the Market fountain, the most demanding place I test (about 2,750 draws a frame).

**Market fountain, r64, 2026-11-14.** Two runs of my baseline launcher, which logs frame times, CPU and GPU clocks and the game's memory maps:

| Window | Draws a frame | fps | p99 frame | 1% low |
|---|---|---|---|---|
| Run 1, last 100 s | 2,685 (a slightly lighter view) | 57.1 | 24.1 ms | 34.6 fps |
| Run 2, last 100 s | 2,745 | 54.7 | 25.1 ms | 34.8 fps |
| Both runs, the same 95 s after arrival | 2,743 and 2,750 | 55.7 and 54.5 | 25.0 ms (mean) | 34.2 fps (mean) |

- With frame times as the only logging, the fountain read 56.1 fps: the baseline's traces cost about 1 fps.
- In that run the 0.1% low is 22.2 fps. It comes from a hitch every ~10 s that adds about 26 ms to one frame; every fountain run has it, logged or not. Without it the 0.1% low would be 34.8 fps. I don't know its cause yet.
- What holds the frame is the GPU back thread. Lichii splits its GPU work over two threads: the front reads the game's command lists, and the back turns them into Vulkan commands. The back does about 14.2 ms of work a frame here, while the GPU is busy 13.1-13.6 ms. A millisecond of GPU time saved buys only about 0.1 ms of frame at this spot.

**Steady play, r62, 2026-11-14.** Two runs of the same route at a 60 fps lock, before I turned on Force maximum clocks. These are 10 s windows without loads, grouped by how heavy the scene is:

| Scene | Draws a frame | fps | 1% low | GPU back thread |
|---|---|---|---|---|
| Light | under 1,500 | 60.0 (cap) | 36.7 fps | 7.7 ms a frame |
| Medium | 1,500-2,100 | 59.9 | 35.9 fps | 11.0 ms |
| Heavy | 2,100-2,700 | 57.3 | 27.1 fps | 13.3 ms |
| Heaviest | 2,700 and up | 50.9 | 22.4 fps | 15.6 ms |

- My goal is a steady 60 fps with 1% lows above 30, at native 1440p upscaled to 4K. Light and medium play are there; heavy and heaviest scenes aren't yet.
- Area loads (r64, one route through five loads at a 120 fps lock): the load items r65 made default, tested together with #1571 (which stays off), cut the hitch time (what frames over 33.4 ms take beyond 16.7 ms) by 29%, or 44% leaving out one slow spot every run passes through. The third and fifth loads improved by 73% and 77%. Loads still freeze for part of a second.

## What you can copy today, without any code

### Windows power

1. **Check the power limit while you play** (HWiNFO's PL1). With the wrong power cord my laptop held Lichii at 35 W (PL1 read 35 W during play, against 82-87 W in Cinebench), and every number I took before 2026-11-12 was very likely at 35 W. On the right cord the P-cores' effective clocks went from ~2.9-3.0 to ~3.3-3.4 GHz.
2. **Power mode: Best performance while plugged in** (Settings > System > Power & battery > Power mode). In "Best power efficiency" my E-cores ran at 1.1-1.2 GHz and the emulated CPU cores spent 25-30% of their time on them. Best performance against that sitting: 16% shorter frames in heavy scenes, though the GPU state also differed between the two sittings.
3. **Optional: energy performance preference 0 and aggressive boost.** A little better, not significantly:

```bat
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFEPP 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFEPP1 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFBOOSTMODE 2
powercfg /setactive SCHEME_CURRENT
```

### Force maximum clocks

Configure > Graphics > Advanced > **Force maximum clocks (Vulkan only)**, a setting upstream Lichii already has. While the emulator is sending GPU work, it keeps a small compute job running on a Vulkan device of its own, so the driver doesn't lower the GPU clock.

- **Why it matters**: across 83 of my logs the GPU ran at different speeds from one sitting to the next, every GPU program 1.2-1.7x slower in the slow sittings, with the same settings, driver, power mode and CPU clocks. It was the driver's on-demand clock under Ocarina's bursty load. GPU-Z logging, my Adrenalin tune and an Adrenalin game profile weren't the cause.
- **Effect** (r64, at the fountain): GPU-Z shows a flat 3196 MHz instead of a clock wandering between 2.0 and 2.85 GHz, and the GPU is busy 13.0-13.6 ms a frame instead of 14.6-14.7 ms in the slower states. In frame rate it's worth about 1-2 fps here, because the back thread holds the frame: 54.2-54.7 fps without it and 54.5-57.1 with it, the 57.1 in a slightly lighter view. It isn't faster than the driver's own fast state (one run that happened to be in it read 55.9 fps). It removes the slow states, which also keeps them out of A/B tests.
- **Cost**: about 60 W more board power (153 W against ~90 W), with the hot spot at 64 °C.
- Help > Apply Recommended Settings turns it off, in upstream's table and in mine, so turn it back on afterwards.
- AMD's Radeon Developer Panel was no alternative here: it offered only Normal and a Stable 1820 MHz clock on this card, and Lichii wouldn't boot a game while it was connected.

### Emulator settings

What I run. Upstream's Help > Apply Recommended Settings sets several of these, and I note where my build's defaults differ from upstream's recommendation. I've only tested them in my build.

- Docked, Resolution 1X (docked, Ocarina renders 2560x1440).
- GPU accuracy Low; asynchronous shaders and asynchronous presentation on; GPU video decoding; GPU clocks Boost (Ocarina sizes its dynamic resolution from it).
- Switch 2 draws with unbuilt pipelines: Bounded (a draw waits up to 8 ms for its pipeline, then is skipped).
- Ocarina of Time anti-aliasing: No sharpening, which skips the game's AA network.
- VRAM usage mode: Aggressive. Upstream recommends Conservative, which caps a discrete card at about 7 GiB at Resolution 1X.
- Precache shaders before game start: Always. Upstream recommends Off. In upstream it builds the game's compute shaders on the loading screen; my build also runs its graphics shader preparation from it.
- Build all cached shaders before game start: on (about 0.3 MB of RAM per shader, up to a quarter of the RAM). My package forces it on.
- Enable Reactive Flushing: off. Upstream recommends on.
- Pin GPU threads to their own cores: off. Upstream recommends on; my build places its threads for the hybrid 255H itself.
- Force maximum clocks on; Exclusive fullscreen; VSync Immediate; Frame rate limit (Switch 2) 120; HDR off; frame generation off.
- AMD FSR 4 (my build's, from CheezyEmu): on, preset Native, docked output 4K.

### Display and drivers

- One 3840x2160 monitor at 160 Hz with FreeSync. FSR 4's 4K output is 1:1, so the scaling filter doesn't matter.
- AMD's driver has no VK_KHR_present_wait, so Fritchii's host frame pacer can't run on it. I use VSync Immediate with a frame rate limit and leave the frames below the refresh rate to FreeSync.
- HDR off: with HDR output on, my build shows the game's 1440p frame stretched to 4K while the GPU still pays for FSR's 4K image. It's a bug I haven't fixed.
- Adrenalin: global features off; my tune is -60 mV, power +10%, VRAM 2614 MHz with fast timing; hardware-accelerated GPU scheduling on. An Adrenalin game profile changed nothing (Adrenalin doesn't detect Lichii as a game).

### What's specific to this hardware

- The RX 9070 XT runs over OCuLink (PCIe 4.0 x4), and the laptop's BIOS forces Resizable BAR off, so only 256 MiB of VRAM is visible to the CPU. In my traces the x4 link hasn't been the limit for this game (an early one measured ~95 MiB/s of staging traffic).
- FSR 4 runs where AMD's runtime offers it (RX 9000 cards; RX 6000 and 7000 with a recent driver); elsewhere the build uses AMD's FSR 3.1.
- The thread placement is written for the 255H's core layout, and the compiler flags target Arrow Lake. The build still runs on any CPU with AVX2.
- 32 GB of RAM: the 4 GB GPU memory reserve for area loads only turns on with 24 GiB or more.
- AMD's driver shader cache has started cold with every new build I've installed, so each build's first launch is slow (2.5-5 minutes of boot builds instead of about 15 s) and runs the shader preparation again.

## What's in the build

The base is upstream Lichii at nx2 `58e418db65` minus the nine commits under [Left out on purpose](#left-out-on-purpose). Below is what differs from that base, grouped by where it came from. "Theirs" marks the author's own measurement on their own hardware, not reproduced here.

The biggest single steps so far, each measured in its own sitting (they don't add up):

- Answering Ocarina's occlusion queries at once (mine, r15): gameplay median 14.5 → 27.5 fps.
- #805's decoupled split GPU threads and #717's CPU work (open upstream PRs then, with an upstream merge in the same build, r28): ~29 → 37.5 fps.
- The right power cord (r55, Best performance in both runs): heavy scenes 43.4 → 50.8 fps, though the GPU was also in a faster state in the second sitting.
- From r55 to r62 (three upstream merges and one small CheezyEmu change): GPU back thread 14.0 → 11.3 ms a frame at the same CPU clocks, heaviest scenes 45.1 → 50.9 fps.
- Force maximum clocks (upstream's setting, r64): GPU busy 14.6-14.7 → 13.0-13.6 ms a frame at the fountain.

### From upstream Lichii, ahead of upstream or kept over its version

- **Decoupled split GPU threads, #805 (jpkto)**, kept over upstream's earlier port of the same PR (#949/#1056). The front GPU thread decides semaphore waits from its own view of releases, macros that read memory run on the back thread, and command lists the GPU wrote wait only for the words GPU work wrote. Here (r28 against r27) the front's waits for the back on GPU-written lists fell from 282 to 50 ms a second, and on macro reads from 217 to about 0.
- **Guest CPU work from #717 (Stephen Jayakar's commit)**: emulated kernel locks spin briefly before sleeping, guest accesses to GPU-cached memory take faster paths, and Ocarina gets host patches (zstd decompression on the host, 8-byte string compares, a resource poll backoff, wider core masks). With only the patches off, emulated cores 4 and 5 went back to 23.8 and 21.8 ms a frame (at most 15.1 and 20.0 with them). Its code generation is upstream's own now (#1531).
- **AMD submission budget, #573 (sanchodecuba11-del)**: on AMD's driver GPU work is submitted every 512 draws instead of 128, a Kokiri compute shader that stalled AMD's compiler is built unoptimized, and large unswizzles run on helper threads. Theirs: 35 → 50 fps at the Deku Tree on an RX 9070 XT.
- **Repeat draws survive more state writes, #974 (mstampfli) and #1216 (DjouWish).** #1216 measured neutral here (r52) and stays on: the verify runs found nothing wrong in its code.
- **Fewer waits between the split threads: #980 and #933 (mstampfli), #911 (zortos293), #1251 (sheepbun).** #980 alone removes about 2,000 front waits a second.
- **Image recycler, pooled image memory and a shared protection lock: #836 and #810 (Burak), #922 (DjouWish).** Theirs (#922): +6% fps on an RX 9070 XT.
- **Texture collector fixes: #976 (Devin Alsup), #1306 (Adam Uremek), #376 (Guilherme Moraes).** #1306, with #1311 (now upstream's), fixed the RX 9070 XT's out-of-memory driver crash when breaking pots.
- **Shader and pipeline build fixes: #970 (El-Sashok), #998 and #999 (aldoeliacim), #300 (Robin Irfan), #321 (Joey Magnus)**; a worker-task fix, #939 (marcusbooker77); and johnpi's replay of rejected split-front macros on the interpreter.
- **Small back-thread speedups**: four commits from #1161 (Julius Bairaktaris) and #1274's instance folding, in builds (r52-r54) that saved 0.15-0.45 ms a frame of back-thread time in all; #954 and #955 (Robert Veloso), not measured.
- **Off by default, still to test**: #1571 (jpkto: overwritten render targets retired) and upstream's #1406/#1407 (Reiixan: AMD's pipeline binary store, which upstream turns on for AMD).
- Many more upstream PRs were in this build before upstream merged them. They're upstream's code now; their authors are under [Credits](#credits).

### From Fritchii (Frizti)

Fritchii is Frizti's Lichii fork built around Ocarina (Frizti/Lichii, branch `oot-gather-fix`). Most of these come from v4.1 and v5.2; when I ported v4.1, people considered it the best-performing Ocarina build.

- **Shader preparation from Fritchii's Ocarina recipe**, on upstream #895's recipe format. Before play, every pipeline the game is predicted to draw (~120,000) is built once, so AMD's driver has them compiled before they're first drawn. Theirs: Ocarina's worst first-visit walking freezes went from ~700-850 ms to 40-70 ms. Here, after the preparation, no slow frame in my r62 and r63 traces had a pipeline built on the GPU thread.
- **The uniform bank check compares only pages that may have changed (EXP9)**, on top of Celmax's per-page check from #345. Here: 164 → 81 ms a second of the back thread (r23 → r27), and verify runs missed 0 changes. Theirs (Hyrule Field): idle 54.5 → 57.7 fps, walking 56.6 → 58.9.
- **Cheaper texture uploads on the GPU thread (EXP8).** Theirs: Hyrule Field 1% lows 31.1/29.5 → 35.2/34.9 fps.
- **Pipeline keys by the vertex attributes the shaders read** (Frizti's port of upstream #408 by ZeroOneTV), so the same shaders stop compiling again for leftover attribute types.
- **Shader translation on two worker threads, and smarter skipping of unbuilt draws** (Fritchii v5.2). Theirs, from an empty cache: GPU-thread pipeline time 7.5 → 4.4 s.
- **Guest memory without the caches' locks, committed as the game uses it** (v5.2; the lock-free reads are upstream's own now, #1479). Theirs, Castle Town: standing 43.0 → 45.3 fps.
- **Render targets relocated instead of rebuilt, with a render-frame clock and a cold sweep** (v5.2). Theirs, Castle Town live toggles: 46.3 → 49.3 fps. Fritchii's image pool switch drives #836's image recycler here instead.
- **Robustness fixes** (v5.2): every ASTC decode and unswizzle store is bounds-checked (AMD corruption and device loss), guest writes wait for a GPU download landing under them, and a staging buffer that can't be made frees the idle ones and tries again instead of aborting.
- **Upstream #820 (noperspective varyings) reverted, as in Fritchii**, where it caused white fog at the Hyrule Field drawbridge.
- **Cheaper zero-page passes** (the passes that take all-zero guest memory pages out of RAM): an early exit per page made each pass about 20% shorter (r50). Upstream later took the same change in #1580.
- **Host frame pacer**, opt-in. It can't run on AMD's driver, which lacks VK_KHR_present_wait.
- Frizti also built Dark's Dynamic FPS mod in as a built-in (it's in upstream now).

### From CheezyEmu (CheezerWheezer)

CheezyEmu is CheezerWheezer's Lichii fork for AMD GPUs. I replaced the build's FSR 4 and its camera jitter with CheezyEmu's FSR work: the jitter made the screen borders shimmer brightly, most visibly on water, and CheezyEmu specializes in AMD compatibility and is thoroughly vetted.

- **AMD FSR 4 for Ocarina through AMD's own runtime**: 1440p rendered and 4K out when docked, no camera jitter, the HUD masked, quality presets, and AMD's FSR 3.1 where FSR 4 isn't available or, for the rest of the session, when it draws black. Cost at the fountain: about 3.2 ms of GPU a frame (r62: 15.2 against 12.0 ms without it), which costs little frame rate yet because the back thread holds the frame (profiled runs: 48.8 fps without FSR, 46.8 and 48.1 with it).
- **AMD FSR frame generation**, opt-in: AMD's swapchain and pacing double the frames on screen, with the HUD masked. It adds at most 1.0-1.8 ms of GPU work to each real frame. I don't use it in play.
- **The FSR mesh list kept between frames** (r58): Ocarina's FSR stops rebuilding its list of moving characters every frame. By estimate that's a few tenths of a millisecond of back-thread time; I haven't measured it on its own.

### From other forks

- **tomk134's lichii-custom**: textures in one contiguous piece of guest memory are untiled in place instead of being copied out first. My first port of it read past a non-contiguous run and crashed two of my builds at a scene load; that's fixed.
- **notquiteog's Elysium**: GPU shaders' signed adds report overflow correctly at the negative boundary, and a reused scratch buffer serves the untiles that still copy.
- **medicinauvsecretaria-lang's "Lichii OoT AMD"**: the driver's shader cache is saved after 32 new pipelines (at most every 10 s) instead of 128 and 30 s, so a driver crash loses less. It only matters on drivers without a cache of their own, so not on AMD's Windows driver.
- Earlier, now upstream's code or replaced by it: Shakoquo's Sharkiix (an AMD query-copy wait, about 5 ms a frame with real occlusion queries, and a depth-compare sampler fix for a black forest fog), the same query change in chuppatricky's AMD test build, and Keeflo541's exit-hang fix from Kitsu.

### My own changes

These were written by AI coding agents at my direction, and tested by me on the laptop.

- **Ocarina's occlusion queries answered "visible" at once.** The game asks about 1,400 "is this visible?" questions a frame, and on this Radeon answering them with GPU queries cost most of the frame. r15 against r13/r14: gameplay median 14.5 → 27.5 fps, GPU busy 47.6 → 12.5 ms a frame. It's a shortcut: the game draws what it would have culled by occlusion. I haven't seen flares or objects through walls. Ocarina only.
- **Smoother area loads** (default since r65): FSR 4 is created at the title screen instead of in the first area load (about 200 ms off that freeze), freed GPU memory blocks are kept and up to 4 GB is made at boot (with 24 GiB of RAM or more), and texture uploads go through a 256 MiB staging ring. On my route the driver's allocation time during loads fell from about 1.8 s to about 1.0 s per run; the hitch results are in [Results](#results).
- **FSR 4's 4K output in 10 bits** in SDR: the FSR stages went from 2.93 to 2.67 ms of GPU a frame.
- **A shader preparation that finishes, resumes and runs once per build.** My first ports of Fritchii's preparation crashed near the end or used over twice the usual RAM, problems of my integration rather than of Fritchii's code. Now it builds the AA network's matrix-math (HMMA) programs in their own jobs, bounds the jobs in flight, saves its progress every 30 s and resumes, and names the build in its stamp, since each new build starts with a cold AMD driver cache. The first full run prepared 120,085 of 120,124 pipelines.
- **CPU placement for the 255H**: GPU threads on P-cores; emulated CPU cores and background pipeline builds on P- and E-cores; core timing on an E-core; nothing on the low-power E-cores. The split GPU threads wait for each other with UMWAIT instead of spinning, which leaves more of a power-limited CPU's budget to the busy cores. Not measured as a whole. The alternatives I measured were worse: everything on P-cores ran 22.5 against 25.2 fps median, and pinning only the GPU threads made frames 27-34% longer.
- **Compiler and PGO**: MinGW clang for x86-64-v3, tuned for Arrow Lake, with PGO and ThinLTO. My recorded Ocarina PGO profile gave no measurable gain over Lichii's own, and it goes stale as the code changes (worth 0-0.4 ms of back-thread time in one comparison).
- **Dynamic FPS takes each frame's own time** (Dark's mod, built in by Frizti). With the old 8-frame average the game ran up to 2-3x real speed for about half a second after slow frames. Chosen by feel.
- **Ocarina's GPU readbacks preflushed by default** (Ghostwolf13's opt-in #259: CPU reads of GPU-written memory went from 73-108 to 10 a second in my test), **with marks that expire after 120 frames** (mine: without that, downloads grew to 1.4-1.6 GiB/s within two minutes and the frame rate fell to 15-20 fps).
- **The game's clock read inside the JIT**, an idea from 999sian's #161. No frame-rate change measured.
- **The bank check skips untouched pages in bulk**: 0.21 ms a frame off the back thread.
- **Fixes**: no crash at quit (r62 and r63 crashed at every quit); a failed pipeline keeps no stages when it's evicted; frame generation on/off is one global setting (Ocarina's old per-game value turned it back on at every launch); AMD frame generation also runs with HDR10 output.
- **Diagnostics**: frame times with 1% and 0.1% lows, a CPU clock and Windows power-mode trace, a GPU shader-clock probe (it shows the driver's clock state in the baseline and trace logs) and a trace of the game's memory maps.
- **Off by default, under test**: native float atomics for Ocarina's ambient-light compute shader, and a lazier transform feedback barrier.

## Left out on purpose

Most of these cost frame time on this machine, replace a shortcut that works better here, or will be rewritten when Fritchii's GPU work comes in; the others have their reason next to them.

- **Nine commits of the latest upstream merge**, reverted on the upstream side: #1655 and #1702, the GPU trace recorder (a call into the video core on every trapped guest store); #1723, per-source-line GPU timing (a check per recorded command); #1137, Ocarina 1.0's output pass check (a check before every native dispatch on the back thread, with no gain on the game version I play); #1736 (it replaces #805's command-list shortcut with page copies and flushes); #1296 (to keep font atlas draws, it makes native draws without depth wait out whole pipeline builds); #1641, draw-state reuse for the unsplit GPU thread (this build runs split, and Fritchii's pipeline work covers that area); #1744, a fix for upstream's FSR jitter (there's no jitter here); #1731, the texture collector's filtered walk (tens to ~150 µs of back-thread time a frame at the fountain).
- **#1486** (an earlier merge): it would have replaced #805's working path and made the front thread wait for the back at each compute dispatch whose size the GPU wrote into a command list. Upstream reverted it too, in #1651 (white flashing in Ocarina).
- **Upstream's Ocarina FSR with camera jitter** (#1060-#1062): replaced by CheezyEmu's FSR, because of the shimmering screen borders.
- **Upstream's GPU-thread pinning by default** (#769): my thread placement instead.
- **AMD's pipeline binary store on by default** (#1406/#1407): off until I've tested it on RDNA 4.
- **#1314** (RsMan-Dev), the Castle Town guard house frame rate: not ported, because Fritchii's next release has its own fix.
- **Graphics pipeline libraries** and the graphics warm-up that needs them (#579/#741): fast-linked libraries draw white fringes on AMD's Windows driver (AMD-Gfx-Drivers#126) and crashed it; Fritchii turns them off on AMD too.
- **Upload reordering from #805**: about 0.1 ms of GPU time, bought with as much back-thread time.
- **A lower internal resolution or output**: ruled out. Native 1440p to 4K stays.
- **Tried and dropped**: native AMD fragment shuffles (a black screen), AMD float16 (#378: a wrong colour, no clear gain), all threads on P-cores, GPU-thread pinning, the FSR camera jitter, and per-draw trims that only moved time from one part of the back thread to another.

## How I test

Credit to Frizti, the author of Fritchii, for the testing methods I use. Fritchii's commits record the Ocarina measurements behind its changes: a fixed spot in Castle Town and a route, A/B toggles in balanced pairs within one session against a control ("each won all 4 pairs"), pairs of fresh launches, 1% and 0.1% lows, verify modes that check a shortcut against the slow way and must report 0 missed, and new switches merged "off until measured". My build reads Fritchii's numbered experiment switches (`EDEN_NX2_EXP<n>`) as they are.

How that works on my laptop:

1. **One build per round, candidates behind switches.** Each A/B launcher (a .bat file) adds one change to a baseline launcher with the same logging, and the log names every switch in effect at boot, so a switch that didn't take effect shows up. Upstream Lichii's AGENTS.md asks for the same: a round's candidates in one build behind switches, a proven fix shipped unconditionally, a disproven one dropped.
2. **Two fixed tests.** Frame rate: standing at the Market fountain, comparing the last 100 s of each run, or windows matched from arrival when runs differ in length. Smoothness: one save and one route through five area loads (A-B-C-B-A), counting the frames over 33.4 ms and their time over 16.7 ms, load by load.
3. **Arms alternate in one sitting** (baseline, change, change, baseline), with 60 s to settle before the 100 s window. Sittings differ more than runs: the same build once read 10.2 and 13.1 ms median GPU busy in two sittings, while two runs in one sitting differ by about 0.3 ms and 1.3 fps.
4. **Frame rates come from unprofiled runs.** Upstream's GPU profile costs about 15% at the fountain, so I use it to find where the time goes, and time builds with a baseline that logs only frame times and a few cheap traces (they cost about 1 fps).
5. **The machine's state is in the baseline and trace logs**: Windows power mode, CPU clocks and package power, and the GPU's shader clock, with GPU-Z logging beside (and HWiNFO when I check the power limit). Force maximum clocks stays on for tests.
6. **Gameplay only, matched by content.** Title screen, menus, loads and runs with a cold shader cache don't count, and windows are compared by draws a frame, not by the threads' own times.
7. **Enough runs.** Per-run noise is about 1.5-2% of frame time: a 4% effect needs about four runs per arm interleaved in one sitting, a 2% effect 10-17.
8. **Verify runs.** Verify launchers check each shortcut against the slow way while I play, and their mismatch counts must stay 0 (in one run, 0 of 112,015 command lists differed).
9. **The rule comes first.** Pass and fail are written down before the runs. A proven change becomes the default with an off switch, until a later cleanup makes it permanent; a disproven one is removed, and an unclear one is tested again on its own. Numbers from other forks stay their authors' claims until I reproduce them.

Where mine differs: Fritchii toggles its switches live in one session, and I relaunch for each arm. The unprofiled baseline, the content matching, the noise estimate and the machine-state logging came out of this laptop's problems: the power limit and the driver's GPU clock states moved results more than many of the changes I was testing. The traces are mostly upstream's (the GPU profile, the frame trace and the stutter trace) plus my frame-time, clock, memory-map and GPU-clock logging. A summary script reads each log, and for the bigger decisions a second analysis recomputes the numbers with its own code.

## How it's built

- **Source**: upstream Lichii's tree at nx2 `58e418db65` with the nine commits under [Left out on purpose](#left-out-on-purpose) reverted, plus one patch that holds everything else on this page (a little over 350 files). My rule for merges: where my code and upstream's do the same thing, upstream's wins, unless its change costs frame time here, replaces a shortcut of mine that works, or will be rewritten when Fritchii's GPU work comes in.
- **CI**: GitHub Actions on Windows with MSYS2 UCRT64 clang (MinGW), lld, Ninja and ccache. The flags are `-march=x86-64-v3 -mtune=arrowlake -ffp-contract=off`: it runs on any CPU with AVX2 and adds no fused multiply-adds the source doesn't ask for, so float results match official builds. PGO uses Lichii's own profile merged with one I recorded playing Ocarina, plus ThinLTO.
- **Tests**: a second workflow builds upstream's unit tests with the same compiler and runs them on every patch change. r65: 1,521 test cases, 1,518 passed, 3 skipped.
- **AMD's runtime**: CI downloads the signed FSR and frame generation DLLs of AMD's FidelityFX SDK v2.4.0, each pinned by SHA-256, and puts them next to lichii.exe with AMD's license, which allows redistributing them in binary form. The DLLs aren't in the repository; only the SDK's API headers are.
- **AI coding agents** wrote my changes, ports and merges, following the method in [AGENTS.md](AGENTS.md) next to this file. Changes go on their own branches and are reviewed by other agents who try to refute them, existing upstream fixes are ported and cited before anything new is written, and merges are planned and resolved area by area. The agents can't run the game, so every build's first real run is mine, and I send the logs back.

## Caveats

- **One machine, one game.** Everything is tuned for this laptop and Ocarina of Time. The occlusion answers, the FSR path, the built-in patches and the recipe do nothing for other games, and other CPUs and GPUs will see other numbers.
- **Old numbers don't compare with new ones.** Before 2026-11-12 the laptop very likely ran at 35 W, until 2026-11-14 tests used a 60 fps lock, r48-r55 ran with frame generation on (and until r63 it came back on at every launch), and Force maximum clocks is on from r64. I compare builds only within one sitting.
- **Some items are experiments, off by default**: #1571, AMD's pipeline binary store, native float atomics and the lazy transform feedback barrier. Fritchii's host pacer and AMD frame generation are opt-in.
- **The first launch of every new build is long**: 2.5-5 minutes of boot builds, then the shader preparation. Fritchii's figure for the preparation is about 15 minutes on an 8-core desktop. The one full run I timed on the laptop took 2,690.9 s, about 45 minutes, back when it was very likely held at 35 W.
- **Known open problems**:
  - Every r65 launch that built its cached pipelines at boot (three so far) disappeared about 8 s in, with no crash line or dump; a launch that skips that build plays. r66, not tested yet, targets the two likeliest causes (a thread running out of stack in AMD's shader compiler, or AMD's compiler failing on the shader form the latest merge brought) and makes the next disappearance report itself.
  - Heavy and heaviest scenes don't hold 60 fps, and their 1% lows (27.1 and 22.4 fps in r62) are under my 30 fps target.
  - The hitch every ~10 s that adds 16-26 ms to one frame at the fountain.
  - Area loads still freeze for part of a second. Part of it is the game's memory maps and unmaps blocking inside Windows (MapViewOfFile3 and UnmapViewOfFile2), and I know of no fix for that.
  - The Castle Town guard house ("pot room") frame rate has no fix here yet; I'm waiting for Fritchii's.
  - HDR output shows the 1440p frame (see [Display and drivers](#display-and-drivers)).
  - Force maximum clocks costs about 60 W, and Apply Recommended Settings turns it off.
  - This MinGW build has no NX converter menu entries, because that library links only with MSVC. Loading .nx games works.
  - One verify run found at least 81 mismatches in 3.23 million reused constant-buffer bindings (upstream #949's "uniform keeps"); the cause isn't proven yet.
- **Next**: Fritchii's multithreaded GPU back thread, when its next release is out. Until then the back thread is the limit.

## Credits

- **Frizti**, Fritchii (Frizti/Lichii): the testing methods I use, the shader preparation and its Ocarina recipe, EXP8 and EXP9, async translation, the guest memory and render target work, the v5.2 robustness fixes, the #820 revert, the zero-page early exit, the host pacer and the Dynamic FPS built-in.
- **CheezerWheezer**, CheezyEmu: Ocarina's AMD FSR 4 path with its FSR 3.1 fallback, HUD masks and presets, AMD FSR frame generation and the kept FSR mesh list; also the float8 finding behind the black FSR 4 output that upstream fixed.
- **Zortos and everyone contributing to upstream Lichii** (zortos293/Lichii): the emulator this build is.
- **Eglantine** ([Djihads80/Eglantine](https://github.com/Djihads80/Eglantine)), which Lichii is a fork of, and **Eden**, which both build on. **Dark**, an Eglantine developer, wrote the Dynamic FPS routine.
- **AMD**: the [FidelityFX SDK](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) v2.4.0, whose runtime does FSR 4, FSR 3.1 and frame generation.
- **Upstream PR authors whose work is here ahead of upstream, instead of its version or behind a test switch**: jpkto (#805, #1571), Stephen Jayakar (#717), sanchodecuba11-del (#573), mstampfli (#974, #980, #933), DjouWish (#1216, #922), zortos293 (#911), sheepbun (#1251), Julius Bairaktaris (#1161), Burak (#836, #810), Robert Veloso (#954, #955), Devin Alsup (#976), Adam Uremek (#1306), Guilherme Moraes (#376), El-Sashok (#970), aldoeliacim (#998, #999), Robin Irfan (#300), Joey Magnus (#321), marcusbooker77 (#939), johnpi (the split-front macro replay), Celmax (#345), Ghostwolf13 (#259), 999sian (#161), ZeroOneTV (#408), Reiixan (#1406, #1407), and the authors of #895, #1274, #831 and #1548.
- **Upstream PRs this build carried before upstream merged them**: ProphetWild (#319, #700), CTGL (#299), Inc0rporate (#337), ogeidexe (#292), Felipe Lima (#329), monsterlag (#346), DJ Lynch (#415), Kajcsi (#288), Gavin Schiller (#699), 6c696e6b (#621), janvitos (#609-#611), Frysix (#645), SmileyTatsu (#296), nyakuoff (#695), Jordan (#463), persluffy-cl (#448), Austin (#1180), kafasam (#1008), Jaramie Morris (#1005), RomainROCH (#1311), Lucas Borges (#1161), and the authors of the nx2 commits I picked early: Paulius, Kaj, SinCuri0, Hazmad, vouion, Andre Saddler, Thibaut LÉAUX, Brett Tegart, William and Dr. Sergio Ribeiro Filho.
- **Other forks and projects**: nxnative, whose MIT-licensed shader_split.py the shader extraction follows when it fingerprints programs; tomk134 (lichii-custom), notquiteog (Elysium), medicinauvsecretaria-lang ("Lichii OoT AMD"), Shakoquo ([Sharkiix](https://github.com/Shakoquo/lichii-sharkiix-version)), chuppatricky (an AMD test build, and #149's split-thread deadlock fix), Keeflo541 (Kitsu), and PantACRO4life (the drain-free acquire polls in Frizti's v3, kept here as an off-by-default path).
