# Claude Code Soak Promotion Gate

Issue: Chainlink #739
Date: 2026-07-02

This is the promotion gate for declaring `claude-code:*` supported. The route
must not be promoted from deprecated/guarded opt-in until this checklist has
live evidence from a container with Claude Code CLI credentials.

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

Result: blocked. No live `claude-code:*` model call was possible, so the route
must remain unpromoted.

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

Blocker found: the live mimirbot helper installed in the container at
`/usr/local/bin/mimirbot-agent.sh` still runs `uv sync --extra discord --extra
codex-plus` and then, when `MIMIR_ALLOW_CLAUDE_CODE=1`, post-installs the stale
`langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@...`
fork. The currently-running daemon venv therefore showed
`langchain-claude-code==0.1.0` and no `langchain-claude-code-mimir` before the
worktree-specific `uv run --extra claude-code` checks. A plain source pull plus
restart can still boot the merged code against the stale adapter and fail the
adapter support assertion.

Result: source-level soak is positive, but promotion remains blocked until the
baked helper/image path is reconciled to install the `claude-code` extra (or
otherwise install `langchain-claude-code-mimir>=0.1.2,<0.2`) and stop
post-installing the old fork.

## Promotion Rule

Only after all required live cases pass may the maintainer update
`claude-code:*` messaging/defaults from deprecated/guarded opt-in to supported
or supported-with-known-limits. The same change set must update release notes
and memory gotchas with the final supported state.
