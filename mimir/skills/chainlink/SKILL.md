---
name: chainlink
description: Local CLI issue tracker for todos, follow-ups, structured records, and the canonical pattern for decomposing multi-step work into a parent + subissues with acceptance criteria, dependency edges, and priority. Use when the user mentions a task to remember, a bug to track, an open question, anything that needs to outlive the current turn, or when planning multi-heartbeat work that needs `chainlink issue ready`-driven pickup across sessions. Includes guidance on writing descriptions future-you can act on (handles, success path, failure path) and idempotency tactics for actions that might fire twice (boundary firing, overlapping heartbeats, retried subagent completion). Pairs with five-whys (chainlink is the storage backend for RCA trees). Storage is local — issues live under a `.chainlink/` directory in the operator's repo.
allowed-tools:
  - Bash
  - Read
  - saga_store
  - send_message
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
report that to the operator rather than guessing.

## Day-to-day patterns

### Create a todo

```bash
chainlink issue create \
  --label todo \
  --label "for-jason" \
  --priority medium \
  "Re-check the simhi multi-session regression after the sim_threshold tune"
# Returns: Created issue #42
```

`--label / -l` is repeatable. Pick labels that match an existing scheme — query
``chainlink label list`` first if you're not sure what's in use.

**Attach a description on creation** with `--description / -d`:

```bash
chainlink issue create "Add timeout to dispatch loop" \
  --description "Currently unbounded; need a 30s ceiling with retry." \
  --priority high \
  --label todo
```

For non-trivial descriptions (with `§`, parens, smart quotes, or multi-line),
use a heredoc — bare `--description "..."` breaks on shell-quoting (see
Failure modes below):

```bash
DESC=$(cat <<'EOF'
Multi-line description with §1.2 and (parens) and
"smart quotes" all preserved.
EOF
)
chainlink issue create "<title>" --description "$DESC" --priority medium --label todo
```

**Priority** is `low | medium | high | critical` (default: `medium`). Treat it
as load-bearing — filter on it later via `--priority high` to focus.

### Add notes / context

```bash
chainlink issue comment 42 "Initial run: 71% multi-session at 0.80 sim."
```

Comment takes a positional `<TEXT>` (no `--body` flag). For non-trivial bodies
with special characters, use the same heredoc pattern as `--description` above.

### List open work

```bash
# All open (default)
chainlink issue list

# Filter by status (flag is --status, NOT --state)
chainlink issue list --status all       # open + closed
chainlink issue list --status closed

# Filter by label / priority
chainlink issue list --label todo
chainlink issue list --priority high
chainlink issue list --label todo --label for-jason

# JSON for scripting
chainlink issue list --label todo --json
```

### Pick what to work on next

```bash
# Unblocked issues only — primary "what's actually actionable?" command
chainlink issue ready

# Tool suggests next from heuristics
chainlink issue next

# Text search across issues — useful before creating to avoid duplicates
chainlink issue search "dispatch timeout"
```

`ready` correctly filters out blocked items AND closed-issue blockers (an issue
unblocks once its blocker closes). Prefer `ready` over `list` when you're asking
"what should I pick up now?"

### Look at one issue

```bash
chainlink issue show 42         # human-readable, shows Blocked-by / Blocking edges
chainlink issue show 42 --json  # structured (for chaining with jq)

# Show the whole hierarchy (NOT a single issue's subtree — tree takes no positional ID)
chainlink issue tree
chainlink issue tree --status open
```

**Note**: `issue tree` does NOT accept a positional ID — it always shows the
whole hierarchy filtered by `--status` / `--quiet`. For a single issue's
children/relations, use `issue show <id>` (its Blocked-by / Blocking edges
show the immediate relationships).

`show` retains historical edges (an issue still lists a closed blocker in
its "Blocked by" output), while `ready` filters them — see Failure modes
for when this matters.

### Mark progress

```bash
chainlink issue label 42 in-progress
chainlink issue unlabel 42 todo
chainlink issue close 42        # finished
```

**Note**: `close` does NOT accept `--comment` or `-m` — if you want to leave a
closing note, run `chainlink issue comment 42 "..."` first, then `chainlink
issue close 42` separately.

### Subissues for breakdowns

When a todo is too big or has dependencies:

```bash
chainlink issue subissue 42 \
  --label todo \
  --description "Acceptance: alert fires on threshold cross + matching test." \
  "Sketch the alert flow"
chainlink issue subissue 42 \
  --label todo \
  --description "Acceptance: dispatch path delivers payload + ack." \
  "Wire it through to the dispatch path"
# Closing the parent doesn't auto-close children — close them as you go.
```

Each subissue should have a non-empty `--description` capturing acceptance
criteria — that's how a future session (or another agent) can pick up the
work without re-reading the parent. See "Subissue decomposition pattern"
below for the full workflow.

### Block / unblock relations

```bash
chainlink issue block 42 17     # 42 blocks 17 (don't work on 17 yet)
chainlink issue unblock 42 17
```

Once issue 42 closes, issue 17 automatically appears in `chainlink issue
ready`. `issue show 17` still lists 42 in its "Blocked by" output as
historical record — trust `ready` for "is this actionable?", trust
`show`/`tree` for "what was this once depending on?"

## When to create a chainlink issue

Create one when:
- The user mentioned something they want you to remember beyond the current
  conversation ("don't let me forget to call the dentist", "track down that
  flaky test").
- You discovered a bug or rough edge worth fixing later but not now.
- A reflection / heartbeat surfaced a follow-up that needs a real owner.
- A 5-Whys analysis produced an action item (use the `rca` + `action-item`
  labels — see `five-whys/CHAINLINK_USAGE.md`).
- A task is too big for one session and benefits from subissue decomposition
  (see pattern below).

Don't create one when:
- It's a within-turn working note → use the Plan tool or just hold it in
  context.
- It's a memory / preference about the user → store in saga via
  `saga_store`, not chainlink.
- The user is just venting and didn't ask you to track anything.

## Subissue decomposition pattern

The canonical shape for any "this is too big for one session" task — two-phase
work split between a planning session and one or more implementing sessions.

### Phase 1 — planning session

1. **Create parent** with a description summarizing the goal and overall
   acceptance criteria.
2. **Decompose** into subissues. Each subissue gets a non-empty
   `--description` with its own acceptance criteria — sized so a future
   session can pick it up without re-reading the parent.
3. **Add dependency edges** via `chainlink issue block <a> <b>` if `<a>`
   must complete before `<b>` is actionable.
4. **Mark parent in-progress** via `chainlink issue label <parent> in-progress`.

Acceptance for the planning session: parent has subissues, every subissue has
a non-empty description with acceptance criteria, dependency graph committed.
Half-decomposed parents are worse than not starting — if planning runs out
before decomposition finishes, comment on the parent and resume next session.

### Phase 2 — implementing sessions

```bash
chainlink issue ready                   # find unblocked subissues
chainlink issue show <subissue>         # read its acceptance criteria
# ... do the work ...
chainlink issue close <subissue>
```

Pick the highest-priority unblocked subissue. Close on completion. The next
session repeats `ready` to find what's actionable now.

### Phase 3 — closing the parent

Once all subissues are closed, do any integration work (tests across the
decomposition, docs, the final PR if applicable), then close the parent.

## Querying open work

End-of-session is a good time to surface what's open. Prefer `ready` for
actionable work; use `list` when you need the full picture including blocked
items:

```bash
# What's actionable RIGHT NOW (unblocked)?
chainlink issue ready

# What's currently labeled todo / for-jason?
chainlink issue list --label todo --label for-jason --json \
  | jq '.[] | "#\(.id): \(.title)"'

# Stale issues (no comment in N days) — useful for librarian sweeps
chainlink issue list --json | jq '.[] | select(.updated_at < "2026-04-04")'
```

If the operator asks "what should I work on?" or "what did we have open?",
``chainlink issue ready`` is the right starting point — not your memory,
and not `list` (which includes blocked items the operator can't actually
start on).

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
- **Descriptions**: every subissue gets one; non-trivial parents get one.
  Empty description = future session can't pick up the work.
- **Priority**: load-bearing. Default `medium`; reserve `high` for "blocks
  shippable work" and `critical` for "service-affecting now".
- **Don't close issues you didn't open** without an explicit operator OK —
  closing destroys the open-work signal.
- **Don't bulk-create**. Each chainlink issue should earn its keep. A pile
  of micro-issues is harder to navigate than five well-titled ones.

## Writing descriptions future-you can act on

Heartbeat picks up issues by reading descriptions cold. Prose ("look into
the rate-limit thing later") is unanchored — a future heartbeat reads it,
shrugs, and moves on. Descriptions with **handles** ("investigate why
`oauth_quota_anomalous` has fired 5×/day on
`discord-100000000000000002` since 2026-05-03") are actionable on first
read. This applies equally to `-d/--description` on `create` and to the
acceptance-criteria bullets on subissue descriptions (see "Subissue
decomposition pattern" above) — same four questions, same handles.

A good description answers four questions:

1. **What's the trigger, named with handles?** Specific event names,
   file paths, commit hashes, dollar thresholds, dedupe-key shapes —
   anything a future heartbeat can grep for. Not "saga thing went
   wrong" but "`saga_synthesis_skipped_boundary` fired 5×/24h on
   synthesis turns since PR #32 (2026-05-06)".
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
the issue. Without handles, the issue is unanchored and gets deferred
forever.

## Idempotency: design for the boundary firing twice

Chainlink issues can be picked up by overlapping heartbeats. Pollers
can emit duplicate events when a cursor wasn't saved before a crash.
Subagent completion notifications can arrive after a retry. Synthesis
turns can fire twice on the same session boundary. **Design actions so
they're safe to run twice.**

The wrong instinct is "I just got here, I should do the thing." The
right instinct is "I just got here, has the thing already been done?"

General tactics:

- **Tag durable artifacts with a unique key.** PR titles, commit
  messages, and chainlink issue titles are good unique keys — grep
  before acting.
- **Update cursors atomically and *after* the side effect.** Save the
  poller cursor only once the events have been emitted; never
  save-then-emit (a crash between would save the cursor but lose the
  event). See `pollers/SKILL.md` for the cursor pattern.
- **Use mkdir / O_CREAT-style claims for cross-process work.** If a
  marker file or directory exists, another instance is already
  working — back off rather than collide.
- **Treat `send_message` and similar with care.** The harness
  circuit-breaks near-duplicate sends within a turn — but design as
  if it won't; the breaker is a safety net, not a correctness
  guarantee.

### Mimir-specific idempotency surfaces

Several primitives have built-in idempotency you can lean on — plus a
couple that *look* idempotent but aren't:

- **`saga_store` is unique-key idempotent.** Same content twice → one
  atom (deduplicated at the atom layer). Synth turns firing twice on
  the same boundary don't create two atoms.
- **`applied_proposals.jsonl` is the proposal-replay guard.** Before
  re-applying a proposal from `state/proposed-changes.md`, check
  whether its id is already in `state/applied_proposals.jsonl` — if so,
  it landed already and re-applying is a foot-gun.
- **`chainlink issue create` is *append-only*, not idempotent.**
  Re-running `issue create "Same title"` produces a *new* issue with a
  fresh id every time. **Always `chainlink issue search "<title-or-handle>"`
  before creating** — this is the most common chainlink foot-gun.
- **`send_message` has harness-level dedup *within a turn* only.**
  Across turns the breaker doesn't help; rely on unique keys in the
  message itself if the same content might be generated twice.
- **`gh pr create` is *not* idempotent.** It happily makes a second
  PR against the same branch. Always `gh pr list --head <branch>`
  first.

## Failure modes

- ``No .chainlink directory found`` → wrong cwd; navigate to the operator's
  repo or initialize via ``chainlink init`` if explicitly authorized.
- ``chainlink: command not found`` → binary missing from PATH. Surface to
  operator; don't try to install yourself.
- ``unexpected argument '--state'`` (or similar `--state` / `--body` / `-m`
  rejection) → flag-name surprises vs gh-style intuition. The CLI is its
  own tool, not a gh clone. **Always check `chainlink <subcommand> --help`
  when a flag fails** — it's exhaustive. The mimir-tracked surprises list
  lives at `memory/issues/chainlink-cli-flag-surprises.md` (close has no
  `--comment` or `-m`; list uses `--status` not `--state`; comment takes
  positional `<TEXT>` not `--body`; create takes `--description / -d` not
  `--body-file`; tree takes no positional ID).
- Comment / description bodies break on `§`, parens, or smart quotes →
  shell-quoting bites. Use the heredoc-to-tmpfile pattern shown under
  "Create a todo" above. Reference: `memory/issues/chainlink-comment-shell-quoting.md`.
- `issue show` says "Blocked by #N" but #N is closed → that's historical
  record. Use `issue ready` to determine actionability; `show` retains
  edges for audit. Reference: `memory/issues/chainlink-ready-vs-show.md`.
- Subissue depth gets unwieldy past ~3 levels — flatten or split into
  separate trees if you find yourself going deeper.
