# Running mimir under systemd (non-Docker / local host)

The supported production deployment is Docker (`restart: unless-stopped` for
auto-restart; see [`docs/watchdog.md`](watchdog.md) for liveness). This page
covers the **non-Docker** path: running `mimir run` directly on a Linux host
under systemd — the host-service equivalent of the Docker restart policy, with
an operator notification on failure and **no watchdog sidecar required**.

> **macOS note:** systemd is Linux-only. On macOS the equivalent supervisor is
> **launchd** (a `.plist` with `<key>KeepAlive</key><true/>`). The unit files
> here won't load on macOS; ask if you want a launchd plist template.

## What you get

| | Mechanism |
|---|---|
| **Restart on crash** | `Restart=on-failure` + `RestartSec` + a `StartLimit*` crash-loop cap |
| **Notify on failure** | `OnFailure=mimir-alert@%n.service` → a oneshot that runs `mimir notify-restart` → ntfy / webhook |
| **Notify on *unclean restart*** | the agent itself, on its next boot (the clean-shutdown marker — works under systemd *and* Docker; see [`docs/watchdog.md`](watchdog.md)) |

The two notify paths are complementary:

- **`OnFailure=`** fires the moment systemd sees the unit *fail* — even if the
  process never comes back. This is the sidecar-free "death notice."
- **The clean-shutdown marker** fires when the agent *does* come back and
  reports that its previous run died uncleanly.

Neither catches "the whole host is down" — for that you still want an external
monitor polling `GET /health` off-box (e.g. a hosted uptime check). See
[`docs/watchdog.md`](watchdog.md).

## Install

Files live in [`deploy/systemd/`](../deploy/systemd/). Edit `User=`,
`MIMIR_HOME`, the `ExecStart` path, and `EnvironmentFile=` to match your
install before enabling.

**System-wide (root):**

```sh
sudo cp deploy/systemd/mimir.service        /etc/systemd/system/
sudo cp deploy/systemd/mimir-alert@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mimir
```

**User-level (no root — runs as your login user):**

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mimir
# survive logout:  sudo loginctl enable-linger "$USER"
```

## Configuration

`EnvironmentFile=` carries the same variables you'd put in `compose.env`:
the model credential (`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` + token),
plus the alert sinks the notifier needs:

- `NTFY_TOPIC` — ntfy.sh topic, and/or
- `MIMIR_WATCHDOG_WEBHOOK_URL` — a generic `{"text": ...}` webhook (Slack
  incoming-webhook / PagerDuty intake / custom).

Without at least one sink, `mimir notify-restart` runs but has nowhere to send.

## Verify

```sh
systemctl status mimir
journalctl -u mimir -f                 # follow logs

# Acceptance test — kill it hard and confirm: it restarts AND you get an alert.
sudo systemctl kill -s KILL mimir
#   → systemd restarts the unit (Restart=on-failure)
#   → OnFailure fires mimir-alert@ → ntfy/webhook "service failed"
#   → on the next boot the agent posts "restarted after an unclean shutdown"
```

## How this maps to the Docker deployment

| Concern | systemd (this doc) | Docker |
|---|---|---|
| Restart on exit | `Restart=on-failure` | `restart: unless-stopped` |
| PID 1 / zombie reaping | systemd | `tini` |
| Notify on failure | `OnFailure=` oneshot | clean-shutdown marker on next boot (+ autoheal/host monitor) |
| Detect a *wedge* | `mimir watchdog` (beat absence) or a host `/health` monitor | same |
| Recover a *wedge* | n/a — systemd won't restart a live-but-hung process either | autoheal sidecar / external kill |
