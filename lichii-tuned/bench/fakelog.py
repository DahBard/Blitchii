"""Generated Lichii logs for the bench tests: the line formats analyze.py reads, with numbers chosen by
the test (frame times, GPU busy, back work, hitches, live flips and marks). No real log content."""
import random

SETTINGS = {'Renderer.force_max_clock': 'true', 'Renderer.frame_gen': 'false', 'Renderer.nx2_frame_rate_limit': '120',
            'Renderer.fullscreen_mode': 'Exclusive', 'Renderer.hdr_mode': 'Off', 'Core.use_speed_limit': 'true'}
WATCH = ['45384f519a8eacd3', '1e1964184a291f00', 'b712c7d7ddd57755', '75d4369f3b1f5868']


def line(t, cls, src, fn, msg, level='Info'):
    return (t, f'[{t:12.6f}] {cls} <{level}> {src}:1:{fn}: {msg}')


def mark(t, text, present=0):
    return line(t, 'Debug', 'core/debugger/nx2_debug_tools.cpp', 'HandleMark', f'Debug server mark: {text} (present {present})')


def flip(t, name, old, new, present=0, gone=False):
    tail = f'(client gone, present {present})' if gone else f'at present {present}'
    return line(t, 'Debug', 'core/debugger/nx2_debug_tools.cpp', 'HandleSwitch', f'Live switch {name}: {old} -> {new} {tail}')


def value(x, t):
    return x(t) if callable(x) else x


def fountain(seed=1, build='0123456789', switches='EDEN_NX2_TEST_X off', settings=None, arrival=70, stay=170.0,
             frame_ms=19.6, noise=0.8, gpu_busy=15.2, back_work=14.5, back_cpu=15.0, back_ghz=4.21, pkg_w=51.0,
             probe=1790, draws=2750, hitches=((5.027, 0.35, 26.0), (4.157, 1.1, 9.0)), xlate=(), crash=False,
             power_mode='best performance', events=(), program='0400486024DEC000', extra_lines=()):
    """One launch: boot, a title at ~400 fps, a load ending just before `arrival` (an integer second),
    then `stay` seconds of play. Numbers may be functions of the log time."""
    rng = random.Random(seed)
    out = [line(0.001, 'Frontend', 'qt_common/qt_common.cpp', 'Init', f'Lichii Version: Lichii Development Build | tuned-rXX-{build}-tuned')]
    s = dict(SETTINGS); s.update(settings or {})
    for i, (k, v) in enumerate(s.items()):
        out.append(line(3.7 + i * 1e-5, 'Config', 'common/settings.cpp', 'LogSettings', f'M- {k}: {v}'))
    out.append(line(5.0, 'HW.GPU', 'common/clock_trace.cpp', 'LogPowerMode', f'Windows power mode "{power_mode}" (user-configured "{power_mode}"), on AC power'))
    out.append(line(5.01, 'HW.GPU', 'video_core/video_core.cpp', 'LogTestSwitches', f'TEST switches in effect: EDEN_NX2_TEST_1216 on, {switches}'))
    out.append(line(5.02, 'Frontend', 'lichii/main_window.cpp', 'BootGame', f'Booting game: {program} | The Legend of Zelda: Ocarina of Time (64-bit) | '))
    ctx = 33.0
    out.append(line(ctx, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_fsr4_upscaler.cpp', 'Prepare', 'AMD FSR: output and context made in 190.0 ms (prepared early)'))
    grid = []
    for period, phase, size in hitches:
        k = 0
        while ctx + phase + k * period < arrival + stay:
            grid.append((ctx + phase + k * period, size)); k += 1
    grid.sort()
    end = arrival + stay
    frames = []; t = ctx + 0.5; gi = 0
    load = [900.0, 640.0, 320.0]
    load_start = arrival - 0.3 - sum(load) / 1000.0
    while t < end - 0.5:
        if load and t >= load_start:
            for ms in load:
                t += ms / 1000.0; frames.append((t, ms))
            load = []; continue
        play = t >= arrival - 0.3
        ms = max(1.0, value(frame_ms, t) + rng.gauss(0, noise)) if play else max(0.5, 2.5 + rng.gauss(0, 0.3))
        while gi < len(grid) and grid[gi][0] <= t:
            gi += 1
        if gi < len(grid) and grid[gi][0] <= t + ms / 1000.0:
            ms += grid[gi][1]; gi += 1
        ms = round(ms, 1); t += ms / 1000.0
        frames.append((t, ms))
    # Per-second lines at each second's last frame end, so the frame ends rebuild exactly.
    groups = {}
    for e, ms in frames:
        groups.setdefault(int(e), []).append((e, ms))
    prev = None; n_total = 0; ends = [e for e, _ in frames]
    for sec in sorted(groups):
        g = groups[sec]; tl = g[-1][0]; n = len(g); n_total += n
        play = tl >= arrival
        out.append(line(tl, 'Render.Vulkan', './video_core/frame_time_stats.h', 'Note', f'DKB frame_ms n={n} ' + ' '.join(f'{ms:.1f}' for _, ms in g)))
        busy = value(gpu_busy, tl) * (1 + rng.gauss(0, 0.004)) if play else 0.15
        out.append(line(tl + 2e-6, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_scheduler.cpp', 'TraceGpuSubmission',
                        f'DKB gpu_frame renderer_frame={n_total} last_submission_tick=1 gpu_frame_span_ms=19.000 gpu_submission_sum_ms={busy:.3f} '
                        f'gpu_busy_ms={busy:.3f} measured_submissions=40 dropped_submissions=0 valid_bits=64 period_ns=10 gpu_upload_sum_ms=0.050 draws_timed=0'))
        wall = 1000.0 * (tl - prev) if prev is not None else 1000.0
        bc = value(back_cpu, tl) if play else 2.0
        out.append(line(tl + 3e-6, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_rasterizer.cpp', 'TickFrame',
                        f'DKB cpu through_frame={n_total} frames={n} window_wall_ms={wall:.3f} gpu_command_cpu_ms={8.0 * n:.3f} '
                        f'gpu_back_cpu_ms={bc * n:.3f} gpu_back_cpu_ms_per_frame={bc:.3f} vulkan_worker_cpu_ms={4.0 * n:.3f}'))
        dpf = value(draws, tl) if play else 2
        out.append(line(tl + 4e-6, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_rasterizer.cpp', 'TickFrame',
                        f'DKB draws through_frame={n_total} frames={n} attempts={int(dpf * n)} skipped=0 (totals since previous line)'))
        prev = tl
    import bisect
    tc = 15.28
    while tc < end:
        k = bisect.bisect_right(ends, tc) - bisect.bisect_right(ends, tc - 10.0)
        bw = value(back_work, tc) * (1 + rng.gauss(0, 0.002)) if tc - 10 >= arrival else 2.0
        work = bw * k / 10.0
        out.append(line(tc, 'HW.GPU', 'common/clock_trace.cpp', 'LogWindow',
                        f'Clock trace per second (EDEN_NX2_CLOCK_TRACE, 10.0 s): back ghz={value(back_ghz, tc):.2f} cpu_ms={work + 30:.1f} poll_ms=30.0 '
                        f'work_ms={work:.1f} p=100% e=0% lpe=0% migrations=1.0 | front ghz=4.17 cpu_ms=460.0 poll_ms=50.0 work_ms=410.0 p=100% e=0% '
                        f'lpe=0% migrations=1.0 | power RAPL_Package0_PKG={value(pkg_w, tc):.1f}W | power_mode="{power_mode}" '
                        f'user_mode="{power_mode}" ac=on battery_saver=off battery=97% changed=no | trace overhead 4.00 ms/s'))
        p = value(probe, tc)
        out.append(line(tc + 0.2, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_gpu_clock_probe.cpp', 'Report',
                        f'GPU clock probe in 10 s: median {p} MHz ({p - 20}-{p + 5}), loop 45.6 us (10 samples), 47151 cycles'))
        q = sum(1 for x in xlate if tc + 0.5 - 10 < x <= tc + 0.5)
        out.append(line(tc + 0.5, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_pipeline_cache.cpp', 'ReportWorstFrame',
                        f'Worst frame in the last 10 s: 30.0 ms (0 of 500 frames over 33 ms); in it {q} pipelines built on the GPU thread in 0.0 ms'))
        out.append(line(tc + 0.5001, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_pipeline_cache.cpp', 'TickTranslations',
                        f'Async translate in the last 10 s: {q} queued (0.0 ms of GPU thread)'))
        tc += 10.0
    tw = arrival + 1.3
    while tw < end:
        out.append(line(tw, 'Render.Vulkan', 'video_core/renderer_vulkan/vk_scheduler.cpp', 'ReportWorkTimes',
                        'DKB gpu_watch renderer_frame=1 timed=100 ' + ' '.join(f'{h}={0.1 + 0.05 * i:.3f}ms/1x(max 0.1ms size 1x1x1)' for i, h in enumerate(WATCH))))
        tw += 2.5
    if crash:
        out.append(line(arrival + 20.0, 'Common', 'common/crash.cpp', 'Report', 'Host crash: exception 0xc0000005', 'Critical'))
    out.append(line(end, 'Frontend', 'lichii/main_window.cpp', 'OnShutdownBegin', 'Shutdown requested; waiting for emulation thread'))
    out.append(line(end + 4.0, 'Frontend', 'lichii/main_window.cpp', 'OnEmulationStopped', 'Emulation cleanup completed'))
    out.extend(events)
    out.extend(extra_lines)
    out.sort(key=lambda x: x[0])
    return '\n'.join(text for _, text in out) + '\n'


def live_schedule(start, items, order='ABBABAAB', settle=3.0, measure=25.2, margin=2.0, gone_in=None, flip_in=None):
    """Marks and flips for live units. items: [(name, switch, a, b, units)]. Returns the events, the
    segments (k, item, arm, start, end) and value_at(switch, t). gone_in / flip_in: a segment number
    in which the client is gone (its switch reverted) or the switch flips once more."""
    events = []; segs = []; changes = {}; t = start; k = 0; present = 100000
    for name, sw, a, b, units in items:
        cur = a; changes.setdefault(sw, [(0.0, a)])
        for u in range(units):
            for arm in order:
                val = b if arm == 'B' else a
                if val != cur:
                    events.append(flip(t, sw, cur, val, present)); changes[sw].append((t, val)); cur = val
                t_open = t + settle; t_close = t_open + margin + measure + margin
                events.append(mark(t_open, f'seg {k} start item={name} arm={arm} unit={u}', present))
                if k == gone_in and cur != a:
                    tg = t_open + 10.0
                    events.append(flip(tg, sw, cur, a, gone=True)); changes[sw].append((tg, a)); cur = a
                if k == flip_in:
                    other = a if cur == b else b
                    tf = t_open + 10.0
                    events.append(flip(tf, sw, cur, other, present)); changes[sw].append((tf, other)); cur = other
                events.append(mark(t_close, f'seg {k} end', present))
                segs.append((k, name, arm, t_open, t_close)); k += 1
                t = t_close + 0.01; present += 1500
        if cur != a:
            events.append(flip(t, sw, cur, a, present)); changes[sw].append((t, a))

    def value_at(sw, x):
        v = None
        for tt, val in changes.get(sw, []):
            if tt <= x:
                v = val
        return v
    return events, segs, value_at, t
