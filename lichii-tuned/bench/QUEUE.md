# Lichii Bench queue and results (for the cloud session)

The bench autopilot on the test PC (`bench.bat autopilot`, see README.txt) reads plans from this
branch and writes results to the `bench-results` branch. The cloud session can't reach the PC;
these two places are the whole interface.

## Queueing a plan

- Add `lichii-tuned/bench/queue/<id>.json` on `tuned` and push. The autopilot polls every 5 min.
  Pushes under `lichii-tuned/bench/` don't start a CI build (the workflow ignores that folder).
- `<id>` is the plan's identity: letters, digits, `.`, `_`, `-`, at most 81 characters. Plans run
  in file-name order, so number them (`0007-r68-ab48.json`).
- The file is an ordinary bench plan (`bench.bat validate` rules: `kind: fountain`, arms, blocks
  or design, rules), plus `build`:
  - `"r68"`: that build;
  - a commit prefix (7 to 40 hex digits): the build of that commit on `tuned`;
  - `"latest"`: the newest installed build when the plan starts.

  The autopilot replaces `build` with the build's commit for the log check and adds an
  `autopilot` object (plan id, build, start count) to the session's `plan.json`.
- Queued plans also have to pass a path rule: an arm's `base` is a launcher file name in the
  build folder, and an env value that is a path must lie in `C:\LichiiBench` or the build folder.
- A plan runs once. Editing its file before it starts replaces it; deleting the file withdraws
  it. To run a plan again, add it under a new name.
- `queue/control.json` with `{"pause": true}` stops new launches (a running launch finishes);
  `false` or deleting the file resumes.

## Builds

- The newest successful `lichii-tuned.yml` run on `tuned` (push or manual, from this repository
  only) is installed on its own: its `lichii-tuned-r<N>-pgo-lto` artifact goes into
  `<builds>\r<N>`, then the build is prepared once (`bench.bat prepare`, up to 75 min).
- A plan naming an older build installs it if its artifact is still there (artifacts last 3
  days) or its folder already exists; otherwise it is refused. A plan for a build that is still
  in CI waits for it.
- A folder is never overwritten or deleted. With less than 10 GB to spare, downloads wait until
  the owner deletes old builds (the heartbeat says so).

## Plan statuses

| Status | Meaning |
|---|---|
| `queued` | Read, waiting its turn (or queued again after a restart, a long pause or `autopilot-stop`). |
| `waiting` | Its build isn't installed or prepared yet; `reason` says which. |
| `running` | Its sitting is running. |
| `done` | The sitting finished; results are up. |
| `stopped` | The sitting stopped on a check (wrong setting, clock state, two failed slots, ...) or its build's preparation failed twice; results are up. Fix and queue under a new name. |
| `refused` | Not valid (JSON, plan rules, path rule), or its build can't be had. |
| `aborted` | The owner held F3 (or pressed Ctrl+C). Not retried. |
| `withdrawn` | Its file left the queue before it started. |
| `gave-up` | Interrupted 3 times by restarts or long pauses. |

Owner input during a launch doesn't change the status: that launch is closed and its slot runs
again once he has been idle 15 min (the run is kept as `interrupted` in the session, and a note
in `runs.json` names it). After more than 6 h between two launches the sitting stops and the
plan is queued again from the start.

## The results branch

```
git fetch origin bench-results
git show origin/bench-results:status/autopilot.json
git ls-tree -r --name-only origin/bench-results results/
```

- `status/autopilot.json`, the heartbeat: `updated`, `state` (`running`, or how it stopped),
  `phase` (what it is doing or waiting for), `owner_idle_min`, `builds` (r, commit, folder,
  prepare status and the last attempt), `plans` (status and reason of each), `counters`
  (sittings, prepares, owner interruptions), `save_changes`, `network_error`,
  `install_problems` (why a build isn't installed: disk space, a folder without lichii.exe, a
  failed download), `outbox` (results not sent yet), `session` (the running sitting's status.json) and `recent` (the last 30 log
  lines). It is written about every 30 min and after each change, between launches only: during
  a preparation or a long launch it can be up to ~80 min old. Older than that with `state`
  `running` means the PC is off, asleep or offline.
- `status/plans/<id>.json`: the plan's `status`, `reason`, `build_r`, `session`, `result` (its
  results folder), `starts` and times.
- `results/<session>/` for each sitting and each preparation:
  - `result.json`: `kind` (`sitting` or `prepare`), `plan`, `build`, `status` (`finished`, the
    runner's stop reason, or `interrupted` for a sitting cut short by a restart), `plan_status`,
    `interrupted_launches`, `zip`, `parts`, `zip_bytes`, `zip_sha256`, `left_out`;
  - `summary.txt` and `runs.json`, as in the zip;
  - the zip: `<session>.zip`, or parts `<session>.zip.001`, `.002`, ... (25 MiB each) to join
    with `cat <session>.zip.0* > <session>.zip` and check against `zip_sha256`. A zip over
    400 MiB is rebuilt without dumps (`-slim`), then without logs (`-nologs`); `left_out` says so.

Feed the zip to `analyze.py` as before. Only the autopilot commits to `bench-results`; the cloud
session reads it and writes only to `queue/` on `tuned`. The logs carry the owner's Windows user
name in paths, so the results stay in this private repository.
