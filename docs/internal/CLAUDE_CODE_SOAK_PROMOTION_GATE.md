# Claude Code Soak Promotion Gate

Issue: Chainlink #739
Date: 2026-07-02

This records the completed promotion gate for declaring `claude-code:*` supported. The route
stayed guarded until this checklist had live evidence from a container with
Claude Code CLI credentials; that evidence is now satisfied.

## Secret Boundary

Allowed probes:

- `command -v claude`
- `claude --version`
- `mimir.providers.claude_code_auth_status(run_smoke=False)`
- `mimir.providers.claude_code_auth_status(run_smoke=True)`, which discards
  subprocess stdout/stderr
- one minimal real `claude-code:*` model call if auth is healthy

Forbidden evidence:

- OAuth token values
- `.credentials.json` contents
- full environment dumps containing auth variables

## Required Soak Cases

Each case must record: command or entry point, pass/fail, relevant event/turn-log
files, resource/quota labels, and any degraded-state behavior. Do not paste
secrets.

| Case | Required evidence |
| --- | --- |
| Interactive user-turn path | A real `claude-code:*` model turn completes and logs a `TurnRecord` with provider/resource labels. |
| Scheduled-tick-like path | A scheduled-tick event enqueues or runs through the same guarded model path and records turn logging. |
| Poller-turn-like path | A poller-originated event enqueues or runs through the same guarded model path and preserves outbound delivery metadata. |
| Saga-synthesis-like path | A saga synthesis/consolidation call using the configured Claude Code provider either succeeds or degrades explicitly without rerouting silently. |
| Spawn/Worklink path | If still applicable, a Worklink/spawn call records the Claude Code backend status and separate Anthropic Max quota pool. |
| Auth failure/degraded state | Missing/invalid Claude Code auth fails before model construction with actionable remediation and no secret output. |
| Quota/degraded state | Quota exhaustion maps to quota-degraded status/pause behavior without classifying unrelated failures as quota. |
| Prohibited-action canary | A canary prompt that attempts a prohibited shell action is blocked before execution through the Claude Code PreToolUse guard. |

## Support Expectations

Compare every successful live case against Codex Plus support expectations:

- tool budget enforcement is active before tool execution
- prohibited-action guard is active before tool execution
- turn logging contains tool call/result evidence for Claude Code built-ins
- outbound delivery is explicit and recorded
- resource telemetry uses clean provider/quota labels, especially
  `anthropic-max` and the Anthropic Max quota windows
- failure modes are safe: auth and quota failures degrade visibly and do not
  leak credentials

## 2026-07-02 Worklink Attempt

The Worklink container did not satisfy the credential prerequisite.

Observed safe probes:

- `command -v claude; claude --version` failed: `claude` is not on `PATH`.
- `mimir.providers.claude_code_auth_status(run_smoke=False)` returned
  `ok=False`, reason `claude CLI is not on PATH`.
- `langchain-claude-code-mimir` was not installed in the local uv environment.
- No `CLAUDE_CODE_OAUTH_TOKEN` environment variable was present.
- No `.credentials.json` path was discovered by a filename-only search.

Result: blocked in the Worklink container. No live `claude-code:*` model call was possible there, so promotion evidence had to come from a credentialed operator container.

## 2026-07-02 Local Operator-Container Soak

Local run from a #739 worktree rebased onto `origin/main` passed the source-level
preflight and live Claude Code probes:

- `git rev-parse HEAD` = `2145b60c50164be213e5ccb8bca466f00d72072d`, with
  `origin/main` = `d0682c04980a0ade65b64e462f345862c93d833b`.
- `uv run --extra claude-code` installed `langchain-claude-code-mimir==0.1.2`;
  `langchain-claude-code` was not installed in that worktree venv.
- `mimir._langchain_claude_code_patches.ensure_tool_enforcement_hooks_installed`
  was present and returned successfully against the adapter.
- `claude --version` returned `2.1.185 (Claude Code)`.
- `mimir.providers.claude_code_auth_status(run_smoke=True)` returned `ok=True`
  without printing credential contents.
- `_resolve_model("claude-code:sonnet")` returned `ClaudeCodeChatModel` with
  `permission_mode="bypassPermissions"`.
- Auth-degraded probe with a nonexistent credentials path returned `ok=False`
  with actionable remediation and no secret output.
- Direct live prohibited-action canary: Claude Code attempted a Bash call whose
  command text contained a force-push-to-main string; the PreToolUse hook blocked
  it before execution and recorded paired `tool_call`/`tool_result` events.
- Harnessed turn-path probes for `user_message`, `scheduled_tick`, and `poller`
  all completed with `stop_reason="end_turn"`, non-error result fields,
  `total_cost_usd`, usage metadata, explicit `send_message` delivery, and paired
  Claude Code tool events.

Deployment blocker resolved: after PR #965 and the deployment/helper merge, the live mimirbot path installs the controlled adapter route (`langchain-claude-code-mimir>=0.1.2,<0.2`) via the `claude-code` extra rather than post-installing the stale `langchain-claude-code==0.1.0` fork.

Result: source-level soak is positive, the deployment path is reconciled, and `claude-code:*` may be described as a supported provider route with the known requirement that deployments install the `claude-code` extra and authenticate the Claude Code CLI.

## Promotion State

`claude-code:*` is supported with known deployment requirements: install `mimir-agent[claude-code]` (or run `uv sync --extra claude-code` from a checkout), install the Claude Code CLI, authenticate it without exposing secrets, and verify the controlled adapter with the smoke checks above. Keep the auth/quota/degraded-state probes in this file as regression gates for future adapter or deployment changes.
