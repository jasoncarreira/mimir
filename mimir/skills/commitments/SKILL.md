---
name: commitments
description: How to read, resolve, and reason about commitments — durable records of future obligations (your own promises and the operator's requests). Use whenever the `## Upcoming commitments` prompt block surfaces something you might act on, or when you want to inspect what's pending beyond what the block shows.
---

# Commitments

A commitment is a durable record of a future obligation: a promise you
made ("I'll review the PR Thursday"), a user request to follow up
("let me know how the deploy goes"), or an open loop worth revisiting
("circle back on the auth migration once #108 lands"). The store is
append-only JSONL at `<home>/.mimir/commitments.jsonl`; the agent
sees active records in the per-turn `## Upcoming commitments` prompt
block.

## What you see turn-to-turn

The `## Upcoming commitments` block lists active records scoped to
the current channel (plus unbound commitments — those visible
everywhere). Each line:

```
- [c-abc123def0] in 3d Review PR #142 by Thursday — for @alice
- [c-2f4a8e1234] (next sprint) Audit logging coverage in dispatcher (unbound)
- [c-77ee0011aa] [care] today Check in with bob after the outage — snoozed×2
```

Components:

- **`[c-...]`** — the commitment id you pass to the lifecycle tools.
- **Due phrase** — `in Nd` / `today` / `overdue Nd` when there's an
  explicit time anchor, or `(natural-language hint)` when the
  extractor saw a phrase like "Thursday" / "once #108 merges" but
  no concrete date. `(no anchor)` means no time info at all.
- **Text** — the natural-language description (capped at ~120 chars
  at write time).
- **`— for @name`** — the recipient, if any. The commitment is
  *for* that person; mention them when you act on it.
- **`(unbound)`** — the commitment isn't tied to a single channel.
  Surfaces everywhere; act on it wherever it makes sense.
- **`[care]` / `[personal]`** — sensitivity prefix. CARE = wellbeing
  follow-ups (someone's hard week, health item). PERSONAL = mundane
  individual asks. Tone the response accordingly.
- **`— snoozed×N`** — you've already punted this N times. ≥3 fires
  an algedonic pileup alarm. If you see ≥2, ask yourself whether
  to actually do it, dismiss it, or rescope.

The block is capped at 8 entries; if a `…and N more` footer appears,
use `commitment_list` to see the rest.

## When to act

Resolve a commitment when:

- **You followed through** → `commitment_complete`. The promised
  thing happened. If you just delivered the answer in this turn,
  pass `message_id` so the completion links to the reply that
  fulfilled it.
- **It's no longer relevant** → `commitment_dismiss` with a short
  `reason`. Cancelled, resolved by someone else, context expired.
  Distinct from complete: dismissed means "didn't need doing,"
  completed means "I did it."
- **You can't do it yet but it still matters** → `commitment_snooze`.
  Pass `for_days` (relative — `7` for a week, fractional OK) or
  `until_unix` (absolute). Include a `reason` so future-you can
  read the audit trail.

If a commitment shows up but you don't recognize it, that's a signal
to check the `Recent session summaries` block or `commitment_list`
for context before acting. Don't blindly dismiss — the extractor may
have caught something real you've forgotten.

## When NOT to act

- **Mid-conversation context** — if the operator is still actively
  asking about something, don't immediately mark the original
  commitment complete just because you mentioned it. Wait until the
  thread settles. The block surfaces the same record each turn; one
  more turn of patience won't lose anything.
- **Repeated snoozing without progress** — if you see your own
  `snoozed×2` count climbing on the same record, you're avoiding
  it. Either commit to a date and do it, or dismiss it with a
  reason ("blocked indefinitely on X" or "the operator deprioritized
  this in turn Y"). Pileup alarms (≥3 snoozes) are a signal that
  *you* are the problem, not the commitment.
- **Synthetic ticks** — heartbeat / poller-tick turns don't see the
  commitments block on purpose. The block is suppressed on
  `scheduler:*` and `poller:*` channels because acting on a stranger's
  commitment from a heartbeat context produces weird routing. Don't
  go fishing for commitments to resolve in a heartbeat.

## Tool reference

### `commitment_complete`

```
{"id": "c-abc123def0", "message_id": "<optional>"}
```

Terminal — completed commitments can't be re-opened. Pass `message_id`
when the just-sent reply fulfilled the promise so the link is
auditable.

### `commitment_snooze`

```
{"id": "c-abc123def0", "for_days": 7, "reason": "blocked on review"}
```

OR

```
{"id": "c-abc123def0", "until_unix": 1714521600, "reason": "after launch"}
```

Exactly one of `for_days` / `until_unix`. The snooze count
auto-increments — chronic punting is visible.

### `commitment_dismiss`

```
{"id": "c-abc123def0", "reason": "operator cancelled in turn ..."}
```

Terminal. Reason is optional but please include one so the audit log
explains why a record left the surface.

### `commitment_list`

```
{"channel_id": "<optional>", "status": "<optional>"}
```

Returns JSON. Defaults to active records (pending + delivered +
snoozed) across all channels when both args are omitted. Use when:

- The prompt block's `…and N more` footer is hiding records.
- You want to see snoozed/delivered records the operator is asking
  about.
- You want a channel-scoped view (pass `channel_id`).

## Algedonic signals

The Phase 2b poller emits three event kinds you may see in the
`Recent feedback signals` block:

- **`commitment_due` (positive)** — a time-anchored commitment is
  inside its window. Act now if you haven't already.
- **`commitment_expired` (negative)** — `due_window_end` passed
  without resolution. The record is terminal-expired. This is a
  miss; understand why before extracting similar ones again.
- **`commitment_snooze_pileup` (negative)** — a record crossed the
  snooze threshold (default 3). Either act or dismiss; further
  snoozing without action is the problem the signal is flagging.

The block dedupes these to first-occurrence-only within the 24h
algedonic window, so you'll see each kind once per record per day.

## Operator parallels

The operator has the same surface via `mimir commitments
{list,add,complete,snooze,dismiss,trim}`. If you complete a record
the operator is also tracking, no coordination needed — the JSONL is
the single source of truth; the operator's CLI replays the same
events. Manual operator entries (no extraction prompt version)
appear in the prompt block the same as extracted ones.
