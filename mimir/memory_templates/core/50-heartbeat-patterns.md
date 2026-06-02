<!-- desc: what works (and what doesn't) during heartbeat ticks -->
# Heartbeat Patterns

Append observations from your heartbeat experience — tasks that
fit well, ones that didn't, time-of-day patterns, mistakes worth
not repeating. Keep it tight; this block is in core memory.

## Multi-item ticks (when one finishes fast)

Default is "pick ONE item per tick" — but that produces an
artificial ceiling on ticks where the picked item happened to be
tightly bounded (a quick audit, a one-edit doc reconciliation).
The prompt cost is sunk regardless; exiting early wastes capacity.

Relaxation: **when the first item finishes in <10 min and the
next ready item is a natural successor, pick a second item rather
than exiting.** Cap at 2 items per tick; cap at 30 min wall-clock
so the next tick doesn't get behind.

"Natural successor" examples:
- next subissue in the same chainlink chain (when it's unblocked
  and bounded)
- another single-edit backlog item from
  `state/heartbeat-backlog.md`
- a propose-only draft that pairs with the just-completed work

If the first item produced something that needs operator review
before its natural successor can run, **do not exit** — surface
and pivot. See §"Surfacing operator-attention items" and
§"Reading backlog as the operator-gated fallback" below.

## Surfacing operator-attention items

When heartbeat work reaches a point that needs operator attention
— a draft requiring approval, a per-file migration list whose
decisions load-bear, an overlap-pair resolution, a propose-only
doc whose recommendations gate the next phase — **send a message
to the operator channel before exiting or pivoting.** Heartbeats
are silent by default but operator-gates are exactly the
surface-it case; silently exiting leaves the operator to discover
the gate next time they think to look.

Message shape (tight, no preamble, no decoration):
- One line: what was completed + path to the artifact.
- One line: the load-bearing decision(s) the operator should
  sanity-check. Don't dump the whole doc — point at the section.
- One line: what the heartbeat is pivoting to (or "exiting,
  reading-backlog empty for this slot").

Channel: route through the operator alert channel (when
configured) or the active operator chat channel.

## Reading backlog as the operator-gated fallback

When current chainlink work hits an operator-gate, the reading
backlog is the canonical pivot — **not exiting**. Reading work
is the right shape because:
- **Ungated** — no decisions required to start.
- **Bounded** — one source per tick (or one chunk if the source
  is too big), 30-min wall-clock cap holds.
- **Non-output-producing in a decision sense** — synthesis lands
  as wiki pages which don't ask the operator for anything.

Curate reading-backlog items in `state/heartbeat-backlog.md`
under a clearly-marked "Reading backlog" section.

Pivot precedence when current work is operator-gated:
1. Reading-backlog item (in priority order).
2. Librarian / propose-only audit (no-decision-output work).
3. Exit silently if neither is available — but log this in the
   heartbeat result so the operator notices.

Wall-clock and cost caps apply to the pivoted-to item the same
way they apply to the primary item; the 30-min cap is for the
whole tick, not per-item.
