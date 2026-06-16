# Liveness watchdog — the dead-man's-switch (chainlink #507)

mimir can alert you when something goes *wrong* (algedonic feedback, the
`alert` skill, `ntfy`) — but all of those fire from a **live** process. A
**hard** failure — OOM-kill, `SIGKILL`, a wedged event loop, the container
dying — leaves the agent unable to say anything. Nothing notices its
**absence**.

The watchdog closes that gap with two halves split across the process
boundary:

| Half | Where | What |
|---|---|---|
| **Beat** | in the agent | a background task rewrites `.mimir/liveness.json` every `MIMIR_LIVENESS_BEAT_SECONDS` (default 60). As an event-loop task it stops if the loop dies *or wedges*. (`.mimir/` is the gitignored runtime path — the beat must not churn the tracked home.) |
| **Watch** | a **separate** process (`mimir watchdog`) | reads the beat's age; when it goes stale, pushes an **out-of-band** alert. Survives the agent dying because it isn't the agent. |

The beat file is the primary signal — the watcher just reads the (bind-mounted)
home, no network/port needed. `GET /health` is a secondary signal a hosted
uptime monitor can poll.

## The watcher

```
mimir watchdog [--home DIR] [--interval 60] [--stale-after 180] [--once]
               [--restart-on-stale] [--restart-grace 10]
```

- **Loop mode** (default): checks every `--interval`s; alerts once when the
  beat first goes stale (only *after* it has seen the agent alive — so a
  watcher started before the agent doesn't false-alarm), and pushes a recovery
  notice when the beat returns.
- **`--once`** (cron mode): one check, exit code `1` if the agent is down.
- **`--restart-on-stale`**: in addition to alerting, *act* — kill the agent (by
  the PID in its beat; SIGTERM, then SIGKILL after `--restart-grace`) so the
  supervisor restarts it. This is the **wedge-recovery** path: a wedged-but-alive
  process never exits, so neither s6 supervision nor Docker `restart:` would
  restart it on their own. One attempt per outage; a fresh beat re-arms it.
  Same-container only (shared PID namespace; no docker socket needed) — meant
  for the in-container s6 watcher below.

### Sinks (configure at least one — or it can't alert anyone)

The watcher is **not** ntfy-locked. It fans out to whichever of these is set:

- **ntfy** — `NTFY_TOPIC` (e.g. `jcarreira_mimirbot`). Zero-setup phone push.
- **Webhook** — `MIMIR_WATCHDOG_WEBHOOK_URL`. POSTs `{"text": "<title>\n<body>"}`,
  the shape a **Slack incoming webhook**, PagerDuty/Opsgenie intake, or any
  custom endpoint accepts. Independent of the agent's Slack bot, so it survives
  the agent dying.

Both can be set; both fire. With neither set, the watcher logs a warning and
keeps watching (useless, but harmless).

## Wiring it

The watcher must be a separate **process** (so a wedged event loop — even one
holding the GIL — can't freeze it). Where that process runs decides which
failures it catches:

### A. In-container s6 sidecar (the scaffold default) — wedge recovery

The scaffold image runs s6-overlay as PID 1 supervising **two** services: the
agent (`mimir run`) and the watcher (`mimir watchdog --restart-on-stale`). See
`deploy/s6-overlay/`. On a stale beat the watcher alerts *and* kills the agent;
s6 restarts that service in place. Tune the threshold with
`MIMIR_WATCHDOG_STALE_AFTER` (default 300s — high enough not to false-kill a
legitimately busy agent).

This is the right tool for a **wedge** (and fast process-crash restart). What it
*can't* do is alert when the whole **container** dies — it dies with it. That
case is covered instead by Docker `restart:` bringing the container back + the
clean-shutdown marker paging on reboot (see below); only "dead and never comes
back" (host/daemon down) escapes, which is option D.

### B. Compose sidecar (separate service, shares the home volume)

```yaml
services:
  mimir:
    # … the agent …
  mimir-watchdog:
    image: mimir:latest          # same image; just runs a different command
    command: ["mimir", "watchdog", "--home", "/mimir-home", "--stale-after", "180"]
    environment:
      NTFY_TOPIC: ${NTFY_TOPIC}                       # and/or:
      MIMIR_WATCHDOG_WEBHOOK_URL: ${MIMIR_WATCHDOG_WEBHOOK_URL}
    volumes:
      - .:/mimir-home:ro          # read the beat; read-only is enough
    restart: unless-stopped
    depends_on: [mimir]
```

### C. Host cron / launchd (outside docker entirely)

`--once` is **exit-code-only** — it checks and exits `1` when down, `0` when up,
and posts **nothing** itself. Each cron tick is a fresh process with no memory,
so self-paging would re-page on every tick of one sustained outage; instead the
**cron monitor pages** (it dedupes per outage). Wire the pager yourself:

```cron
# pipe-to-pager: cron only mails/pages when the command exits non-zero
*/2 * * * * mimir watchdog --home /path/to/home --once --stale-after 180 || /usr/local/bin/page-me "mimir down"
```

Or hand the exit status to a dead-man monitor (healthchecks.io etc.) that pages
once when check-ins stop. The `NTFY_TOPIC` / `MIMIR_WATCHDOG_WEBHOOK_URL` sinks
above are for the **loop-mode** watcher (A/B), which dedupes in memory.

### D. Hosted uptime monitor on `/health`

Point an external uptime monitor (UptimeRobot, Healthchecks.io dead-man timer,
etc.) at the agent's `:8080/health`. If the loop wedges or the process dies,
`/health` stops responding and the hosted monitor pages you — fully external.

## Verifying it (the acceptance test)

```
docker kill -s KILL mimir        # hard-kill; the agent can't say goodbye
# within (stale_after + interval), the operator receives the out-of-band alert
# from the watchdog — with zero action from the dead agent.
```

Graceful shutdown and deliberate alerts are already covered by `ntfy` + the
`alert` skill; the watchdog covers only the case where the agent **can't speak
for itself**.

## Complementary mechanisms

The watchdog is one layer. Three others stack with it, each covering a
different failure domain:

| Mechanism | Where | Catches | Misses |
|---|---|---|---|
| **Clean-shutdown marker** | in the agent (next boot) | crash / OOM / hard-restart that **came back** — the agent reports it restarted uncleanly (`.mimir/session.json`, `liveness_unclean_restart` event + out-of-band notice). No sidecar. | a death it never recovers from |
| **`mimir watchdog`** (this doc) | separate process | the agent **absent** — dead or wedged, even when it never comes back | the whole host being down (run it off-box for that) |
| **Docker `HEALTHCHECK` → `/health`** | the container | a wedged loop (the poll times out → `unhealthy`) | **does not restart on its own** — `restart:` reacts only to *exit* |
| **`OnFailure=` (systemd)** | the host supervisor | the unit entering `failed` (see `docs/systemd.md`) | the host itself being down |

**Restarting a wedge.** A wedged-but-alive process never *exits*, so neither
`restart: unless-stopped` nor systemd `Restart=` will restart it. The
`HEALTHCHECK` *detects* it; to *act* on it, add one of:

- an **autoheal sidecar** (e.g. `willfarrell/autoheal`) that watches the
  healthcheck and restarts the container on `unhealthy` — off-the-shelf, no
  code; the natural actuator for the healthcheck above;
- a **Swarm / k8s liveness probe** (restart-on-probe-failure is built in);
- an in-process **loop-watchdog thread** that force-exits a stalled loop so the
  `restart:` policy fires.

Whole-host failure is the one case nothing on-box can catch — that's the
hosted-monitor-on-`/health` layer (option D above).
