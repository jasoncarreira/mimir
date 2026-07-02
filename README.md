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
  audits behavior + memory architecture, opens protected-surface
  proposal PRs for core/prompt changes, and uses Chainlink or state/spec
  notes for non-protected follow-ups. The legacy §12.2 applied-proposals
  audit still covers historical `state/proposed-changes.md` entries.
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

Requires Python 3.11+. mimir also shells out to a few host tools — install at
least **`ripgrep`** (the file-search tool's backend), plus `git`/`jq`, and
`poppler-utils`/`tesseract-ocr` if you ingest PDFs. The Docker image bundles
these; off-Docker see
[`docs/mimir-nondocker-guide.md`](./docs/mimir-nondocker-guide.md) for the full
list and per-OS install commands.

### Install from PyPI

```bash
pip install "mimir-agent[anthropic]"   # pick the model-provider extra(s) you'll use

# Set up an agent home (creates dirs, seeds skills, generates API keys)
mimir setup --home ~/mimir-home

# Configure auth — pick one
#   API key:                     edit ~/mimir-home/.env, set ANTHROPIC_API_KEY
#   Anthropic Max plan:          install [claude-code] + Claude Code CLI; see below
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
| `anthropic`, `claude-code`, `openai`, `codex-plus` | model adapter packages (`claude-code` = Claude Max OAuth subprocess; `codex-plus` = ChatGPT Plus / Pro Codex subscription via the OAuth-backed gateway) |
| `discord`, `slack` | bridge runtimes |
| `mcp` | Model Context Protocol client |

For **Claude Max** (the subprocess provider via Claude Code), install the
normal `claude-code` extra (which pulls `langchain-claude-code-mimir>=0.1.2,<0.2`) plus the Claude Code CLI:

```bash
pip install "mimir-agent[claude-code]"
npm install -g @anthropic-ai/claude-code
claude setup-token   # or: claude login
claude --version && claude -p 'ping'
```

Do not paste Claude tokens or `~/.claude` files into chat, logs, or issues.

### Or clone for development

```bash
git clone https://github.com/jasoncarreira/mimir.git
cd mimir
uv sync --extra dev
# For the Claude Code subprocess path, use:
# uv sync --extra dev --extra claude-code

uv run mimir setup --home ~/mimir-home
uv run mimir run --home ~/mimir-home
```

`mimir setup` activates four recurring tasks out of the box: hourly
heartbeat, weekly reflection, weekly saga consolidation, weekly
behavioral introspection report. All gated by the homeostat so a
saturated plan window doesn't blow through your quota.

**First contact — onboarding.** On a brand-new home, `mimir setup` seeds an
`init` block into core memory that points the agent at its **onboarding**
skill. So you don't configure the agent by hand — just start talking to it
(message it on whatever bridge you've enabled), and it runs onboarding:
conversational setup that writes its own persona, communication, and schedule
blocks from what it learns. When onboarding is done the agent deletes the
`init` block, and it's never re-seeded — so it won't re-trigger on later
`setup` runs.

See `.env.example` for every environment variable mimir reads.

## Web UI

Once running, mimir serves an operator web UI on `MIMIR_WEB_PORT` (default port
`8080`). The documented default frontend is the React app at
`http://localhost:8080/app`; the bare root redirects there. The app prompts for
`MIMIR_API_KEY` on first visit and remembers it:

- **`/app/chat` — chat.** Send local web-chat messages and watch streamed
  replies/reactions.
- **`/app/turns` — turn viewer.** A live, auto-refreshing feed of every turn: the
  inbound trigger, the tools the agent ran, and what it said back. The first
  place to watch the agent work or debug a turn.
- **`/app/ops` — ops dashboard.** Live health + usage: token/cost rate, plan-window
  headroom, scheduled-tick activity, recent errors, and pending `mimir-agent`
  updates.
- **`/app/saga` — memory viewer.** Browse saga's memory atoms.
- **`/app/memory` — state/memory browser.** Browse `memory/` and `state/`.
- **`/app/admin` — admin/config.** Inspect model/config/env state with secrets
  redacted.

Legacy vanilla HTML routes (`/turns`, `/ops`, `/saga`, `/state`) remain
available while parity is verified. They return `X-Mimir-Frontend: legacy-html`
and link to `/app`; treat them as compatibility routes, not the default UI.

React uses the same JSON/API routes (`/api/v1/turns`, `/api/v1/ops`,
`/api/v1/saga`, `/api/v1/memory`, `/api/v1/web/bootstrap`) plus the web-chat
bridge. API routes are auth-gated by `MIMIR_API_KEY` (the React shell, retained
legacy HTML shells, bootstrap/auth helpers, and `/health` are exempt so browser
code can load and prompt for the key); expose the port publicly only with
`MIMIR_API_KEY` set.

## File-tool access outside the home

By default the agent's file tools (`read_file`/`ls`/`glob`/`edit_file`) are
confined to `MIMIR_HOME`. To let them read/edit a repo **outside** the home — a
source checkout the agent develops, a work codebase — set
`MIMIR_FILE_TOOL_ROOTS` to a comma-separated list of `path[:ro|:rw]` entries
(bare `path` = `rw`):

```bash
MIMIR_FILE_TOOL_ROOTS="/home/me/code/myrepo:rw,/srv/reference:ro"
```

`/tmp` is always granted `rw`. Roots must be absolute existing directories; `~`,
`..`, `/`, `/etc`, and anything overlapping the home are rejected. A real file in
no configured root now returns an actionable error instead of a silent "not
found". **In Docker, also bind-mount the path into the container and point the
variable at its in-container path** (the container can't reach host paths that
aren't mounted). Full details + a compose example:
[`docs/mimir-nondocker-guide.md` §4](./docs/mimir-nondocker-guide.md#4-file-tool-access-outside-the-home-mimir_file_tool_roots).

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
[`benchmarks/saga/saga.example.toml`](./benchmarks/saga/saga.example.toml) for the
provider options + per-section documentation.

## Memory diagnostics

`mimir memory doctor` is a read-only health report for Mimir's memory
surfaces: core/channel/issue memory, `learnings-pending.md`, memory and
wiki indexes, SAGA substrate checks, and state/wiki drift.

```bash
mimir memory doctor --home /mimir-home
mimir memory doctor --home /mimir-home --json
```

It reports `ok` / `warning` / `error` status, exits nonzero only for
`error`, and never auto-fixes or rewrites memory. See
[`docs/memory-doctor.md`](./docs/memory-doctor.md) for the full command
contract and automation guidance.

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
| `[dev]` | pytest + bridges + Anthropic / OpenAI / Codex Plus adapters + faiss | Default for contributors — covers the agent core, saga, bridges. Claude Code adapter import coverage runs in CI with `[claude-code]`; `[dev]` keeps the default contributor graph lean. |
| `[anthropic]` / `[claude-code]` / `[openai]` / `[codex-plus]` | Single model adapter | Runtime install with one model path. |

Developers on the Claude Code subprocess path add the adapter extra and
install/authenticate the CLI once per host:

```bash
uv sync --extra dev --extra claude-code
npm install -g @anthropic-ai/claude-code
claude setup-token
```

```bash
# Tests — minimal toolchain
uv pip install -e ".[dev]"
uv run pytest                                       # 600+ tests
uv run pytest --ignore=tests/test_bench_via_mimir.py  # skip the slow integration test

# Tests — full toolchain, including the Claude Code adapter import smoke
uv pip install -e ".[dev]"
uv pip install -e ".[claude-code]"
uv run pytest
```

### React frontend

The React app lives under `frontend/` and is served by aiohttp at `/app`.
Production builds write to `mimir/react_app/dist`, which is included in package
data when a release artifact is built. Docker/PyPI installs serve that packaged
bundle directly; source-checkout/non-Docker runs must build it once before
expecting `/app` to load.

```bash
npm ci
npm run dev      # Vite dev server for frontend work
npm run build    # production bundle into mimir/react_app/dist
npm test         # Vitest frontend tests
```

Focused cutover validation:

```bash
env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_web_ui.py tests/test_web_chat_bridge.py --tb=short
npm ci
npm test
```

See [`docs/react-frontend-cutover.md`](./docs/react-frontend-cutover.md) for the
end-to-end smoke checklist covering Chat, Turn Viewer, Ops, SAGA, State/Memory,
the right-side details panel, default-retro skin loading, and PR evidence for
GitHub issue #726 under Chainlink parent #524.

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
