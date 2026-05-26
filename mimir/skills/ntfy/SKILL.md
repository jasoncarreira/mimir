---
name: ntfy
description: Send a phone-push notification to the operator via ntfy.sh. Use ONLY for genuine algedonic alarms — events the operator needs to know about within minutes, not hours. The push lands on their phone; the cost of misuse is operator desensitization (they mute the topic and the channel goes dark). Do not use for routine surfacing, status updates, or things that can wait for the next chat turn.
---

<!-- desc: Send a phone-push notification via ntfy.sh — for genuine algedonic alarms only (events the operator needs within minutes, not hours). -->

# ntfy.sh phone push

The operator-attention rung that survives chat-platform outage. It's the
phone-push step in the `fallback-chains` "reach the operator" chain
(rung 3, between email and the terminal state-file marker), and the
in-process delivery surface that gives algedonic alarms a wake-up
faster than the next turn cycle.

**Default state: don't use this.** Almost everything important enough
to surface to the operator can wait for the next chat turn (Discord/Slack
DM is rung 1) or a heartbeat result. ntfy.sh exists for the narrow case
where the chat rung is broken or the latency between events.jsonl
record and next-turn render is itself the failure.

## When to use

The allow-list (initially per chainlink #36, narrow on purpose):

1. **Cost-rate runaway** — sustained ≥$50/hr, well above the $30/hr
   PR-stacking baseline. Indicates a loop or fan-out the homeostat
   isn't catching.
2. **Plan window approaching saturation** — 5h or 7d window crossing
   90%. Operator needs to know before it locks me out, not after.
3. **Bot disconnected from Discord/Slack** — the canonical case where
   ntfy.sh is needed *because* the chat rung failed. Emit before the
   chat connection retries fail terminally so the operator knows to
   investigate.
4. **Saga write failures persisting across N consecutive turns** —
   silent memory loss is invisible to operator until they ask a
   question that should hit a recently-stored atom and get a miss.
5. **Scheduler wedge** — heartbeat hasn't fired in 90+ min when it
   should have. Dead-man poller surfaces this; the consume side is
   ntfy.sh.
6. **Repeated tool-denied path-outside-home in a tight window** —
   pattern indicating filesystem-layout misunderstanding that's
   wasting tool calls without making progress.

If a case feels like it *might* fit but you have to talk yourself
into it, it doesn't fit. The signal-to-noise ratio of this topic is
the only thing keeping it useful — every false-positive trains the
operator to ignore the next push.

## When NOT to use

- Routine heartbeat operator-attention surfacing (Discord DM is the
  right tool — see the `alert` skill).
- Status reports, "I shipped a PR", "I noticed X but it can wait."
- Anything where "next turn cycle" or "next time the operator
  messages me" is acceptable latency.
- Test pushes — if you need to verify the topic works, ask the
  operator to run a curl from their shell. Don't burn alarm signal
  on debugging.

## How

Read the env var `NTFY_TOPIC` from the container environment
(operator-set; opaque path component on ntfy.sh — the topic itself
is the secret). If the var is unset or empty, **skip the push
silently and log an event** — don't block the work that triggered
the alarm. The operator can see the missed push in events.jsonl on
next turn.

Bare primitive (use directly until the helper at chainlink #36 lands):

```bash
curl -fsS \
  -H "Title: <one-line summary>" \
  -H "Priority: <1-5; 4=high, 5=urgent>" \
  -H "Tags: <comma-separated emoji shortcodes>" \
  -d "<body — keep it short, the operator reads this on a lock screen>" \
  "https://ntfy.sh/$NTFY_TOPIC"
```

ntfy.sh accepts the body as the message text and uses `Title`,
`Priority`, `Tags` headers for metadata (see
https://docs.ntfy.sh/publish/ for the full set). The free tier is
fine for our volume; no auth — the topic name is the secret, so
treat `NTFY_TOPIC` like any other credential (don't echo it, don't
log it, don't paste it in a PR).

### Header conventions for mimir alarms

- **Title** — `mimir: <category>` so the operator can see at a
  glance which class of alarm (`mimir: cost runaway`,
  `mimir: discord disconnected`, `mimir: scheduler wedge`).
- **Priority** — `4` (high) for the allow-list above. Reserve `5`
  (urgent) for events where the operator needs to act in <5 min
  to prevent data loss. Most allow-list items are `4`.
- **Tags** — emoji shortcodes: `warning` for cost/plan, `rotating_light`
  for outages, `wrench` for scheduler issues, `floppy_disk` for saga.
  Visible in the push notification — helps the operator triage from
  the lock screen.

### Body shape

Keep the body to ~3 lines max. The operator reads it on a phone
lock screen — anything longer gets cut off and they have to unlock
to see context. Lead with the metric or symptom, then the
last-known-good window, then where to look for detail:

```
Cost rate $52.40/hr (3.0× baseline of $8.50/hr) for the past 18 min.
Last under-baseline reading: 18:14 UTC.
events.jsonl has details; check for fan-out or runaway loop.
```

## Dedup discipline

Until the helper at chainlink #36 lands the in-process dedup, the
discipline is **manual**: before posting, check that you haven't
already posted the same alarm key in the last hour.

Per-key dedup window: **1 hour**. A key is `<category>:<dedupe-anchor>`
— for cost-rate runaway it's `cost-runaway:<sustained-window-start>`,
for Discord disconnect it's `discord-down:<first-failure-ts>`. The
goal is one push per *event*, not one push per *symptom-observation*.

If you can't tell whether an alarm is a re-fire or a fresh event,
**don't fire**. False-negative on a re-fire is recoverable (the
alarm fires next time the dedup window expires); false-positive
on a re-fire trains the operator to mute the topic.

## Failure modes

- **`NTFY_TOPIC` unset** — log a `ntfy_skip_no_topic` event and
  proceed without the push. Don't block; don't error. The skill is
  optional infrastructure.
- **Network failure / ntfy.sh down** — log a `ntfy_post_failed`
  event with the curl exit code. Fall through to whatever rung is
  next in the chain (state-file marker for the operator-attention
  case). Don't retry in a tight loop — ntfy.sh outages are rare
  but real, and a retry storm would burn through tool calls.
- **HTTP 4xx (topic invalid, banned, etc.)** — log a
  `ntfy_post_rejected` event with the status code. This is an
  operator-config issue, not a transient — surface in the next
  heartbeat result.
- **HTTP 5xx (ntfy.sh internal)** — log and skip. ntfy.sh's
  free tier doesn't guarantee delivery; treat 5xx as
  best-effort-failed and don't retry.

## Composition with other skills

- **`alert`** — for non-urgent operator surfacing, use the
  `MIMIR_OPERATOR_ALERT_CHANNEL` (Discord/Slack DM). ntfy.sh is
  for cases where that channel may itself be the failure or where
  latency matters.
- **`fallback-chains`** — ntfy.sh is rung 3 of the canonical
  "reach the operator" chain (Discord → email → ntfy.sh →
  state-file marker). When firing it because rungs 1-2 failed,
  *also* write the state-file marker so the next session picks
  up the context even if the push was missed.
- **`pollers` / `world-scanning`** — dead-man pollers (cases 5+6
  on the allow-list) are the natural producer side. Pollers
  detect *absence* of expected signals; the consume side for
  high-severity findings is ntfy.sh.
- **`circuit-breaker`** — if the same alarm key has fired 3+
  times in a day, the dedup window is the wrong shape or the
  underlying issue isn't being fixed. Stop firing and surface
  the meta-pattern as a chainlink interest item instead — the
  operator already knows.

## Volume calibration

The right rate for ntfy.sh pushes is **<1 per week** under healthy
operation. If pushes are firing more often, either the allow-list
is too broad or there's a genuine systemic issue worth a chainlink
issue. Either way, the alarm itself is no longer the right
response — the meta-pattern is.

If a week passes with zero pushes, that's the expected state. Don't
manufacture pushes to "test" the channel — operator-driven test
runs are the right shape.
