---
name: mimir-project-architecture-and-validation
description: Durable Mimir architecture boundaries, validation commands, release requirements, and security-sensitive surfaces for feature work.
type: project
---

Mimir (`mimir-agent` on PyPI, `mimir` as the import package) is a Python 3.11+ long-running LLM-agent harness built on DeepAgents/LangGraph. It combines persistent memory, skills/tools, scheduler ticks, channel bridges, feedback loops, and an operator UI.

## Code Boundaries

- `mimir/server.py` is the HTTP/runtime composition root. It wires the agent, dispatcher, history, indexing, SAGA, sessions, scheduler, and bridges.
- `mimir/agent.py` owns the shared DeepAgents graph, model routing, tool execution, memory injection, and memory crediting. Prompt-construction order changes affect every deployment and should be treated as high risk.
- `mimir/saga/` is the production memory backend: SQLite/WAL, FTS5, FAISS, embeddings, triples, migrations, and consolidation. It is not `benchmarks/saga/`, which is a separate uv workspace benchmark shell.
- `mimir/bridges/` contains Discord, Slack, local web chat, and benchmark stdout adapters. Per-channel dispatch is FIFO; channels run concurrently under a global cap.
- `frontend/` is a separate React/TypeScript/Vite app. Production output is `mimir/react_app/dist` and aiohttp serves it at `/app`.
- Frontend API contracts are generated from `mimir.web_contracts`; never hand-edit `frontend/src/api/generated/contracts.ts`.
- Durable agent-home state includes JSONL turns/events/history, Markdown memory/state trees, schedules, skills, and `.mimir` runtime data. `SagaStore` owns `mimir.saga.db`, serializes writes, and uses separate production read connections to avoid shared-connection/FTS failures.

## Validation Contract

- Repository Python is pinned by `.tool-versions` and supports 3.11+. Set up a normal development checkout with `uv sync --extra dev`.
- Main Python gate: `uv run pytest -q --tb=short`. CI runs it on Python 3.11 and 3.12 after skill-conformance tests.
- Run every `mimir/optional-skills/*/tests` directory in its own pytest process. Their duplicate `poller` and `tests.conftest` module names collide when combined.
- Agent-loop changes also require the LongMemEval harness described in `benchmarks/longmemeval_via_mimir/README.md`.
- Frontend gate: `npm ci`, `npm test`, `npm run typecheck`, and `npm run build`. CI's effective frontend gate is `npm run test` plus `npm run build` (which includes `tsc --noEmit`).
- There is no declared repository-wide Python lint command or frontend `lint` script; do not invent one as an acceptance gate.
- Packaging uses Hatchling. Build the React app before `uv build`; `mimir/react_app/dist` is VCS-ignored but force-included in the wheel. The publish workflow asserts that the wheel contains `mimir/react_app/dist/index.html`.
- Tag pushes matching `v*` build and publish to PyPI through OIDC, then create a GitHub Release from the matching `CHANGELOG.md` section.

## Change And Review Rules

- Branch from `main`. Substantial changes should start from an issue and PRs require one approving review.
- Prefer existing patterns over new abstractions; new Python modules use future annotations and public functions carry type hints. Tests generally mirror the module under test.
- A green focused test is not sufficient. Run the full applicable gate and verify real CLI/wiring signatures when tests use fakes.
- Review strictly: any concern worth reporting requires changes rather than an approval with nits.
- Do not disturb unrelated branches, worktrees, or uncommitted state. Mimir has many concurrent autonomous and operator worktrees.

## Security And Deployment Boundaries

- Supported posture is one operator inside a trusted boundary, not public multi-tenant ingress. Public/non-loopback HTTP binding is refused without `MIMIR_API_KEY`.
- The agent and installed skills can run shell commands, access agent-home files, and make outbound calls without skill-level privilege separation. Treat skills and subprocess backends as privileged code.
- `mimir/tools/prohibited_action_guard.py` is regex defense-in-depth, not a sandbox. DNS-rebinding mitigation for web fetch is not fully enforced, and shell-output redaction is incomplete.
- The `claude-code` model path uses `bypassPermissions`; Mimir's adapter pre-tool enforcement hooks are therefore security-critical.
- Authorization changes, `mimir/server.py`, `mimir/agent.py`, `mimir/saga/`, migrations, generated contracts, credentials, and CI/release plumbing deserve high-risk review.
- Never run `uv run` or `uv sync` against the live mimirbot `/workspace/mimir` checkout; use its existing `.venv/bin/python` for smokes and inspect its branch/uncommitted state before deployment work.

Primary sources: `README.md`, `SPEC.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/workflows/tests.yml`, `.github/workflows/publish.yml`, `pyproject.toml`, and `package.json`.
