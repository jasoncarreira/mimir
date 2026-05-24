# Spec: Bind-mount staleness health probe + self-restart

<!-- desc: detect VirtioFS bind-mount stale-inode states and recover by killing PID 1 -->

**Status:** filed 2026-05-06. Implementation in flight (PR pending).

## Problem

Twice in 24h (yesterday 16:31 UTC, today 23:58 UTC), the container's
bind mount of `<mimirbot>/state/home` → `/mimir-home` got into a
stale-inode state. The host directory itself stays intact, but the
guest kernel's view of the bind mount source becomes annotated with
`//deleted` (visible in `/proc/self/mountinfo`). Once that happens:

- The Python process at PID 1 keeps running fine — its own cwd
  (`/workspace/mimir`) is on a different bind mount.
- Cron-driven background work (oauth_usage_poll, scheduled_tick
  enqueue) keeps logging successfully — those don't spawn subprocesses
  rooted in `/mimir-home`.
- **But every SDK turn fails** with `ProcessError: Command failed with
  exit code 1`. The bundled claude binary spawns a bash subprocess
  with `cwd=/mimir-home`, bash calls `getcwd()`, gets ENOENT against
  the stale inode, prints `error: The current working directory was
  deleted, so that command didn't work.`, exits 1.
- The agent surfaces this as repeated turn errors but doesn't have any
  signal that the underlying *cause* is bind-mount-level.

The two confirmed incidents both had the same fingerprint, both
required operator intervention via `docker compose restart`, and the
second one corrupted ~2.5h of an active longmemeval benchmark before
the operator noticed.

## Why this is worth solving

- **Silent failure mode.** The agent keeps running, the harness keeps
  logging "OK"-shaped events for non-SDK work (cron, web UI, etc.),
  but the user-facing behavior is total — every turn errors. By the
  time the operator notices, hours of work can be lost.
- **Triggered by host-side conditions outside our control.** This is a
  classic Docker-on-macOS bind-mount issue (VirtioFS dentry cache
  losing track of the host inode under sustained writes / Time Machine
  / Spotlight / etc.). OrbStack inherits the same kernel-level
  symptom; it's not a Docker Desktop-specific bug.
- **The fix is mechanical** — a `docker compose restart` re-establishes
  the bind mount cleanly. There's no state to recover, no migration to
  do; we just need the agent to detect the condition and self-restart.

## Detection

The cheapest probe that reliably distinguishes healthy from stale:

```python
import subprocess
result = subprocess.run(
    ["pwd"],
    cwd="/mimir-home",   # or config.home
    capture_output=True,
    text=True,
    timeout=5,
)
stale = (
    result.returncode != 0
    or "deleted" in result.stderr.lower()
)
```

Why this works:

- **Spawns a fresh subprocess** with `cwd=/mimir-home`. The Python
  parent's cwd is unaffected (it's on a different bind mount); the
  subprocess inherits the *kernel's current resolution* of
  `/mimir-home`, which is the stale inode when the bind is broken.
- **`pwd`** is a coreutil that calls `getcwd(2)`. When the cwd inode
  is unlinked, `getcwd()` returns ENOENT and pwd prints "current
  working directory was deleted" to stderr, exits 1. When the cwd is
  healthy, pwd prints the path and exits 0.
- **No LLM cost, no `/mimir-home` writes, no API calls.** ~10ms per probe.

Probes that *don't* work for this:

- `os.path.exists("/mimir-home")` from the Python parent — returns
  True even when the bind is stale (parent does the resolution from a
  different namespace context).
- `claude --version` — doesn't `getcwd()`, succeeds even when stale.
- `claude --print "hi"` — works but bills an LLM call per probe.

## Auto-recovery

When the probe detects stale state, signal the container to restart:

```python
import os, signal
os.kill(1, signal.SIGTERM)
```

Why `kill 1`:

- Container's PID 1 is `uv run mimir run --home /mimir-home`
  (entrypoint from `mimirbot/start.sh`). The mimir user (UID 1000)
  owns it and can send signals to it.
- When PID 1 exits, the container exits.
- `mimirbot/compose.yml` already has `restart: unless-stopped`, so
  Docker re-creates the container, which re-establishes the bind
  mount cleanly. Confirmed by the two operator-driven restarts: bind
  mount comes back healthy.
- The new Python process picks up where the old one left off — events
  log replay, scheduler reload, all the existing startup machinery
  handles the rest.

## Restart-loop guard

If the probe is wrong (false positive) or the bind mount is
*persistently* stale across restarts, naive auto-restart spirals:
restart → probe fails immediately → restart → … forever. Bound the
behavior with a sliding-window restart counter:

- Persist restart timestamps to
  `<MIMIR_HOME>/.mimir/health-probe-restarts.jsonl` (one
  JSON line per triggered restart).
- Before triggering, count restarts within the last 60 minutes.
  If ≥3, **don't restart**. Log a high-priority algedonic event
  (`bind_mount_stale_persistent`) so the operator notices on
  next inspection. The bot will keep logging turn failures, but
  the system stops thrashing.
- Retention: rotate the file when it crosses 1000 lines, same
  pattern as the JSONL caps.

The threshold (3 in 60 min) is chosen because:
- A genuine VirtioFS staleness event clears on first restart in
  practice (we've seen this twice). 3 restarts in an hour means
  something else is wrong — operator action needed.
- The cost of one false-positive restart is small (~10s of downtime,
  some in-flight benchmark turn lost). The cost of three restarts
  in an hour without recovery is "the operator has been ignoring
  algedonic events for hours" — at that point the bot should stop
  thrashing and let the operator take over.

## Scheduling

The probe should run on a tight cadence — fast enough to limit damage,
slow enough that the probe itself isn't the noise:

- **60-second interval** (cron `* * * * *`). Bounds maximum damage
  to ~60 turns lost in the worst case (synthesis turns at 10-min
  cadence, scheduled ticks at 1h, user turns ad hoc — at 1-min
  probe cadence we catch the staleness within the first SDK turn
  attempt).
- Register via the existing scheduler (`mimir.scheduler`) like the
  OAuth poller already does. Auto-installed at startup; no
  scheduler.yaml entry needed.
- Disabled when not in a Linux container — the issue is specific to
  Linux-on-macOS bind mounts via VirtioFS. Detection: check
  `/proc/self/mountinfo` exists AND contains a `virtiofs` entry. If
  not (e.g., bare-metal Linux, native filesystem), the probe is a
  no-op. Bare-metal deployments don't get health-checked but also
  don't have the bug.

## Algedonic events

Three events fire from this loop:

| Event | Polarity | When | Renderer text |
|---|---|---|---|
| `bind_mount_stale_detected` | negative | probe fails, about to trigger restart | "Bind mount stale-inode detected (`/mimir-home`); auto-restart triggered (count: N/3 in last 60min)" |
| `bind_mount_stale_persistent` | negative | probe fails, AND restart guard tripped (≥3 in 60min) | "Bind mount stale-inode persists despite N auto-restarts in last 60min; operator action needed (try `docker compose down && up`)" |
| `bind_mount_recovered` | positive | probe passes after a previous restart | "Bind mount healthy again after auto-restart" |

All wired into `mimir/feedback.py` like the existing OAuth events;
add to `_FIRST_OCCURRENCE_ONLY_KINDS` so a flapping probe doesn't
crowd the algedonic block.

## Files touched

- `mimir/health_probe.py` (new) — the probe logic + restart trigger +
  bookkeeping. ~120 LOC.
- `mimir/scheduler.py` — `add_health_probe_job(home, cron_expr)`
  similar to `add_oauth_usage_poll_job`. ~30 LOC.
- `mimir/server.py` — wire the cron up at startup. Detect VirtioFS
  via `/proc/self/mountinfo` and skip on non-VirtioFS deployments.
  ~10 LOC.
- `mimir/feedback.py` — three new event kinds + render functions.
  ~30 LOC.
- `mimir/config.py` — `health_probe_cron: str` (default
  `"* * * * *"`), `health_probe_max_restarts_per_hour: int`
  (default `3`). ~10 LOC.
- `tests/test_health_probe.py` (new) — see test plan. ~150 LOC.

Total: ~350 LOC.

## Test plan

The probe path itself is mockable end-to-end:

- `test_probe_passes_when_subprocess_returns_zero` — mock
  `subprocess.run` to return CompletedProcess with returncode=0 and
  empty stderr; assert probe reports healthy.
- `test_probe_detects_deleted_in_stderr` — mock returns returncode=1
  and `b"deleted"` in stderr; assert probe reports stale.
- `test_probe_treats_timeout_as_stale` — mock raises
  TimeoutExpired; assert probe reports stale.
- `test_restart_guard_allows_first_restart` — empty bookkeeping
  file; assert restart fires.
- `test_restart_guard_blocks_after_threshold` — bookkeeping file
  with 3 timestamps in last 60min; assert restart does NOT fire,
  `bind_mount_stale_persistent` event emits.
- `test_restart_guard_lets_old_timestamps_age_out` — bookkeeping
  file with 5 timestamps from >2h ago + 1 from 30min ago; assert
  count is 1, restart fires.
- `test_recovery_event_after_restart` — first probe stale, second
  passes; assert `bind_mount_recovered` fires once.
- `test_probe_skipped_on_non_virtiofs` — mock
  `/proc/self/mountinfo` to contain only ext4 entries; assert
  probe is a no-op.

The actual `os.kill(1, SIGTERM)` is gated behind a `_send_restart_signal()`
function that tests monkeypatch to a no-op. Don't actually fork the
test runner.

## Out of scope

- **Fixing VirtioFS itself** — out of our control, lives in OrbStack /
  Docker Desktop / Linux kernel.
- **Recovery without restart** — there's no kernel-level "refresh this
  bind mount's dentry cache" syscall available to userspace. Restart
  is the only knob.
- **Slack/Discord operator alert on restart** — algedonic events
  surface in the next turn's prompt, which is sufficient. Adding an
  out-of-band alert is a separate plumbing problem (operator alert
  channel — already filed in v0.4 §6).
- **Probing other bind mounts** (`/workspace`, `/benchmark`, etc.).
  Those don't carry critical agent state; let them fail loudly via
  their natural failure mode rather than adding probes for each.
- **Switching to named volumes** — alternative mitigation, separate
  decision (loses host-side inspection convenience). If the
  health-probe approach proves insufficient (e.g., we hit it
  weekly), revisit named volumes as a follow-up.

## Edge cases the implementation must handle

- **Probe fires during agent shutdown** — `os.kill(1, SIGTERM)` while
  the container is already shutting down should be a no-op. Wrap in
  `try/except OSError` and swallow ESRCH.
- **Probe fires during container start** — the bind mount might not
  be fully ready in the first ~1s. Skip the first probe iteration
  if uptime < 30s (read `/proc/1/stat` field 22, divide by `CLK_TCK`).
- **Bookkeeping file is corrupt JSON** — log a warning, treat as
  empty (zero recent restarts). Don't refuse to restart on a
  bookkeeping file we can't read — that'd defeat the whole purpose.
- **Probe's `subprocess.run` blocks forever** — bounded by `timeout=5`
  in the call. If timeout itself hangs (rare kernel issue), the cron
  job gets cancelled by APScheduler's `misfire_grace_time` on the
  next iteration. Set `misfire_grace_time=30` on the job.
- **Multiple probes overlap** — set `max_instances=1, coalesce=True`
  on the APScheduler job, same pattern as `add_oauth_usage_poll_job`.
- **`/mimir-home` doesn't exist** (non-mimirbot deployment, just bare
  mimir) — the probe still runs from `config.home`. If that path
  doesn't exist at all, the probe fails the same way it would on
  staleness. Acceptable; means home is misconfigured anyway.
