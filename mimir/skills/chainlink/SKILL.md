---
name: chainlink
description: Local CLI issue tracker for todos, follow-ups, and structured records. Use when the user mentions a task to remember, a bug to track, an open question, or anything that needs to outlive the current turn. Pairs with five-whys (chainlink is the storage backend for RCA trees) and works as a general-purpose backlog you can query later. Storage is local — issues live under a `.chainlink/` directory in the operator's repo.
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
