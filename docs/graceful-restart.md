# Graceful-drain restart (deploy-safe lifecycle)

Before this, a `docker compose restart` (or `stop`) killed whatever turn was
in flight — so deploys meant manually idle-checking each agent first. mimir now
**drains** on `SIGTERM`: it stops accepting new work, lets in-flight turns
finish (up to a bound), records a clean shutdown, and exits. (chainlink #510)

## The flow

On `SIGTERM`/`SIGINT` (what `docker compose stop`/`restart` and systemd send):

1. HTTP sites stop accepting connections; the shared shutdown signal closes
   live-event, turn-event and chat SSE streams. During `on_shutdown`, the
   scheduler stops **before** dispatcher admission closes, so an edge-trigger
   scan cannot advance its cursor while its events are rejected. New inbound
   then receives `503 queue_full_or_closed`; bridge events drop cleanly.
2. The dispatcher drain starts before aiohttp waits for handlers. At the start
   of graceful `on_cleanup`, the clean-shutdown marker is written **before**
   joining the drain or waiting for late resource teardown. It records graceful
   intent, not completed teardown, so an intended slow stop does not cause a
   false unclean-restart alert (see [`docs/watchdog.md`](watchdog.md)).
3. In-flight turns are **drained** up to `MIMIR_DRAIN_TIMEOUT_SECONDS`
   (default **30**). If the drain times out, still-running turns are **cancelled**
   and a `dispatcher_drain_timeout` event records the in-flight/queued counts.
4. Resources are closed and the process exits. The supervisor (Docker
   `restart:` / systemd) brings it back.

Net: `docker compose restart` mid-turn lets the turn finish first; deploys no
longer need a manual idle-check.

## Configuration — keep the shutdown inside the supervisor grace

`MIMIR_HTTP_SHUTDOWN_TIMEOUT_SECONDS` (default 5; must be finite and positive)
bounds aiohttp's wait for in-flight handlers. `MIMIR_DRAIN_TIMEOUT_SECONDS` (default 30) bounds the
dispatcher drain; it starts while handlers are closing. Budget conservatively:

**HTTP shutdown timeout + drain timeout + cleanup margin < supervisor stop grace**.

The **supervisor's** kill grace must exceed this total, or it can SIGKILL
before teardown finishes (and, if HTTP shutdown stalls, before the marker):

- **Docker Compose:** `stop_grace_period`. Docker's default is only **10s** —
  too short. The scaffold `compose.yml` sets `stop_grace_period: 45s`; match it
   to the budget above in operator composes. With defaults, 5 + 30 + 9 < 45.

  ```yaml
  services:
    mimir:
      restart: unless-stopped
      stop_grace_period: 45s   # > HTTP timeout (5) + drain (30) + cleanup margin
  ```

- **systemd:** `TimeoutStopSec` (default 90s — already comfortably above the
  drain default; lower it only if you also lower the drain). See
   [`docs/systemd.md`](systemd.md).

- **s6:** Set `S6_SERVICES_GRACETIME` to more than the same budget (in
  milliseconds); check Compose `stop_grace_period` too when both supervise.

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
