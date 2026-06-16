# Graceful-drain restart (deploy-safe lifecycle)

Before this, a `docker compose restart` (or `stop`) killed whatever turn was
in flight — so deploys meant manually idle-checking each agent first. mimir now
**drains** on `SIGTERM`: it stops accepting new work, lets in-flight turns
finish (up to a bound), records a clean shutdown, and exits. (chainlink #510)

## The flow

On `SIGTERM`/`SIGINT` (what `docker compose stop`/`restart` and systemd send):

1. The dispatcher is **closed** — new inbound is rejected cleanly (`POST /event`
   → `503 queue_full_or_closed`; bridge events drop rather than half-process).
2. In-flight turns are **drained**: it waits up to `MIMIR_DRAIN_TIMEOUT_SECONDS`
   (default **30**) for the live turns to finish.
3. If the drain times out, the still-running turns are **cancelled** and a
   `dispatcher_drain_timeout` event is logged (with the in-flight/queued counts)
   — so a cut-off turn is visible, and shutdown stays deterministic instead of
   hanging until Docker SIGKILLs.
4. The clean-shutdown marker is set (so the next boot does **not** raise an
   "unclean restart" alert — see [`docs/watchdog.md`](watchdog.md)), and the
   process exits. The supervisor (Docker `restart:` / systemd) brings it back.

Net: `docker compose restart` mid-turn lets the turn finish first; deploys no
longer need a manual idle-check.

## Configuration — the two timeouts must agree

`MIMIR_DRAIN_TIMEOUT_SECONDS` (default 30) bounds the in-process drain. The
**supervisor's** kill grace must be **≥** that, or it SIGKILLs straight through
the drain:

- **Docker Compose:** `stop_grace_period`. Docker's default is only **10s** —
  too short. The scaffold `compose.yml` sets `stop_grace_period: 45s`; match it
  to (drain timeout + a few seconds of other cleanup) in operator composes.

  ```yaml
  services:
    mimir:
      restart: unless-stopped
      stop_grace_period: 45s   # >= MIMIR_DRAIN_TIMEOUT_SECONDS (default 30)
  ```

- **systemd:** `TimeoutStopSec` (default 90s — already comfortably above the
  drain default; lower it only if you also lower the drain). See
  [`docs/systemd.md`](systemd.md).

Set `MIMIR_DRAIN_TIMEOUT_SECONDS=0` to wait unbounded (not recommended with a
supervisor that has its own kill grace).

## Verifying it

```sh
# start a long turn, then restart mid-flight:
docker compose restart mimir
#  → POST /event during the window returns 503
#  → the in-flight turn finishes (within the drain timeout) before exit
#  → logs/events.jsonl shows `dispatcher_draining`; only a turn that overran
#    the timeout shows `dispatcher_drain_timeout`
#  → next boot does NOT post an unclean-restart notice (clean marker was set)
```
