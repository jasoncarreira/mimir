"""PreToolUse / PostToolUse hooks layered on SDK preset tools (SPEC §7.3, §6.3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mimir.event_logger import init_logger
from mimir.hooks import (
    PATH_GUARDED_TOOLS,
    WRITE_TOOLS,
    make_post_tool_use_hook,
    make_pre_tool_use_hook,
)


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


def _ctx() -> dict:
    return {"signal": None}


@pytest.mark.asyncio
async def test_pre_hook_passes_through_when_path_inside_home(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "memory/topics/x.md"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_pre_hook_extra_roots_allow_absolute_paths(tmp_path: Path):
    """When extra_roots is configured, file-op tools accept paths
    inside any of the roots — mimirbot's case for reading its own
    source at /workspace/mimir or the bench harness at /benchmark."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    target = workspace / "mimir" / "agent.py"
    target.parent.mkdir()
    target.write_text("# agent")

    hook = make_pre_tool_use_hook(home, extra_roots=[workspace])
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(target)},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    # No deny → empty dict means the hook allows the call.
    assert out == {}


@pytest.mark.asyncio
async def test_pre_hook_extra_roots_still_block_unconfigured_paths(tmp_path: Path):
    """A path outside both home AND extra_roots is rejected."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    elsewhere = tmp_path / "elsewhere"
    home.mkdir()
    workspace.mkdir()
    elsewhere.mkdir()
    secret = elsewhere / "secret.md"
    secret.write_text("nope")

    hook = make_pre_tool_use_hook(home, extra_roots=[workspace])
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(secret)},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    deny = out["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"
    assert "MIMIR_FILE_OP_ROOTS" in deny["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_pre_hook_denies_absolute_path_outside_home(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/passwd"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    deny = out["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"
    assert "escapes configured roots" in deny["permissionDecisionReason"]
    assert "Read" in deny["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_pre_hook_allows_absolute_path_inside_home(tmp_path: Path):
    """SDK CLI typically forwards absolute paths even for relative model
    inputs. The hook must accept absolute paths that resolve inside home."""
    hook = make_pre_tool_use_hook(tmp_path)
    abs_path = str(tmp_path / "memory" / "topics" / "x.md")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": abs_path},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_pre_hook_denies_dotdot_escape(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "../etc/evil"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    deny = out["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"
    assert "escapes configured roots" in deny["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_pre_hook_ignores_tools_without_paths(tmp_path: Path):
    """Bash isn't path-guarded — its commands can do anything inside home;
    confinement comes from the cwd being set."""
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /etc"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_pre_hook_ignores_other_tools(tmp_path: Path):
    """MCP tools (mcp__mimir__*) aren't in PATH_GUARDED_TOOLS — their args
    are validated inside the tool handler itself."""
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__mimir__file_search",
            "tool_input": {"query": "x"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_post_hook_skipped_when_no_indexer(tmp_path: Path):
    hook = make_post_tool_use_hook(tmp_path, reindex=None)
    out = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "memory/x.md"},
            "tool_response": {"is_error": False},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_post_hook_calls_reindex_for_write(tmp_path: Path):
    reindex = AsyncMock()
    hook = make_post_tool_use_hook(tmp_path, reindex=reindex)

    target = tmp_path / "memory" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_text("body")

    await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"is_error": False},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    reindex.assert_awaited_once_with("memory/x.md")


@pytest.mark.asyncio
async def test_post_hook_skips_failed_writes(tmp_path: Path):
    reindex = AsyncMock()
    hook = make_post_tool_use_hook(tmp_path, reindex=reindex)
    await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "memory/x.md"},
            "tool_response": {"is_error": True, "content": "old_string not found"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    reindex.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_hook_ignores_non_write_tools(tmp_path: Path):
    reindex = AsyncMock()
    hook = make_post_tool_use_hook(tmp_path, reindex=reindex)
    await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "memory/x.md"},
            "tool_response": {},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    reindex.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_hook_handles_relative_paths(tmp_path: Path):
    """When the model passes a relative path, the hook can use it directly
    without trying to compute relative-from-absolute."""
    reindex = AsyncMock()
    hook = make_post_tool_use_hook(tmp_path, reindex=reindex)
    await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "memory/topics/y.md"},
            "tool_response": {"is_error": False},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    reindex.assert_awaited_once_with("memory/topics/y.md")


def test_constants_have_expected_membership():
    assert "Read" in PATH_GUARDED_TOOLS
    assert "Grep" in PATH_GUARDED_TOOLS  # added in Phase 6
    assert "Write" in WRITE_TOOLS
    assert "Read" not in WRITE_TOOLS
    # Web tools are NOT path-guarded; Bash inherits cwd-confinement.
    assert "WebSearch" not in PATH_GUARDED_TOOLS
    assert "WebFetch" not in PATH_GUARDED_TOOLS
    assert "Bash" not in PATH_GUARDED_TOOLS


@pytest.mark.asyncio
async def test_pre_hook_guards_grep_path_arg(tmp_path: Path):
    """Grep accepts an optional ``path`` arg (search root). Confinement applies."""
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO", "path": "/etc"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    deny = out["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_pre_hook_passes_grep_with_no_path(tmp_path: Path):
    """Grep without an explicit path defaults to cwd — fine."""
    hook = make_pre_tool_use_hook(tmp_path)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    assert out == {}


@pytest.mark.asyncio
async def test_pre_hook_does_not_guard_web_tools(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    for tool_name, tool_input in [
        ("WebSearch", {"query": "anything"}),
        ("WebFetch", {"url": "https://example.com", "prompt": "summarize"}),
    ]:
        out = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": "tu",
            },
            "tu",
            _ctx(),
        )
        assert out == {}, f"{tool_name} should not be path-guarded"


# ── Tool-call budget tests ───────────────────────────────────────────────────

import pytest as _pytest  # noqa: E402

from mimir import _context  # noqa: E402
from mimir.models import TurnContext  # noqa: E402


def _budget_ctx(budget: int = 5) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        session_id="c-1",
        trigger="user_message",
        channel_id="c-1",
        started_at=0.0,
        tool_call_budget=budget,
    )


@_pytest.mark.asyncio
async def test_pre_hook_budget_lookup_by_session_id(tmp_path: Path):
    """Stage-1+ migration regression: hook callbacks fire on a task
    that the SDK forked at first client.connect(), so contextvar
    inheritance is broken — every turn after the first sees the
    fork-time ctx (None or the first turn's), and budget counter
    accumulates instead of resetting. The fix is session_id-based
    lookup; this test pins it.

    Simulate the broken-contextvar scenario by NOT setting the
    contextvar — only registering the ctx via set_current_turn (which
    populates _active_turns) and then immediately resetting it. The
    hook receives session_id in input_data, looks up _active_turns,
    finds the live ctx, and increments its (per-turn) counter."""
    from mimir._context import _active_turns

    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=10)
    # Manually register without setting contextvar — mirrors what the
    # real run_turn does, viewed from the hook task's perspective
    # (where contextvar is not visible).
    _active_turns[ctx.turn_id] = ctx
    try:
        for _i in range(3):
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu",
                    "session_id": ctx.turn_id,
                },
                "tu",
                _ctx(),
            )
            assert out == {}
        assert ctx.tool_call_count == 3
    finally:
        _active_turns.pop(ctx.turn_id, None)


@_pytest.mark.asyncio
async def test_pre_hook_budget_resets_per_turn_via_session_id(tmp_path: Path):
    """Two consecutive turns must each get their own budget counter.
    Pre-fix bug: hook saw turn 1's ctx for turn 2's calls and
    accumulated (turn 2 saw 31/30 on its first call). Post-fix:
    session_id lookup returns turn N's ctx for turn N's hook calls.

    Both turns burn 5 calls; both end with their own ctx at count=5,
    not count=10."""
    from mimir._context import _active_turns

    hook = make_pre_tool_use_hook(tmp_path)
    ctx_a = TurnContext(
        turn_id="turn-a", session_id="c-1", trigger="user_message",
        channel_id="c-1", started_at=0.0, tool_call_budget=10,
    )
    ctx_b = TurnContext(
        turn_id="turn-b", session_id="c-1", trigger="user_message",
        channel_id="c-1", started_at=0.0, tool_call_budget=10,
    )
    for ctx in (ctx_a, ctx_b):
        _active_turns[ctx.turn_id] = ctx
        try:
            for _i in range(5):
                await hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Grep",
                        "tool_input": {"pattern": "x"},
                        "tool_use_id": "tu",
                        "session_id": ctx.turn_id,
                    },
                    "tu",
                    _ctx(),
                )
        finally:
            _active_turns.pop(ctx.turn_id, None)
    # Each ctx counted only its own 5 calls — no cross-turn bleed.
    assert ctx_a.tool_call_count == 5
    assert ctx_b.tool_call_count == 5


@_pytest.mark.asyncio
async def test_pre_hook_budget_passes_under_threshold(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=10)
    token = _context.set_current_turn(ctx)
    try:
        for _i in range(3):
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
            assert out == {}
        assert ctx.tool_call_count == 3
    finally:
        _context.reset_current_turn(token)


@_pytest.mark.asyncio
async def test_pre_hook_budget_warns_at_soft_threshold(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=10)  # soft threshold = 7
    token = _context.set_current_turn(ctx)
    try:
        # Burn the first 6 calls — pass-through.
        for _i in range(6):
            await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
        # 7th call hits the soft threshold and emits an allow-with-warning.
        out = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Grep",
                "tool_input": {"pattern": "y"},
                "tool_use_id": "tu",
            },
            "tu",
            _ctx(),
        )
        decision = out.get("hookSpecificOutput", {})
        assert decision.get("permissionDecision") == "allow"
        assert "tool-call budget at 7/10" in decision.get("permissionDecisionReason", "")
    finally:
        _context.reset_current_turn(token)


@_pytest.mark.asyncio
async def test_pre_hook_budget_denies_over_cap(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=3)
    token = _context.set_current_turn(ctx)
    try:
        # First 3 calls allowed.
        for _i in range(3):
            await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "memory/x.md"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
        # 4th is over budget — denied.
        out = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "memory/y.md"},
                "tool_use_id": "tu",
            },
            "tu",
            _ctx(),
        )
        decision = out.get("hookSpecificOutput", {})
        assert decision.get("permissionDecision") == "deny"
        reason = decision.get("permissionDecisionReason", "")
        assert "Tool-call budget exhausted" in reason
        assert "4/3" in reason
    finally:
        _context.reset_current_turn(token)


@_pytest.mark.asyncio
async def test_pre_hook_budget_exempts_send_message_and_react(tmp_path: Path):
    """send_message and react MUST stay callable even when the budget is
    exhausted — they're the agent's exit hatch from the panic-loop deny."""
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=2)
    token = _context.set_current_turn(ctx)
    try:
        # Burn the entire budget.
        for _i in range(2):
            await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
        # send_message and react still pass through and don't increment.
        for tool_name in ["mcp__mimir__send_message", "mcp__mimir__react"]:
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": {"text": "answer", "channel_id": "c-1"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
            assert out == {}, f"{tool_name} should bypass budget"
        assert ctx.tool_call_count == 2  # not incremented by send/react
    finally:
        _context.reset_current_turn(token)


@_pytest.mark.asyncio
async def test_pre_hook_budget_zero_disables_check(tmp_path: Path):
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=0)
    token = _context.set_current_turn(ctx)
    try:
        for _i in range(50):
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu",
                },
                "tu",
                _ctx(),
            )
            assert out == {}
        assert ctx.tool_call_count == 0  # not incremented when budget=0
    finally:
        _context.reset_current_turn(token)
