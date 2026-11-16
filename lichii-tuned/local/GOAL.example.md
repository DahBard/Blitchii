# Goal (copy to GOAL.md and fill in)

Goal: in one sentence, what must be true. Example: "Steady 60 fps at the Market fountain with a
1% low of 40 or better, at native 1440p to 4K FSR 4, frame generation off."

Measured by: the bench plan kind and metric. Example: fountain, fresh launches, perf-trace-noprofile
baseline; fps and 1% low over the matched 100 s windows; at least 4 runs per arm.

Reached when: the exact numbers. Example: mean fps >= 59.5 and 1% low >= 40 in a sitting of 8
runs, with no run under 58 fps.

Don't touch: limits on changes. Example: no lower resolution or render scale, no frame
generation, nothing that costs the GPU back thread.

Budget: when to stop if it isn't reached. Example: 20 iterations, or 3 days, or every candidate
in the backlog tried.

Notify me: when reached, when stopped as impossible (with the evidence and the alternatives), and
on anything that needs me (a crash the bench can't get past, a candidate that needs a big upstream
merge or a Fritchii port).
