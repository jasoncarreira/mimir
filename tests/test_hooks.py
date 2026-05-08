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


def _read_events(tmp_path: Path) -> list[dict]:
    """Read events.jsonl back into a list of dicts. The autouse _logger
    fixture writes here; tests that emit events can inspect them."""
    import json

    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@_pytest.mark.asyncio
async def test_pre_hook_budget_events_record_resolution_path_session_id(
    tmp_path: Path,
):
    """CR#18: tool_call_denied / tool_call_budget_warning events must
    carry a resolution_path field that distinguishes the by-session-id
    lookup (production path under the SDK harness, where the hook task's
    contextvars are stale) from the contextvar fallback (same-task
    callers and tests). If the SDK ever stops passing session_id in
    hook input, this counter flips and the operator notices instead of
    budget enforcement silently breaking."""
    from mimir._context import _active_turns

    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=2)  # soft threshold = 1
    # Mirror the SDK-harness scenario: ctx registered for session_id
    # lookup, contextvar NOT set.
    _active_turns[ctx.turn_id] = ctx
    try:
        # Call 1 hits soft threshold (count=1, budget=2 → 70% = 1) → warning event.
        # Call 2 fills budget. Call 3 exceeds → deny event.
        for _i in range(3):
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

    events = _read_events(tmp_path)
    warns = [e for e in events if e.get("type") == "tool_call_budget_warning"]
    denies = [
        e
        for e in events
        if e.get("type") == "tool_call_denied"
        and e.get("reason") == "tool_call_budget_exceeded"
    ]
    assert warns, "expected a tool_call_budget_warning event"
    assert denies, "expected a tool_call_denied event"
    assert all(e.get("resolution_path") == "session_id" for e in warns)
    assert all(e.get("resolution_path") == "session_id" for e in denies)


@_pytest.mark.asyncio
async def test_pre_hook_budget_events_record_resolution_path_single_active(
    tmp_path: Path,
):
    """When the SDK doesn't pass a matching session_id but exactly one
    turn is registered in ``_active_turns``, the resolution chain picks
    it via ``get_only_active_turn()`` (resolution_path='single_active').
    This is the primary production path: SDK 0.1.x emits a CLI-internal
    session_id that doesn't match our turn_id keys, so the chain falls
    through to the single-active heuristic — which is what we want, the
    one in-flight turn IS the one we should be enforcing against."""
    hook = make_pre_tool_use_hook(tmp_path)
    ctx = _budget_ctx(budget=1)  # any over-budget call denies immediately
    token = _context.set_current_turn(ctx)  # registers in _active_turns
    try:
        for _i in range(2):
            await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "memory/x.md"},
                    "tool_use_id": "tu",
                    # No session_id — forces single_active resolution.
                },
                "tu",
                _ctx(),
            )
    finally:
        _context.reset_current_turn(token)

    events = _read_events(tmp_path)
    denies = [
        e
        for e in events
        if e.get("type") == "tool_call_denied"
        and e.get("reason") == "tool_call_budget_exceeded"
    ]
    assert denies, "expected a tool_call_denied event"
    assert all(e.get("resolution_path") == "single_active" for e in denies)


@_pytest.mark.asyncio
async def test_pre_hook_budget_punts_on_stale_contextvar(tmp_path: Path):
    """The SDK's hook task is forked at first ``client.connect()`` and
    captures whatever ``_current_turn`` was at that moment. If that
    turn ends but the hook task's contextvar still points at it, the
    hook would otherwise mutate the dead ctx forever — its
    ``tool_call_count`` accumulates across every subsequent turn until
    it exceeds budget, and from then on every tool call is denied.

    Guard: when the contextvar fallback resolves to a ctx whose
    turn_id is no longer in ``_active_turns``, we DON'T enforce. A
    ``tool_call_budget_punted`` event surfaces the punt for ops."""
    from mimir._context import _active_turns

    hook = make_pre_tool_use_hook(tmp_path)
    # Build a ctx that's set on the contextvar but explicitly NOT in
    # _active_turns — simulates the post-turn-end state where the hook
    # task is still pointing at a deregistered ctx.
    ctx = _budget_ctx(budget=1)
    ctx.tool_call_count = 999  # would otherwise deny immediately
    _context._current_turn.set(ctx)
    assert ctx.turn_id not in _active_turns

    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "memory/x.md"},
            "tool_use_id": "tu",
        },
        "tu",
        _ctx(),
    )
    # No deny — the stale ctx is treated as "no active turn".
    assert out == {} or "permissionDecision" not in out.get(
        "hookSpecificOutput", {}
    )
    # Stale ctx's count must NOT have been incremented.
    assert ctx.tool_call_count == 999

    events = _read_events(tmp_path)
    punts = [
        e
        for e in events
        if e.get("type") == "tool_call_budget_punted"
        and e.get("reason") == "stale_contextvar"
    ]
    assert punts, "expected a tool_call_budget_punted event"
    assert punts[0].get("stale_turn_id") == ctx.turn_id


@_pytest.mark.asyncio
async def test_pre_hook_budget_per_client_cell_correct_under_concurrency(
    tmp_path: Path,
):
    """Multi-channel concurrent: two pooled clients, each with its own
    ``_TurnCell`` captured by its own SDK hook task. Two turns in
    flight at the same time; tool calls coming from client A's hook
    must increment ctx_A's counter, not ctx_B's.

    The ``_current_client_cell`` ContextVar simulates the per-task
    capture the SDK does at connect: we set it to cell_A, fire client
    A's hook, set it to cell_B, fire client B's hook. Each hook fire
    sees the cell it captured; mutations to ``cell.turn_id`` (done by
    ``acquire_ctx``) are visible. This is the property the contextvar
    fallback FAILED to guarantee — see
    test_pre_hook_budget_punts_on_stale_contextvar for the failure
    mode this design fixes."""
    from mimir._context import _TurnCell, _active_turns, _current_client_cell

    hook = make_pre_tool_use_hook(tmp_path)
    ctx_a = TurnContext(
        turn_id="ta", session_id="ca", trigger="user_message",
        channel_id="ca", started_at=0.0, tool_call_budget=10,
    )
    ctx_b = TurnContext(
        turn_id="tb", session_id="cb", trigger="user_message",
        channel_id="cb", started_at=0.0, tool_call_budget=10,
    )
    _active_turns[ctx_a.turn_id] = ctx_a
    _active_turns[ctx_b.turn_id] = ctx_b

    cell_a = _TurnCell()
    cell_a.turn_id = ctx_a.turn_id
    cell_b = _TurnCell()
    cell_b.turn_id = ctx_b.turn_id

    try:
        # Three tool calls on client A, two on client B, interleaved.
        # Each must hit only its own ctx's counter.
        sequence = [cell_a, cell_b, cell_a, cell_a, cell_b]
        for cell in sequence:
            token = _current_client_cell.set(cell)
            try:
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
            finally:
                _current_client_cell.reset(token)

        assert ctx_a.tool_call_count == 3
        assert ctx_b.tool_call_count == 2

        events = _read_events(tmp_path)
        # No denies, no warnings (under budget), no punts.
        punts = [e for e in events if e.get("type") == "tool_call_budget_punted"]
        assert punts == []
        # All resolution_path tags should be client_cell — the per-client
        # path is the primary one when the cell is set.
        denies = [e for e in events if e.get("type") == "tool_call_denied"]
        warns = [e for e in events if e.get("type") == "tool_call_budget_warning"]
        for e in denies + warns:
            assert e.get("resolution_path") == "client_cell"
    finally:
        _active_turns.pop(ctx_a.turn_id, None)
        _active_turns.pop(ctx_b.turn_id, None)


@_pytest.mark.asyncio
async def test_pre_hook_budget_per_client_cell_isolates_budget_breaches(
    tmp_path: Path,
):
    """Even when one channel exhausts its budget, a concurrent channel
    with budget remaining must keep working. Pins the multi-channel
    isolation property."""
    from mimir._context import _TurnCell, _active_turns, _current_client_cell

    hook = make_pre_tool_use_hook(tmp_path)
    ctx_busy = TurnContext(
        turn_id="t-busy", session_id="c-busy", trigger="user_message",
        channel_id="c-busy", started_at=0.0,
        tool_call_budget=2, tool_call_count=2,  # already at limit
    )
    ctx_fresh = TurnContext(
        turn_id="t-fresh", session_id="c-fresh", trigger="user_message",
        channel_id="c-fresh", started_at=0.0, tool_call_budget=10,
    )
    _active_turns[ctx_busy.turn_id] = ctx_busy
    _active_turns[ctx_fresh.turn_id] = ctx_fresh

    cell_busy = _TurnCell()
    cell_busy.turn_id = ctx_busy.turn_id
    cell_fresh = _TurnCell()
    cell_fresh.turn_id = ctx_fresh.turn_id

    try:
        # Tool call on the busy channel — denies.
        token = _current_client_cell.set(cell_busy)
        try:
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "memory/x.md"},
                    "tool_use_id": "tu1",
                },
                "tu1",
                _ctx(),
            )
        finally:
            _current_client_cell.reset(token)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

        # Tool call on the fresh channel — passes.
        token = _current_client_cell.set(cell_fresh)
        try:
            out = await hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x"},
                    "tool_use_id": "tu2",
                },
                "tu2",
                _ctx(),
            )
        finally:
            _current_client_cell.reset(token)
        assert out == {}
        assert ctx_fresh.tool_call_count == 1
        # Busy ctx wasn't bumped further by the second call.
        assert ctx_busy.tool_call_count == 3  # was 2, deny incremented to 3
    finally:
        _active_turns.pop(ctx_busy.turn_id, None)
        _active_turns.pop(ctx_fresh.turn_id, None)


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
