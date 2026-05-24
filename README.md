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
  five-whys debugging skill, the reflection skill, etc.) — decision-
  framework and failure-mode docs that the tool description alone
  can't carry.
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
#   Non-Anthropic Anthropic-compat (Minimax, Kimi, …): see "Alternative providers"

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

## Alternative providers (Minimax, Kimi, …)

`MIMIR_MODEL_SPEC` picks the model and provider. Forms:

- `claude-code:<model>` — Max OAuth subprocess (default, free under Max plan).
- `anthropic:<model>` — direct Anthropic API (paid credit).
- `openai:<model>` — direct OpenAI.

Reasoning-token model families that expose an **Anthropic-compat
endpoint** (Minimax, Moonshot Kimi) ride the `anthropic:` provider with
`ANTHROPIC_BASE_URL` overridden:

```bash
# Minimax via Anthropic-compat
ANTHROPIC_API_KEY=<minimax-key>
ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic
MIMIR_MODEL_SPEC=anthropic:MiniMax-M2.7
```

```bash
# Moonshot Kimi via Anthropic-compat
ANTHROPIC_API_KEY=<moonshot-key>
ANTHROPIC_BASE_URL=https://api.moonshot.ai/anthropic
MIMIR_MODEL_SPEC=anthropic:kimi-k2-0905-preview
```

Prefer Anthropic-compat over OpenAI-compat for these providers when both
are offered: the provider converts reasoning to proper Anthropic-shape
`thinking` content blocks server-side. The OAI-compat path returns the
same model's reasoning as inline `<think>...</think>` tags in the
content string — a less structured response that mimir would have to
parse out before it could be cleanly logged + suppressed.

Note: overriding `ANTHROPIC_BASE_URL` also affects any other consumer
in the same process (e.g., the `claude` CLI subprocess that saga's
`claude_code` provider spawns). If you set this, configure saga.toml's
`[llm]` to route through the same alternate provider rather than
falling back to `claude_code` — see
[`saga/saga.example.toml`](./saga/saga.example.toml) for the
provider options + per-section documentation.

## Scheduler timezone

`scheduler.yaml` cron expressions are interpreted in UTC by default —
e.g., `cron: "0 8 * * *"` means 08:00 UTC, not 08:00 in your local
time. Set `MIMIR_SCHEDULER_TZ` to a IANA zone to author crons in
local wall-clock time (DST-aware via system tzdata):

```bash
MIMIR_SCHEDULER_TZ=America/New_York   # ET-shaped crons
```

Affects every cron in the agent home: `scheduler.yaml` LLM-tick jobs,
auto-installed `saga-consolidate` and `introspection-report`,
commitments-due-check, and every poller from
`.claude/skills/*/pollers.json`. Invalid zone names fall back to UTC
with a logged warning rather than crashing the scheduler.

## Development

Optional extras:

| Extra | Pulls | When to use |
|---|---|---|
| `[dev]` | pytest + bridges + Anthropic / OpenAI / Codex Plus adapters + faiss | Default for contributors — covers the agent core, saga, bridges. |
| `[dev-claude-code]` | `[dev]` + the `langchain-claude-code` git fork | Working on the Claude Code subprocess path specifically. The fork is a private SHA pin (upstream patches haven't merged); the bare `[dev]` install skips claude-code-specific tests. |
| `[claude-code]` | Just the fork (no test toolchain) | Runtime install for users on Claude Max who don't intend to develop. |
| `[anthropic]` / `[openai]` / `[codex-plus]` | Single model adapter | Runtime install with one model path. |

```bash
# Tests — minimal toolchain (claude-code-specific tests will skip)
uv pip install -e ".[dev]"
uv run pytest                                       # 600+ tests
uv run pytest --ignore=tests/test_bench_via_mimir.py  # skip the slow integration test

# Tests — full toolchain (claude-code-specific tests will run)
uv pip install -e ".[dev-claude-code]"
uv run pytest

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
