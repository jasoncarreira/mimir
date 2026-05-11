---
name: chainlink
description: Local CLI issue tracker for todos, follow-ups, and structured records. Use when the user mentions a task to remember, a bug to track, an open question, or anything that needs to outlive the current turn. Pairs with five-whys (chainlink is the storage backend for RCA trees) and works as a general-purpose backlog you can query later. Storage is local — issues live under a `.chainlink/` directory in the operator's repo.
allowed-tools:
  - Bash
  - Read
---

# Chainlink

`chainlink` is a CLI issue tracker (Rust binary). Each repo / workspace has a
`.chainlink/` directory holding issues with id, title, labels, parent/child
relations, and comments. Everything is local — no GitHub, no server.

The five-whys skill uses chainlink as its storage backend (see
`five-whys/CHAINLINK_USAGE.md` for the RCA-specific commands). This skill is
the general-purpose CLI reference for everything else: todos, bug tracking,
follow-ups, parking-lot items, anything that needs to persist between turns.

## Where to operate

Chainlink commands need a `.chainlink/` directory in the cwd or an ancestor.
The operator's home repo or a working directory will have one. If you get
``No .chainlink directory found``, you're in the wrong place — check the
operator's repo root for `.chainlink/`, or ask before initializing one.

To initialize a fresh tracker (only when the operator says so):

```bash
chainlink init
```

## Verifying availability

```bash
chainlink --version
```

If the command isn't found, the binary isn't installed in this environment —
report that to the operator rather than guessing. (The mimirbot Dockerfile
should install it; if it didn't, that's a separate fix.)

## Day-to-day patterns

### Create a todo

```bash
chainlink issue create \
  --label todo \
  --label "for-jason" \
  "Re-check the simhi multi-session regression after the sim_threshold tune"
# Returns: Created issue #42
```

`--label` is repeatable. Pick labels that match an existing scheme — query
``chainlink label list`` first if you're not sure what's in use.

### Add notes / context

```bash
chainlink issue comment 42 "Initial run: 71% multi-session at 0.80 sim. \
Worth re-running once we have the contradiction-resolution change in."
```

### List open work

```bash
# All open
chainlink issue list

# Filter by label
chainlink issue list --label todo
chainlink issue list --label for-jason

# Closed too (default is open)
chainlink issue list --state all

# JSON for scripting
chainlink issue list --label todo --json
```

### Look at one issue

```bash
chainlink issue show 42         # human-readable
chainlink issue show 42 --json  # structured (for chaining with jq)
chainlink issue tree 42         # show subissues + relations
```

### Mark progress

```bash
chainlink issue label 42 in-progress
chainlink issue unlabel 42 todo
chainlink issue close 42        # finished
```

### Subissues for breakdowns

When a todo is too big or has dependencies:

```bash
chainlink issue subissue 42 --label todo "Sketch the alert flow"
chainlink issue subissue 42 --label todo "Wire it through to the dispatch path"
# Closing the parent doesn't auto-close children — close them as you go.
```

### Block / unblock relations

```bash
chainlink issue block 42 17     # 42 blocks 17 (don't work on 17 yet)
chainlink issue unblock 42 17
```

## When to create a chainlink issue

Create one when:
- The user mentioned something they want you to remember beyond the current
  conversation ("don't let me forget to call the dentist", "track down that
  flaky test").
- You discovered a bug or rough edge worth fixing later but not now.
- A reflection / heartbeat surfaced a follow-up that needs a real owner.
- A 5-Whys analysis produced an action item (use the `rca` + `action-item`
  labels — see `five-whys/CHAINLINK_USAGE.md`).

Don't create one when:
- It's a within-turn working note → use the Plan tool or just hold it in
  context.
- It's a memory / preference about the user → store in saga via
  `saga_store`, not chainlink.
- The user is just venting and didn't ask you to track anything.

## Writing interest items future-you will actually use

Heartbeat picks up interest items by reading their descriptions cold. An
item written as prose ("look into the rate-limit thing later") is
unanchored: a future heartbeat reads it, shrugs, and moves on. An item
with **handles** ("investigate why `oauth_quota_anomalous` has fired
5×/day on `discord-1500672382166110321` since 2026-05-03") is actionable
on first read.

A good interest item answers four questions:

1. **What's the trigger, named with handles?** Not "saga thing went
   wrong" — "`saga_synthesis_skipped_boundary` fired 5×/24h on synthesis
   turns since PR #32 (2026-05-06)." Specific event names, file paths,
   commit hashes, dollar thresholds, dedupe-key shapes. Anything a
   future heartbeat can grep for.
2. **Why does this matter?** The reason it crossed the bar from noise
   to signal. "Atoms aren't landing → silent memory loss" is a reason;
   "felt off" is not.
3. **What's the success path?** The concrete next step if the
   investigation finds something. "If the cause is the new dedup
   window, the fix is bumping the floor in `_pre_message_hook`."
4. **What's the failure path?** Likely modes if the cause isn't what
   you expect. "If it's not the dedup window, check the consolidator —
   PR #32 also touched `_partition_turns`."

The handles matter most. With handles, future-you can grep
`events.jsonl`, query saga, or `git log` and find every record tied to
the item. Without handles, the item is unanchored and gets deferred
forever.

## Idempotency: design for the boundary firing twice

Chainlink interest items can be picked up by overlapping heartbeats.
Pollers can emit duplicate events when a cursor wasn't saved before a
crash. Subagent completion notifications can arrive after a retry.
Synthesis turns can fire twice on the same session boundary. **Design
actions so they're safe to run twice.**

The agent's instinct is "I just got here, I should do the thing." The
right instinct is: "I just got here, has the thing already been done?"

Concrete tactics:

- **Tag durable artifacts with a unique key.** "Posted draft #abc123
  to channel" — if you wake up and see #abc123 already exists, skip
  rather than duplicate. PR titles, commit messages, and chainlink
  issue titles are all good unique keys you can grep for before
  acting.
- **Update cursors atomically and *after* the side effect.** Save
  the poller cursor only once the events have been emitted; never
  save-then-emit (a crash between saves the cursor but loses the
  event). See `pollers/SKILL.md` for the cursor pattern.
- **Use mkdir / O_CREAT-style claims for cross-process work.** If a
  marker file or directory exists, another instance is already
  working — back off rather than collide.
- **Treat send_message and similar with care.** A duplicate message
  annoys the operator. The harness already circuit-breaks
  near-duplicate sends within a turn — but design as if it won't.
  The breaker is a safety net, not a correctness guarantee.

### Mimir-specific idempotency surfaces

Several mimir primitives have built-in idempotency you can lean on,
plus a couple that look idempotent but aren't:

- **Saga atom IDs are unique-key idempotency for free.** `saga_store`
  returns an `atom_id`; if the same content lands twice, saga
  deduplicates at the atom layer. Synth turns firing twice on the
  same boundary don't create two atoms.
- **`applied_proposals.jsonl` is the proposal-replay guard.** Before
  re-applying a proposal from `state/proposed-changes.md`, check
  whether its id is already in `state/applied_proposals.jsonl` — if
  so, it landed already and re-applying is a foot-gun.
- **`chainlink issue create` is *append-only*, not idempotent.**
  Re-running `issue create "Same title"` produces a *new* issue with
  a fresh id every time. Before creating an interest item, query
  for an existing issue with the same title or a distinctive label
  (`chainlink issue list --label interest --json | jq ...`). This
  is the most common chainlink foot-gun.
- **`send_message` has harness-level dedup within a turn.** The
  dispatcher circuit-breaks near-duplicate sends — but only within
  the same turn. Across turns, the breaker doesn't help; rely on
  unique keys in the message itself if the same content might be
  generated twice.
- **PR creation is *not* idempotent.** `gh pr create` against the
  same branch happily makes a second PR. Always `gh pr list --head
  <branch>` first.

## Querying open work

End-of-heartbeat or end-of-session is a good time to surface what's open:

```bash
# What's currently labeled todo / for-jason?
chainlink issue list --label todo --label for-jason --json \
  | jq '.[] | "#\(.id): \(.title)"'

# Stale issues (no comment in N days) — useful for the heartbeat skill's
# librarian protocol.
chainlink issue list --json | jq '.[] | select(.updated_at < "2026-04-04")'
```

If the operator asks "what should I work on?" or "what did we have open?",
``chainlink issue list`` + a quick filter is the answer — not your memory.

## Five-Whys integration

Five-whys reuses chainlink for tree storage. The labels are:

| Label | Meaning |
|---|---|
| `rca` | Part of a root cause analysis |
| `5-whys` | Root issue of a 5 Whys tree |
| `bedrock` | Leaf node — root cause found |
| `action-item` | Concrete fix derived from bedrock |
| `external-boundary` | Root cause outside your control |
| `accepted-tradeoff` | Known intentional choice — not a bug |

See `five-whys/CHAINLINK_USAGE.md` for the full tree-construction flow,
falsification cascades, and example.

## Conventions

- **Labels**: prefer existing labels over inventing new ones. ``chainlink
  label list`` shows what's in use.
- **Titles**: imperative for todos ("Add timeout to dispatch loop"), past-
  tense / observational for records ("Discord typing-indicator regression
  fixed in commit 2ea9863").
- **Don't close issues you didn't open** without an explicit operator OK —
  closing destroys the open-work signal.
- **Don't bulk-create**. Each chainlink issue should earn its keep. A pile
  of micro-issues is harder to navigate than five well-titled ones.

## Failure modes

- ``No .chainlink directory found`` → wrong cwd; navigate to the operator's
  repo or initialize via ``chainlink init`` if explicitly authorized.
- ``chainlink: command not found`` → binary missing from PATH. Surface to
  operator; don't try to install yourself.
- Subissue depth gets unwieldy past ~3 levels — flatten or split into
  separate trees if you find yourself going deeper.
