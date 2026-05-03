# mimir

A memory-centric agent harness on the Claude Agent SDK.

mimir wraps the SDK with the surrounding apparatus an agent needs to
operate over time, across channels, and across sessions: persistent
memory (via [saga](./saga)), a tool-and-skill registry, scheduled
ticks for autonomous work, message bridges (Discord / Slack / Bluesky
/ web / benchmark stdout), and a feedback-loop / homeostat layer that
keeps the agent regulated as it accumulates state.

The name is from Norse myth — Mímir, the keeper of memory and counsel.

## What it gives you

- **A real memory backend.** Every turn is recorded; significant
  observations consolidate into structured atoms with embedding +
  triple representations; retrieval at the start of each turn pulls
  relevant prior context into the prompt automatically.
- **Skills, not just tools.** Skills are markdown files an agent
  loads on demand to learn a workflow (the librarian protocol, the
  five-whys debugging skill, the reflection skill, etc.). The
  `mountaineering` skill turns the agent into a hill-climbing
  optimizer for any task with a `score.sh`.
- **Scheduled work.** Cron-backed scheduler fires per-channel ticks
  (heartbeat, reflection, custom). The §12.4 homeostat suppresses
  ticks when the plan window saturates or cost-rate trips.
- **Multi-channel bridges.** Discord, Slack, Bluesky, web chat,
  benchmark stdout. The agent has one identity across channels;
  `state/identities.yaml` resolves platform aliases to canonical
  names.
- **Reflection + double-loop learning.** Weekly reflection skill
  audits behavior + memory architecture, drafts proposals into
  `state/proposed-changes.md`, and (via the §12.2 applied-proposals
  audit) closes the loop on whether merged changes actually helped.
- **Predictions and calibration.** Agent writes structured
  predictions about future outcomes; CLI tracks them; weekly review
  compares predicted vs measured. Single source of operational
  calibration data.

## Repository layout

```
mimir/                      # the agent harness — top-level package
saga/                       # memory backend (vendored, formerly MSAM)
benchmarks/longmemeval_via_mimir/   # integration bench against LongMemEval
tests/                      # pytest suite
docs/                       # architectural notes
SPEC.md                     # detailed design doc
FUTURE_WORK.md              # roadmap + experiments
FEEDBACK-LOOPS.md           # mapping of every feedback loop in the system
V0.4.md, V0.5.md            # version specs (historical)
```

`saga/` is a memory-system package this repo depends on heavily —
see [saga/README.md](./saga/README.md). Originally a fork of MSAM by
Jaden Schwab, now extensively modified.

## Quickstart

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
# Clone + install
git clone <repo-url> mimir
cd mimir
uv sync

# Set up an agent home (creates dirs, seeds skills, generates API keys)
uv run mimir setup --home ~/mimir-home

# Configure auth — pick one
#   Anthropic Max plan (free):   claude setup-token
#   API key:                     edit ~/mimir-home/.env, set ANTHROPIC_API_KEY
#   Gateway:                     set ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN

# Optional but recommended for saga's embeddings:
#   set OPENAI_API_KEY in ~/mimir-home/.env

# Run
uv run mimir run --home ~/mimir-home
```

`mimir setup` activates four recurring tasks out of the box: hourly
heartbeat, weekly reflection, weekly saga consolidation, weekly
behavioral introspection report. All gated by the homeostat so a
saturated plan window doesn't blow through your quota.

See `.env.example` for every environment variable mimir reads.

## Development

```bash
# Tests
uv run pytest                                       # 600+ tests
uv run pytest --ignore=tests/test_bench_via_mimir.py  # skip the slow integration test

# Saga's own tests
cd saga && uv run pytest saga/tests/
```

The bench harness is in `benchmarks/longmemeval_via_mimir/`. See
that directory's README for running an A/B of two saga configs and
scoring with the gpt-4o judge.

## Reading order

If you're orienting yourself in the codebase:

1. **[SPEC.md](./SPEC.md)** — what mimir is, the design choices
2. **[FEEDBACK-LOOPS.md](./FEEDBACK-LOOPS.md)** — the regulatory
   architecture (mapped to Beer's Viable System Model)
3. **[saga/README.md](./saga/README.md)** — memory backend
4. **[FUTURE_WORK.md](./FUTURE_WORK.md)** — roadmap, including the
   §12 series on autonomous-iteration loops
5. **[V0.5.md](./V0.5.md)** — most recent shipped version's spec
   (V0.4.md for the prior one)

## License

Copyright © 2026 Jason Carreira. License terms TBD — see
[LICENSE](./LICENSE) for the current posture (all rights reserved
pending a real open-source license decision).

The [saga](./saga) subdirectory is independently MIT-licensed —
see [saga/LICENSE](./saga/LICENSE) (combined copyright Jaden Schwab
+ Jason Carreira). Saga's MIT terms are unaffected by mimir's
top-level posture.
