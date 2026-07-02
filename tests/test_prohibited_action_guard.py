"""Tests for the prohibited action guard (Change 4, S5-1 fix).

Covers:
- Pattern matching in check_prohibited_bash / is_bash_tool
- BudgetGateMiddleware integration (blocks prohibited calls, emits event)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mimir._context import _active_turns, reset_current_turn, set_current_turn
from mimir._langchain_claude_code_patches import (
    _pre_tool_use_hook,
    _tool_events_var,
)
from mimir.models import TurnContext
from mimir.tools.prohibited_action_guard import (
    _BLOCK_PREFIX,
    check_prohibited_bash,
    is_bash_tool,
)
from mimir.tools.budget_gate import BudgetGateMiddleware


# ─── check_prohibited_bash patterns ──────────────────────────────────────────


class TestForcePushPatterns:
    def test_force_push_main_blocked(self) -> None:
        result = check_prohibited_bash("git push --force origin main")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_force_push_master_blocked(self) -> None:
        result = check_prohibited_bash("git push --force origin master")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_force_push_with_lease_blocked(self) -> None:
        result = check_prohibited_bash("git push --force-with-lease origin main")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_force_push_reversed_blocked(self) -> None:
        """Reversed-arg form: branch before force flag."""
        result = check_prohibited_bash("git push origin main --force")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_force_push_reversed_master_blocked(self) -> None:
        result = check_prohibited_bash("git push origin master --force")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_short_f_blocked(self) -> None:
        result = check_prohibited_bash("git push -f origin main")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_short_f_master_blocked(self) -> None:
        result = check_prohibited_bash("git push -f origin master")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_feature_branch_allowed(self) -> None:
        """Force-push to a feature branch is NOT prohibited."""
        result = check_prohibited_bash("git push --force origin feature-my-fix")
        assert result is None

    def test_feature_branch_allowed_no_origin(self) -> None:
        """Force-push without naming main/master is allowed."""
        result = check_prohibited_bash("git push --force feature-branch")
        assert result is None

    def test_normal_push_allowed(self) -> None:
        """A regular push to main with no force flag is allowed."""
        result = check_prohibited_bash("git push origin main")
        assert result is None

    def test_normal_push_master_allowed(self) -> None:
        result = check_prohibited_bash("git push origin master")
        assert result is None

    def test_unrelated_command_allowed(self) -> None:
        result = check_prohibited_bash("ls -la /tmp")
        assert result is None

    def test_git_fetch_allowed(self) -> None:
        result = check_prohibited_bash("git fetch origin main")
        assert result is None

    def test_force_push_with_lease_reversed_blocked(self) -> None:
        """Reversed-arg form with --force-with-lease."""
        result = check_prohibited_bash("git push origin main --force-with-lease")
        assert result is not None
        assert _BLOCK_PREFIX in result


class TestComposeEnvGuard:
    """Bash references to ``compose.env`` must be blocked. It's
    operator-managed and holds real secrets (API keys, tokens) plus the
    agent's own runtime config (model, flags), so editing it from the
    in-container shell is both a secret-exposure and a self-modification
    vector. This guard closes it.

    The operator's path (editing compose.env from the host) doesn't go
    through the agent's tool dispatch, so it's unaffected — only the
    in-container shell is constrained.
    """

    def test_compose_env_write_via_redirect_blocked(self) -> None:
        # echo ... >> compose.env — appending config/secrets.
        result = check_prohibited_bash(
            'echo "MIMIR_MODEL=evil" >> /mimir-home/compose.env'
        )
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_compose_env_write_via_overwrite_blocked(self) -> None:
        # echo ... > compose.env — wholesale replacement.
        result = check_prohibited_bash('echo "ANYTHING=x" > compose.env')
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_compose_env_via_cat_heredoc_blocked(self) -> None:
        result = check_prohibited_bash(
            "cat > /mimir-home/compose.env <<'EOF'\nKEY=value\nEOF"
        )
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_compose_env_via_sed_inplace_blocked(self) -> None:
        # sed -i to edit a specific line.
        result = check_prohibited_bash(
            "sed -i 's/MIMIR_MODEL=a/MIMIR_MODEL=b/' "
            "/mimir-home/compose.env"
        )
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_compose_env_read_blocked_too(self) -> None:
        # Coarse on purpose — reading the file gets blocked too. The
        # agent has no legitimate reason to read compose.env (operator
        # secrets live there).
        result = check_prohibited_bash("cat /mimir-home/compose.env")
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_compose_env_substring_in_arg_blocked(self) -> None:
        # ``compose.env`` appearing as a substring anywhere triggers
        # the guard. Operator's host-side ``docker compose --env-file
        # ./compose.env up -d`` runs OUTSIDE the agent's tool dispatch
        # (different process tree), so this only catches in-container
        # shell.
        result = check_prohibited_bash(
            "diff /tmp/backup.env /mimir-home/compose.env"
        )
        assert result is not None
        assert _BLOCK_PREFIX in result

    def test_unrelated_compose_path_allowed(self) -> None:
        # ``docker-compose.yml`` or ``compose.yml`` don't contain
        # ``compose.env`` as a substring → allowed.
        result = check_prohibited_bash("cat /mimir-home/compose.yml")
        assert result is None

    def test_unrelated_env_file_allowed(self) -> None:
        # A different .env file (e.g. ``<home>/.env``) doesn't match.
        result = check_prohibited_bash("cat /mimir-home/.env")
        assert result is None


class TestIsBashTool:
    def test_shell_exec_is_bash(self) -> None:
        assert is_bash_tool("shell_exec") is True

    def test_bash_async_is_bash(self) -> None:
        assert is_bash_tool("bash_async") is True

    def test_bash_exec_is_bash(self) -> None:
        assert is_bash_tool("bash_exec") is True

    def test_mcp_shell_exec_is_bash(self) -> None:
        assert is_bash_tool("mcp__mimir__shell_exec") is True

    def test_mcp_bash_async_is_bash(self) -> None:
        assert is_bash_tool("mcp__mimir__bash_async") is True

    def test_normalized_mcp_shell_exec_is_bash(self) -> None:
        assert is_bash_tool("mcp_mimir_shell_exec") is True

    def test_bash_capital_b_is_bash(self) -> None:
        """claude-code's native shell built-in surfaces as 'Bash' (capital B)
        when registered through deepagents. Regression for the correctness gap
        flagged in Jason's code review: is_bash_tool('Bash') was False before
        this fix, allowing force-push commands through the guard unchecked."""
        assert is_bash_tool("Bash") is True

    def test_non_bash_tool_not_checked(self) -> None:
        assert is_bash_tool("send_message") is False

    def test_memory_query_not_bash(self) -> None:
        assert is_bash_tool("memory_query") is False

    def test_unknown_tool_not_bash(self) -> None:
        assert is_bash_tool("some_random_tool") is False


# ─── BudgetGateMiddleware integration ────────────────────────────────────────


def _make_request(tool_name: str, command: str) -> Any:
    """Build a minimal ToolCallRequest-like object for the middleware tests.

    The middleware uses getattr(request, 'tool_call', None) which returns
    a dict, then .get('name') / .get('args') / .get('id'). We use a
    MagicMock to simulate this without importing ToolCall/ToolRuntime.
    """
    mock_request = MagicMock()
    mock_request.tool_call = {
        "name": tool_name,
        "args": {"command": command},
        "id": "test-tool-call-id",
    }
    mock_request.tool = None
    return mock_request


class TestMiddlewareBlocksProhibited:
    def test_middleware_blocks_prohibited(self) -> None:
        """BudgetGateMiddleware.wrap_tool_call returns ToolMessage(status='error')
        for a force-push to main."""
        from langchain_core.messages import ToolMessage

        middleware = BudgetGateMiddleware()
        request = _make_request("shell_exec", "git push --force origin main")
        handler = MagicMock()

        result = middleware.wrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage), f"Expected ToolMessage, got {type(result)}"
        assert result.status == "error"
        assert _BLOCK_PREFIX in result.content
        # Handler should NOT have been called — the call was blocked.
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_async_blocks_prohibited(self) -> None:
        """awrap_tool_call also blocks prohibited commands."""
        from langchain_core.messages import ToolMessage

        middleware = BudgetGateMiddleware()
        request = _make_request("shell_exec", "git push -f origin master")
        handler = MagicMock()

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert _BLOCK_PREFIX in result.content
        handler.assert_not_called()

    def test_middleware_allows_normal_push(self) -> None:
        """A normal push to main (no force) is NOT blocked by the middleware."""
        from langchain_core.messages import ToolMessage

        middleware = BudgetGateMiddleware()
        request = _make_request("shell_exec", "git push origin main")
        handler = MagicMock(return_value=ToolMessage(
            content="ok", tool_call_id="test-tool-call-id", name="shell_exec",
        ))

        result = middleware.wrap_tool_call(request, handler)

        # Handler should have been called (allowed through prohibition check).
        # It may still be gated by budget, but the prohibition didn't fire.
        handler.assert_called_once_with(request)

    def test_middleware_non_bash_tool_not_checked(self) -> None:
        """Non-bash tools (send_message) are not run through prohibition check."""
        from langchain_core.messages import ToolMessage

        middleware = BudgetGateMiddleware()
        request = _make_request("send_message", "git push --force main")
        handler = MagicMock(return_value=ToolMessage(
            content="sent", tool_call_id="test-tool-call-id", name="send_message",
        ))

        result = middleware.wrap_tool_call(request, handler)

        # send_message is budget-exempt AND not a bash tool, so it goes to handler.
        handler.assert_called_once_with(request)

    def test_middleware_emits_algedonic_event(self) -> None:
        """Blocked prohibited action emits a 'prohibited_action_blocked' event."""
        captured_events: list[tuple] = []

        def fake_emit(kind: str, **kwargs: Any) -> None:
            captured_events.append((kind, kwargs))

        with patch("mimir.tools.budget_gate._emit_event_sync", side_effect=fake_emit):
            middleware = BudgetGateMiddleware()
            request = _make_request("shell_exec", "git push --force origin main")
            handler = MagicMock()
            middleware.wrap_tool_call(request, handler)

        assert captured_events, "Expected at least one event to be emitted"
        kinds = [e[0] for e in captured_events]
        assert "prohibited_action_blocked" in kinds, (
            f"Expected 'prohibited_action_blocked' event, got: {kinds}"
        )
        # Check the reason is present and truncated to <=200 chars.
        _, kwargs = next(e for e in captured_events if e[0] == "prohibited_action_blocked")
        assert "reason" in kwargs
        assert len(kwargs["reason"]) <= 200
        assert kwargs["tool"] == "shell_exec"


# ─── Claude Code PreToolUse enforcement ─────────────────────────────────────


def _make_turn_ctx(*, budget: int = 5) -> TurnContext:
    return TurnContext(
        turn_id="t-claude-code-guard",
        session_id="ch-claude-code-guard",
        trigger="user_message",
        channel_id="ch-claude-code-guard",
        started_at=time.monotonic(),
        tool_call_budget=budget,
    )


def _permission_decision(result: dict[str, Any]) -> str | None:
    output = result.get("hookSpecificOutput")
    if not isinstance(output, dict):
        return None
    decision = output.get("permissionDecision")
    return decision if isinstance(decision, str) else None


class TestClaudeCodePreToolUseGuard:
    @pytest.mark.asyncio
    async def test_prohibited_bash_canary_denies_before_runtime(self) -> None:
        """A denied Claude Code Bash call returns a PreToolUse denial, so a
        simulated SDK dispatcher does not invoke the tool runtime."""
        runtime_called = False
        events: list[dict[str, Any]] = []
        token = _tool_events_var.set(events)
        try:
            result = await _pre_tool_use_hook(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push --force origin main"},
                },
                "toolu_canary",
                None,
            )
            if _permission_decision(result) != "deny":
                runtime_called = True
        finally:
            _tool_events_var.reset(token)

        assert _permission_decision(result) == "deny"
        assert runtime_called is False
        assert any(
            event.get("type") == "tool_result"
            and event.get("is_error") is True
            and event.get("denied") is True
            for event in events
        )

    @pytest.mark.asyncio
    async def test_budget_denial_uses_session_id_when_hook_task_lacks_context(
        self,
    ) -> None:
        ctx = _make_turn_ctx(budget=1)
        ctx.tool_call_count = 1
        _active_turns[ctx.turn_id] = ctx
        try:
            result = await _pre_tool_use_hook(
                {
                    "session_id": ctx.turn_id,
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/example.txt"},
                },
                "toolu_budget",
                None,
            )
        finally:
            _active_turns.pop(ctx.turn_id, None)

        assert _permission_decision(result) == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Tool-call budget exhausted" in reason
        assert ctx.tool_call_count == 1

    @pytest.mark.asyncio
    async def test_allowed_claude_code_tool_consumes_budget(self) -> None:
        ctx = _make_turn_ctx(budget=2)
        token = set_current_turn(ctx)
        try:
            result = await _pre_tool_use_hook(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/example.txt"},
                },
                "toolu_read",
                None,
            )
        finally:
            reset_current_turn(token)

        assert result == {}
        assert ctx.tool_call_count == 1
