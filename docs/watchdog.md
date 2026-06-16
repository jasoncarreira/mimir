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
| **Beat** | in the agent | a background task rewrites `state/liveness.json` every `MIMIR_LIVENESS_BEAT_SECONDS` (default 60). As an event-loop task it stops if the loop dies *or wedges*. |
| **Watch** | a **separate** process (`mimir watchdog`) | reads the beat's age; when it goes stale, pushes an **out-of-band** alert. Survives the agent dying because it isn't the agent. |

The beat file is the primary signal — the watcher just reads the (bind-mounted)
home, no network/port needed. `GET /health` is a secondary signal a hosted
uptime monitor can poll.

## The watcher

```
mimir watchdog [--home DIR] [--interval 60] [--stale-after 180] [--once]
```

- **Loop mode** (default): checks every `--interval`s; alerts once when the
  beat first goes stale (only *after* it has seen the agent alive — so a
  watcher started before the agent doesn't false-alarm), and pushes a recovery
  notice when the beat returns.
- **`--once`** (cron mode): one check, exit code `1` if the agent is down.

### Sinks (configure at least one — or it can't alert anyone)

The watcher is **not** ntfy-locked. It fans out to whichever of these is set:

- **ntfy** — `NTFY_TOPIC` (e.g. `jcarreira_mimirbot`). Zero-setup phone push.
- **Webhook** — `MIMIR_WATCHDOG_WEBHOOK_URL`. POSTs `{"text": "<title>\n<body>"}`,
  the shape a **Slack incoming webhook**, PagerDuty/Opsgenie intake, or any
  custom endpoint accepts. Independent of the agent's Slack bot, so it survives
  the agent dying.

Both can be set; both fire. With neither set, the watcher logs a warning and
keeps watching (useless, but harmless).

## Wiring it (must be out-of-process)

A watcher *inside* the agent container won't survive the container dying — run
it as a **separate** service or on the host.

### A. Compose sidecar (separate service, shares the home volume)

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

### B. Host cron / launchd (outside docker entirely)

```cron
*/2 * * * * NTFY_TOPIC=jcarreira_mimirbot mimir watchdog --home /path/to/home --once --stale-after 180
```

`--once` exits non-zero when down, so it also composes with a monitoring cron
that pages on a failing command.

### C. Hosted uptime monitor on `/health`

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
