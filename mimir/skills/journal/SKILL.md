---
name: journal
description: Append a dated entry to a daily journal under `state/journal/YYYY-MM-DD.md`. Use to checkpoint your reasoning at meaningful turn boundaries — what the user wanted, what you actually did, what you predict next. The journal is a long-running log you can grep later when something surprising shows up and you want to know "what did I think was going on at the time?".
allowed-tools:
  - Bash
  - Read
  - send_message
---

# Journal

Append-only daily log under `state/journal/`. Each entry captures one
checkpoint; days roll over at midnight UTC. Read with `Read`, search with
`grep`. Don't edit prior entries — write a new one if your understanding
shifted.

The journal is for *your* future self, not the operator. Operator-facing
status goes in chat replies; design decisions go in code/PR comments;
algedonic signals go in the feedback loop. The journal is where you tell
yourself the story so the next turn (or next week) can read it back.

## When to write an entry

- Closing out a non-trivial turn (anything that involved more than a single
  reply or a single tool call).
- After making a prediction about how something will go — write the
  prediction down so you can check it later.
- When a hypothesis turns out wrong, even if the surface fix is small —
  the *why-I-thought-it-would-work* is the load-bearing part.
- When you decide *not* to do something the user nudged toward, with the
  reason. Future-you needs to see the reasoning if the same nudge comes
  back next week.

## When NOT to write

- Mid-turn scratch — use the Plan tool or just hold it in context.
- Status reports for the operator — those go in `send_message` replies.
- Routine successful tool calls — events.jsonl already captures those.

## Format

```markdown
## <ISO-8601 ts> — <one-line headline>

**User wanted:** <what they actually asked for, in their words if useful>

**What I did:** <the action you took, with file paths or PR numbers>

**Predictions:** <what you expect to break or surprise; the next turn or
the next operator session can grade these>

**Open questions / followups:** <only if there are real ones>
```

Keep entries terse. The headline is the index — make it greppable.

## Append pattern

```bash
DAY=$(date -u +%Y-%m-%d)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$MIMIR_HOME/state/journal"
cat >> "$MIMIR_HOME/state/journal/$DAY.md" <<EOF

## $TS — <headline>

**User wanted:** ...

**What I did:** ...

**Predictions:** ...
EOF
```

## Reading back

```bash
ls "$MIMIR_HOME/state/journal/" | tail -n 10               # last 10 days
grep -l "<keyword>" "$MIMIR_HOME/state/journal/"*.md       # which days mention X
grep -B 2 -A 10 "<keyword>" "$MIMIR_HOME/state/journal/"*.md
```

When the operator asks "what was I working on last Tuesday?" or "did we
ever try X?", the journal is the source of truth — not events.jsonl, which
captures actions, not reasoning.
