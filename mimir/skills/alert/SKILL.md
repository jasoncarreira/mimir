---
name: alert
description: When and how to use the operator alert channel for high-priority signals that don't fit the current conversation. Pair with MIMIR_OPERATOR_ALERT_CHANNEL in your system prompt's Operator config section.
---

# Operator alert channel

If `MIMIR_OPERATOR_ALERT_CHANNEL` is configured, the system prompt's
Operator config section will name a channel id (e.g. `dm-slack-U05ABC` or
`dm-discord-NNN`). Use it for high-priority signals to the operator that
*don't fit the current conversation*. If the line is absent, the feature
is off — fall back to your usual options (heartbeat backlog, the channel
you're already in).

## Use it for

- Critical errors you can't recover from on your own
- Urgent findings during a heartbeat the operator should know about now
- Dispatch failures or systemic issues that need human attention
- Time-sensitive escalations where the cost of waiting is high

## Don't use it for

- Routine updates ("I did a thing today")
- Status reports or check-ins
- Low-priority observations (those go in `state/heartbeat-backlog.md`)
- Anything that can wait for the next time the operator messages you

## How

The alert channel is a normal channel id — the registered bridge
dispatches by prefix. So just call `send_message` against it like any
other channel:

```
send_message(channel_id="dm-slack-U05ABC", text="...")
```

Make the message specific and self-contained. The operator may be
reading it out of context, so:

- Lead with what happened ("Discord dispatch failing for the past 4 hours")
- Include the key data ("DiscordError: Forbidden, ts: 2026-05-01T14:02Z")
- Say what you've already tried, if anything
- End with what you need from them, if anything

## Volume calibration

If you find yourself using this channel more than once a day, you're
probably misusing it — re-read the "Don't use it for" list. The signal
loses meaning when it goes off too often.
