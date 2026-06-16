# mimir

[![PyPI](https://img.shields.io/pypi/v/mimir-agent.svg)](https://pypi.org/project/mimir-agent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A memory-centric agent harness built on [deepagents](https://github.com/langchain-ai/deepagents)
(LangGraph). Install: `pip install mimir-agent`.

mimir wraps an LLM agent loop with the surrounding apparatus a long-running
agent needs to operate over time, across channels, and across sessions:
persistent memory (the in-process `mimir.saga` backend), a tool-and-skill registry, scheduled
ticks for autonomous work, message bridges (Discord / Slack / web /
benchmark stdout), and a feedback-loop / homeostat layer that keeps the
agent regulated as it accumulates state.

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
- **Multi-channel bridges.** Discord, Slack, web chat, and
  benchmark stdout. The agent has one identity across channels;
  `state/identities.yaml` resolves platform aliases to canonical
  names. (Social posting — e.g. Bluesky — is the `social-cli`
  optional skill, not a bridge.)
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
mimir/                              # the agent harness — top-level package
mimir/saga/                         # in-process memory backend (runtime)
benchmarks/longmemeval_via_mimir/   # integration bench against LongMemEval
benchmarks/saga/                    # bench shell — separate workspace package, imported by the longmemeval runners
tests/                              # pytest suite
docs/                               # architectural notes (public) + internal/ (process docs)
SPEC.md                             # detailed design doc
FEEDBACK-LOOPS.md                   # mapping of every feedback loop in the system
```

The runtime memory backend lives at `mimir/saga/` and is part of the
`mimir-agent` package. The `saga` workspace package at `benchmarks/saga/`
is a separate bench shell that the LongMemEval runners under
`benchmarks/longmemeval_via_mimir/` import as
`saga.benchmarks.longmemeval.*`.

## Quickstart

Requires Python 3.11+.

### Install from PyPI

```bash
pip install "mimir-agent[anthropic]"   # pick the model-provider extra(s) you'll use

# Set up an agent home (creates dirs, seeds skills, generates API keys)
mimir setup --home ~/mimir-home

# Configure auth — pick one
#   API key:                     edit ~/mimir-home/.env, set ANTHROPIC_API_KEY
#   Anthropic Max plan (free):   one extra install step — see "Claude Max plan" below
#   Gateway:                     set ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
#   Non-Anthropic Anthropic-compat (Minimax, Kimi, …): see "Alternative providers"

# Optional but recommended for saga's embeddings:
#   set OPENAI_API_KEY in ~/mimir-home/.env

# Run
mimir run --home ~/mimir-home
```

Available extras (combine in one install command — e.g. `pip install
"mimir-agent[anthropic,discord,slack,mcp]"`):

| Extra | Pulls |
|---|---|
| `anthropic`, `openai`, `codex-plus` | model adapter packages (`codex-plus` = ChatGPT Plus / Pro Codex subscription via the OAuth-backed gateway) |
| `discord`, `slack` | bridge runtimes |
| `mcp` | Model Context Protocol client |

For **Claude Max** (the subprocess provider via `langchain-claude-code`),
that package is currently a git-pinned fork — PyPI rejects published
packages that declare direct `git+` URLs, so it isn't an extra. Install
the fork as a separate step after `mimir-agent`:

```bash
pip install mimir-agent[anthropic]
pip install "langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@c03f075c8b84fb0c718de1aabdd6493f5d191786"
claude setup-token   # OAuth dance — once per host
```

This works around upstream PRs (#2 / #4 / #6) that haven't merged yet.
Once they land + a release is cut, the second `pip install` collapses
back into a `[claude-code]` extra. Tracked in mimir-repo issue #268.

### Or clone for development

```bash
git clone https://github.com/jasoncarreira/mimir.git
cd mimir
uv sync --extra dev
# For the Claude Code subprocess path, also:
# uv pip install "langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@c03f075c8b84fb0c718de1aabdd6493f5d191786"

uv run mimir setup --home ~/mimir-home
uv run mimir run --home ~/mimir-home
```

`mimir setup` activates four recurring tasks out of the box: hourly
heartbeat, weekly reflection, weekly saga consolidation, weekly
behavioral introspection report. All gated by the homeostat so a
saturated plan window doesn't blow through your quota.

See `.env.example` for every environment variable mimir reads.

## Web UI

Once running, mimir serves an operator web UI on `MIMIR_WEB_PORT` (default port
`8080`). There's no root landing page — start at a page route such as
`http://localhost:8080/turns`; the page prompts for `MIMIR_API_KEY` on first
visit and remembers it:

- **`/turns` — turn viewer.** A live, auto-refreshing feed of every turn: the
  inbound trigger, the tools the agent ran, and what it said back. The first
  place to watch the agent work or debug a turn.
- **`/ops` — ops dashboard.** Live health + usage: token/cost rate, plan-window
  headroom, scheduled-tick activity, recent errors, and pending `mimir-agent`
  updates.
- **`/saga` — memory viewer.** Browse saga's memory atoms.
- **`/state` — file browser.** Browse `memory/` and `state/`.

Each HTML page has a JSON twin (`/api/turns`, `/api/ops`, `/api/saga`,
`/api/memory`) for scripting. The data/API routes are auth-gated by
`MIMIR_API_KEY` (the HTML shells and `/health` are exempt so the JS can load
and prompt for the key); expose the port publicly only with `MIMIR_API_KEY` set.

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
`skills/*/pollers.json`. Invalid zone names fall back to UTC
with a logged warning rather than crashing the scheduler.

## Development

Optional extras:

| Extra | Pulls | When to use |
|---|---|---|
| `[dev]` | pytest + bridges + Anthropic / OpenAI / Codex Plus adapters + faiss | Default for contributors — covers the agent core, saga, bridges. Claude-code-specific tests skip cleanly via `pytest.importorskip`. |
| `[anthropic]` / `[openai]` / `[codex-plus]` | Single model adapter | Runtime install with one model path. |

Developers on the Claude Code subprocess path install the
git-pinned fork as a separate step (same reasoning as the runtime
Claude Max path above):

```bash
uv sync --extra dev
uv pip install "langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@c03f075c8b84fb0c718de1aabdd6493f5d191786"
```

```bash
# Tests — minimal toolchain (claude-code-specific tests will skip)
uv pip install -e ".[dev]"
uv run pytest                                       # 600+ tests
uv run pytest --ignore=tests/test_bench_via_mimir.py  # skip the slow integration test

# Tests — full toolchain (claude-code-specific tests will run)
uv pip install -e ".[dev]"
uv pip install "langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@c03f075c8b84fb0c718de1aabdd6493f5d191786"
uv run pytest
```

The bench harness is in `benchmarks/longmemeval_via_mimir/`. See
that directory's README for running an A/B of two saga configs and
scoring with the gpt-4o judge.

## Reading order

If you're orienting yourself in the codebase:

1. **[SPEC.md](./SPEC.md)** — what mimir is, the design choices
2. **[FEEDBACK-LOOPS.md](./FEEDBACK-LOOPS.md)** — the regulatory
   architecture (mapped to Beer's Viable System Model)
3. **[mimir/saga/\_\_init\_\_.py](./mimir/saga/__init__.py)** — memory
   backend operation surface (the module docstring is the public-API
   reference)
4. **[docs/](./docs/)** — additional architectural notes (`docs/internal/`
   holds historical process docs that may help when archeology is needed
   but aren't part of the public contract)

## License

MIT — see [LICENSE](./LICENSE). Copyright © 2026 Jason Carreira.

## Contributing & security

- [CONTRIBUTING.md](./CONTRIBUTING.md) — how to file issues and PRs
- [SECURITY.md](./SECURITY.md) — vulnerability disclosure + threat-model posture
- [CHANGELOG.md](./CHANGELOG.md) — release notes
