"""Tests for Claude Code adapter validation and Mimir's tool-safety hooks.

The maintained ``langchain-claude-code-mimir`` package now carries the adapter
fixes that used to live as Mimir monkeypatches. This module exercises the
remaining Mimir-owned safety plane: SDK PreToolUse/PostToolUse hooks and the
validation helpers that keep stale upstream ``langchain-claude-code==0.1.0``
unsupported.
"""
from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from mimir._langchain_claude_code_patches import (
    _post_tool_use_failure_hook,
    _post_tool_use_hook,
    _pre_tool_use_hook,
    _tool_events_var,
    install_tool_event_hooks,
)


# ── install_tool_event_hooks ────────────────────────────────────────


def _clear_tool_event_marker(cls: type) -> None:
    if hasattr(cls, "_mimir_tool_event_hooks_installed"):
        delattr(cls, "_mimir_tool_event_hooks_installed")


@pytest.mark.asyncio
async def test_pre_post_hooks_record_events_with_tool_use_id():
    """The pre/post hook callbacks themselves should append correctly
    shaped event dicts to the active capture list. Verifies the
    serialization shape independent of SDK plumbing — fast, no fake
    chat-model class needed."""
    events: list[dict[str, Any]] = []
    token = _tool_events_var.set(events)
    try:
        await _pre_tool_use_hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            "toolu_01abc",
            None,
        )
        await _post_tool_use_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"output": "file.txt"},
            },
            "toolu_01abc",
            None,
        )
    finally:
        _tool_events_var.reset(token)

    assert len(events) == 2
    call, result = events
    assert call["type"] == "tool_call"
    assert call["tool_use_id"] == "toolu_01abc"
    assert call["name"] == "Bash"
    assert call["input"] == {"command": "ls"}
    assert "ts_mono_ns" in call

    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_01abc"
    assert result["name"] == "Bash"
    assert result["result"] == {"output": "file.txt"}
    assert result["is_error"] is False
    # Result should arrive after the call (monotonic clock).
    assert result["ts_mono_ns"] >= call["ts_mono_ns"]


@pytest.mark.asyncio
async def test_failure_hook_records_is_error_true():
    """PostToolUseFailure should produce a tool_result event with
    ``is_error=True`` and the SDK-supplied error string preserved."""
    events: list[dict[str, Any]] = []
    token = _tool_events_var.set(events)
    try:
        await _post_tool_use_failure_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "/bin/false"},
                "error": "exited with code 1",
            },
            "toolu_01fail",
            None,
        )
    finally:
        _tool_events_var.reset(token)

    assert len(events) == 1
    e = events[0]
    assert e["type"] == "tool_result"
    assert e["is_error"] is True
    assert e["error"] == "exited with code 1"
    assert e["tool_use_id"] == "toolu_01fail"


@pytest.mark.asyncio
async def test_hooks_noop_outside_active_context():
    """When no capture context is set (``_tool_events_var`` is None),
    the hook callbacks must silently no-op — they can't append to a
    nonexistent list. Important because hooks could in principle fire
    from a stray code path that hasn't entered our patched _aquery."""
    # Verify ContextVar default is None before we set it anywhere.
    assert _tool_events_var.get() is None
    out = await _pre_tool_use_hook(
        {"tool_name": "Bash", "tool_input": {}}, "toolu_orphan", None,
    )
    assert out == {}  # no-op return shape
    assert _tool_events_var.get() is None  # untouched


def _make_dummy_for_hooks() -> tuple[type, type]:
    """Stand-in for ChatClaudeCode covering the surface our hooks patch
    monkeys: ``_build_options``, ``_aquery``, ``_astream``.

    ``_build_options`` returns a minimal stand-in for ``ClaudeAgentOptions``
    so we can assert that our hooks dict was merged in.
    ``_aquery`` simulates the original return tuple shape ``(content,
    tool_calls, generation_info)``.
    ``_astream`` simulates a streaming sequence with a final result chunk
    carrying ``finish_reason``.
    """
    ccm = pytest.importorskip("langchain_claude_code.claude_chat_model")

    class _FakeOptions:
        def __init__(self) -> None:
            # ``ClaudeAgentOptions.hooks`` defaults to None upstream; the
            # patch must handle that path without crashing.
            self.hooks: dict | None = None

    class _Chunk:
        def __init__(self, content: str, generation_info: dict | None):
            class _M:
                def __init__(self, c: str):
                    self.content = c
            self.message = _M(content)
            self.generation_info = generation_info

    class _FakeChatClaudeCode:
        def _build_options(self, **overrides: Any) -> _FakeOptions:
            return _FakeOptions()

        async def _aquery(self, *args: Any, **kwargs: Any):
            # Simulate a turn during which our patched _build_options
            # was called and hooks were registered. The hooks would
            # have been invoked by the SDK, populating events_list via
            # ContextVar. We simulate that here directly.
            opts = self._build_options()
            assert opts.hooks is not None, (
                "patched _build_options should have injected hooks"
            )
            # Mimic the SDK invoking hooks during execution.
            await _pre_tool_use_hook(
                {"tool_name": "Read", "tool_input": {"file_path": "/a"}},
                "toolu_1", None,
            )
            await _post_tool_use_hook(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/a"},
                    "tool_response": "contents",
                },
                "toolu_1", None,
            )
            await _pre_tool_use_hook(
                {"tool_name": "Bash", "tool_input": {"command": "echo"}},
                "toolu_2", None,
            )
            await _post_tool_use_hook(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo"},
                    "tool_response": {"output": "hi"},
                },
                "toolu_2", None,
            )
            return "done", [], {"total_cost_usd": 0.0}

        async def _astream(self, *args: Any, **kwargs: Any):
            opts = self._build_options()
            assert opts.hooks is not None
            # Drive hooks (would normally be the SDK).
            await _pre_tool_use_hook(
                {"tool_name": "Glob", "tool_input": {"pattern": "*.py"}},
                "toolu_g1", None,
            )
            await _post_tool_use_hook(
                {
                    "tool_name": "Glob",
                    "tool_input": {"pattern": "*.py"},
                    "tool_response": {"matches": ["a.py"]},
                },
                "toolu_g1", None,
            )
            yield _Chunk(content="hi", generation_info=None)
            yield _Chunk(
                content="",
                generation_info={"finish_reason": "stop", "total_cost_usd": 0.0},
            )

    _orig = ccm.ClaudeCodeChatModel
    ccm.ClaudeCodeChatModel = _FakeChatClaudeCode
    return _FakeChatClaudeCode, _orig


def _restore_chat_model(orig: type) -> None:
    ccm = pytest.importorskip("langchain_claude_code.claude_chat_model")
    ccm.ClaudeCodeChatModel = orig


@pytest.mark.asyncio
async def test_install_hooks_attaches_tool_events_to_aquery_result():
    """After patching, ``_aquery`` must attach the captured events list
    to ``generation_info["tool_events"]``. Events arrive interleaved
    (call→result→call→result) per the hook ordering at call time."""
    fake_cls, orig = _make_dummy_for_hooks()
    try:
        _clear_tool_event_marker(fake_cls)
        install_tool_event_hooks()

        instance = fake_cls()
        content, tool_calls, gi = await instance._aquery()

        assert content == "done"
        events = gi.get("tool_events")
        assert events is not None
        # Two calls + two results, interleaved.
        assert [e["type"] for e in events] == [
            "tool_call", "tool_result", "tool_call", "tool_result",
        ]
        # Names + ids preserved verbatim from the hook input.
        assert events[0]["name"] == "Read"
        assert events[0]["tool_use_id"] == "toolu_1"
        assert events[1]["name"] == "Read"
        assert events[2]["name"] == "Bash"
        assert events[3]["name"] == "Bash"
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_install_hooks_attaches_tool_events_to_astream_result_chunk():
    """In the streaming path, ``tool_events`` must land on the result
    chunk (the one carrying ``finish_reason``), not on intermediate
    text chunks."""
    fake_cls, orig = _make_dummy_for_hooks()
    try:
        _clear_tool_event_marker(fake_cls)
        install_tool_event_hooks()

        instance = fake_cls()
        chunks = [c async for c in instance._astream()]

        # Intermediate text chunk has no generation_info — untouched.
        assert chunks[0].generation_info is None
        # Result chunk carries tool_events.
        gi = chunks[1].generation_info
        assert gi is not None
        assert "tool_events" in gi
        events = gi["tool_events"]
        assert len(events) == 2
        assert events[0]["type"] == "tool_call"
        assert events[0]["name"] == "Glob"
        assert events[1]["type"] == "tool_result"
        assert events[1]["name"] == "Glob"
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_install_hooks_preserves_user_supplied_hooks():
    """The patch must NOT clobber hooks that the operator (or another
    library) already supplied via ``_build_options`` overrides. Our
    callbacks should append to existing matchers, not replace them."""
    fake_cls, orig = _make_dummy_for_hooks()
    try:
        _clear_tool_event_marker(fake_cls)
        # Override _build_options to pre-populate a hook (simulating
        # an operator who set up permission-gate hooks).
        from claude_agent_sdk import HookMatcher

        async def _user_pre(*_a, **_kw):
            return {}

        def _build_with_existing(self, **overrides):  # type: ignore[no-untyped-def]
            class _O:
                def __init__(self) -> None:
                    self.hooks: dict | None = {
                        "PreToolUse": [HookMatcher(hooks=[_user_pre])],
                    }
            return _O()

        fake_cls._build_options = _build_with_existing
        install_tool_event_hooks()

        # Enter a capture context so the patched _build_options injects.
        events: list[dict[str, Any]] = []
        token = _tool_events_var.set(events)
        try:
            opts = fake_cls()._build_options()
        finally:
            _tool_events_var.reset(token)

        # PreToolUse should have BOTH the user's hook AND ours (length 2),
        # in append-order (user first, ours second).
        assert opts.hooks is not None
        assert len(opts.hooks["PreToolUse"]) == 2
        # PostToolUse + PostToolUseFailure should be ours only.
        assert "PostToolUse" in opts.hooks
        assert "PostToolUseFailure" in opts.hooks
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_install_hooks_idempotent():
    """Re-applying the patch is a no-op — the marker prevents double-
    wrapping (which would corrupt _aquery / _astream / _build_options
    on every reload)."""
    fake_cls, orig = _make_dummy_for_hooks()
    try:
        _clear_tool_event_marker(fake_cls)
        install_tool_event_hooks()
        after_first_aquery = fake_cls._aquery
        after_first_astream = fake_cls._astream
        after_first_build = fake_cls._build_options
        assert fake_cls._mimir_tool_event_hooks_installed is True

        install_tool_event_hooks()  # second call — must no-op
        assert fake_cls._aquery is after_first_aquery
        assert fake_cls._astream is after_first_astream
        assert fake_cls._build_options is after_first_build
    finally:
        _restore_chat_model(orig)


# ── integration test: real ClaudeSDKClient + built-in Bash tool ─────


def _claude_cli_available() -> bool:
    """True iff the Claude SDK + ``claude`` CLI are both usable.

    The integration test imports ``claude_agent_sdk`` and then spawns the
    CLI subprocess. Dev-only installs intentionally omit the Claude Code
    adapter extra, so absence of the SDK should skip rather than fail.
    """
    import importlib.util
    import shutil
    import subprocess
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return False
    if not shutil.which("claude"):
        return False
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _claude_sdk_can_invoke() -> bool:
    """True iff the ``claude`` CLI can actually make API calls in this
    environment (i.e. has valid OAuth credentials and can reach Anthropic).

    The integration test uses ``ClaudeSDKClient`` which spawns the claude
    CLI as a subprocess.  The CLI must be able to authenticate — otherwise
    ``receive_response()`` returns immediately with zero messages and no
    hooks fire, producing a false failure (events=[]).

    Probe: run ``claude -p "ok" --output-format text`` with a short
    timeout and check it produces non-empty stdout.  This is a cheap
    single-exchange call; failure (empty output, non-zero exit, or
    timeout) indicates the credentials are absent or the endpoint is
    unreachable.

    Note: this check is intentionally *not* cached — it is only ever
    called during test *collection* (inside ``skipif``), which happens
    once per session.  A session-level cache would be premature
    optimisation for a single integration test.
    """
    if not _claude_cli_available():
        return False
    import subprocess
    try:
        r = subprocess.run(
            ["claude", "-p", "respond with the single word: ok",
             "--output-format", "text"],
            capture_output=True, timeout=30, text=True,
        )
        return r.returncode == 0 and len(r.stdout.strip()) > 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_SKIP_REASON_SDK_INTEGRATION = (
    "claude CLI not available or cannot make API calls in this environment "
    "(missing OAuth credentials / network) — integration test for built-in "
    "tool hook coverage requires a live ClaudeSDKClient turn"
)

@pytest.mark.skipif(
    not _claude_sdk_can_invoke(),
    reason=_SKIP_REASON_SDK_INTEGRATION,
)
@pytest.mark.asyncio
async def test_hooks_capture_built_in_bash_tool_integration():
    """End-to-end: spawn a real ClaudeSDKClient, register our hooks via
    ``install_tool_event_hooks``'s machinery (manually, since we're
    bypassing the langchain wrapper here), and confirm that an actual
    built-in Bash invocation surfaces as paired PreToolUse + PostToolUse
    events.

    This is the canonical proof of the patch's value over the legacy
    ``_parse_assistant_message`` path: built-in tools (Bash/Read/Edit/
    Write/Glob/ToolSearch) execute inside the claude CLI subprocess
    and never surface as ToolResultBlocks in AssistantMessage content.
    Only the hook control-protocol fires for them.

    Skipped in environments without the claude CLI (CI without OAuth
    credentials in keychain). Locally runnable as a sanity check.
    """
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
    )

    events: list[dict[str, Any]] = []
    token = _tool_events_var.set(events)
    try:
        options = ClaudeAgentOptions(
            hooks={
                "PreToolUse": [HookMatcher(hooks=[_pre_tool_use_hook])],
                "PostToolUse": [HookMatcher(hooks=[_post_tool_use_hook])],
                "PostToolUseFailure": [
                    HookMatcher(hooks=[_post_tool_use_failure_hook]),
                ],
            },
            allowed_tools=["Bash"],
            max_turns=4,
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "Use the Bash tool to run `echo hooks-integration-test` "
                "and tell me what it printed. Then stop.",
            )
            async for _ in client.receive_response():
                pass
    finally:
        _tool_events_var.reset(token)

    # Filter to Bash events (the model could in principle also call
    # other built-ins, though we constrained allowed_tools).
    bash_events = [e for e in events if e["name"] == "Bash"]
    assert bash_events, (
        f"expected Bash tool to fire hooks; got events for: "
        f"{sorted({e['name'] for e in events})}"
    )

    # Pair pre/post by tool_use_id. Every PreToolUse must have a
    # matching PostToolUse (or PostToolUseFailure → is_error=True).
    by_id: dict[str, dict[str, dict]] = {}
    for e in bash_events:
        by_id.setdefault(e["tool_use_id"], {})[e["type"]] = e
    for tid, phases in by_id.items():
        assert "tool_call" in phases, (
            f"Bash {tid} has tool_result without tool_call"
        )
        assert "tool_result" in phases, (
            f"Bash {tid} has tool_call without tool_result — this is the "
            "exact bug the hooks patch fixes for built-in tools"
        )

    # The result event's ``tool_use_id`` must equal its paired call's —
    # this is what makes ordering + pairing reliable downstream.
    for tid, phases in by_id.items():
        assert phases["tool_call"]["tool_use_id"] == tid
        assert phases["tool_result"]["tool_use_id"] == tid


@pytest.mark.asyncio
async def test_hooks_capture_built_in_bash_tool_mocked():
    """Mocked companion to ``test_hooks_capture_built_in_bash_tool_integration``.

    The integration test above proves that an unmodified
    ``ClaudeSDKClient`` actually invokes our PreToolUse / PostToolUse
    hooks when a built-in Bash tool fires. That test is skipped in
    environments without claude CLI auth, which means CI (no OAuth
    keychain) and fresh-contributor machines get zero coverage of the
    hook-pairing contract.

    This test exercises the *contract* the SDK is documented to honor:
    for each built-in tool invocation, the SDK calls
    ``_pre_tool_use_hook`` with ``{"tool_name", "tool_input"}`` BEFORE
    execution, then ``_post_tool_use_hook`` with
    ``{"tool_name", "tool_response"}`` AFTER, both with the SAME
    ``tool_use_id``. By simulating that call pattern directly against
    our hook impls — no subprocess, no network, no auth — we pin the
    hook-side wiring even when the SDK side can't be exercised.

    Combined coverage:
    - this test                          ← hook-side contract (always)
    - integration test (skipped w/o auth)← SDK-side wiring (when possible)
    - earlier unit tests in this file    ← hook plumbing internals
    """
    events: list[dict[str, Any]] = []
    token = _tool_events_var.set(events)
    try:
        # The SDK's call pattern for two sequential Bash invocations.
        # Each pair shares a tool_use_id and is delivered in this order:
        # pre(call1), post(call1), pre(call2), post(call2). Real model
        # output also typically yields one pair per Bash command.
        await _pre_tool_use_hook(
            {"tool_name": "Bash", "tool_input": {"command": "echo a"}},
            tool_use_id="toolu_01",
            _ctx=None,
        )
        await _post_tool_use_hook(
            {"tool_name": "Bash",
             "tool_response": {"stdout": "a\n", "stderr": "", "exit_code": 0}},
            tool_use_id="toolu_01",
            _ctx=None,
        )
        await _pre_tool_use_hook(
            {"tool_name": "Bash", "tool_input": {"command": "echo b"}},
            tool_use_id="toolu_02",
            _ctx=None,
        )
        await _post_tool_use_hook(
            {"tool_name": "Bash",
             "tool_response": {"stdout": "b\n", "stderr": "", "exit_code": 0}},
            tool_use_id="toolu_02",
            _ctx=None,
        )
    finally:
        _tool_events_var.reset(token)

    # Same assertions as the integration test, against the simulated
    # event stream.
    bash_events = [e for e in events if e["name"] == "Bash"]
    assert bash_events, (
        f"expected Bash hook events; got: {sorted({e['name'] for e in events})}"
    )

    # Pair pre/post by tool_use_id — every PreToolUse has a matching PostToolUse.
    by_id: dict[str, dict[str, dict]] = {}
    for e in bash_events:
        by_id.setdefault(e["tool_use_id"], {})[e["type"]] = e

    assert set(by_id.keys()) == {"toolu_01", "toolu_02"}, (
        f"expected exactly two paired tool_use_ids; got {sorted(by_id)}"
    )
    for tid, phases in by_id.items():
        assert "tool_call" in phases, (
            f"Bash {tid} has tool_result without tool_call"
        )
        assert "tool_result" in phases, (
            f"Bash {tid} has tool_call without tool_result — the exact "
            "shape the hooks patch fixes for built-in tools"
        )
        # The tool_use_id on each phase round-trips correctly.
        assert phases["tool_call"]["tool_use_id"] == tid
        assert phases["tool_result"]["tool_use_id"] == tid

    # Ordering: pre always precedes post for the same tool_use_id (the
    # SDK guarantees this; we pin that our hooks don't reshuffle).
    by_id_order: dict[str, list[str]] = {}
    for e in bash_events:
        by_id_order.setdefault(e["tool_use_id"], []).append(e["type"])
    for tid, order in by_id_order.items():
        assert order == ["tool_call", "tool_result"], (
            f"Bash {tid} hook events out of order: {order}"
        )

    # Success path: is_error is False on every tool_result.
    for e in bash_events:
        if e["type"] == "tool_result":
            assert e.get("is_error") is False, (
                f"unexpected is_error on success-path event: {e}"
            )


@pytest.mark.asyncio
async def test_hooks_capture_failure_path_mocked():
    """Companion to the success-path mock above. When the SDK reports a
    tool failure (PostToolUseFailure path), the appended event must
    carry ``is_error=True`` and the ``error`` field — distinguishable
    from the success path even though both share the ``tool_result``
    type tag. Downstream consumers (turn-logger, algedonic surfacing)
    rely on this discriminator."""
    events: list[dict[str, Any]] = []
    token = _tool_events_var.set(events)
    try:
        await _pre_tool_use_hook(
            {"tool_name": "Bash", "tool_input": {"command": "exit 1"}},
            tool_use_id="toolu_fail",
            _ctx=None,
        )
        await _post_tool_use_failure_hook(
            {"tool_name": "Bash", "error": "non-zero exit code: 1"},
            tool_use_id="toolu_fail",
            _ctx=None,
        )
    finally:
        _tool_events_var.reset(token)

    assert len(events) == 2
    call, result = events
    assert call["type"] == "tool_call"
    assert call["tool_use_id"] == "toolu_fail"
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_fail"
    assert result["is_error"] is True
    assert result.get("error") == "non-zero exit code: 1"


# ── streaming hook capture ────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_event_hooks_attach_events_to_stream_result_chunk():
    """The streaming wrapper should attach hook events to the final result
    chunk without relying on adapter-level metadata monkeypatches.
    """
    ccm = pytest.importorskip("langchain_claude_code.claude_chat_model")

    class _Chunk:
        def __init__(self, content: str, generation_info: dict | None):
            class _M:
                def __init__(self, c: str):
                    self.content = c
            self.message = _M(content)
            self.generation_info = generation_info

    class _FakeOptions:
        def __init__(self):
            self.hooks: dict | None = None

    class _FakeChatClaudeCode:
        def _build_options(self, **_kw):
            return _FakeOptions()

        async def _aquery(self, *args, **kwargs):
            # Hooks patch wraps this method too; provide a stub so the
            # patch can grab it. Not exercised by this test (we only
            # iterate _astream).
            return "", [], {}

        async def _astream(self, *args, **kwargs):
            # Drive a hook (would normally come from the SDK) so the
            # hooks-side capture list is non-empty when we reach the
            # final chunk.
            opts = self._build_options()
            pre = opts.hooks["PreToolUse"][0].hooks[0]
            await pre(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                "toolu_combo", None,
            )
            yield _Chunk(content="streaming text", generation_info=None)
            yield _Chunk(
                content="",
                generation_info={
                    "finish_reason": "stop",
                    "total_cost_usd": 0.0,
                },
            )

    _orig = ccm.ClaudeCodeChatModel
    ccm.ClaudeCodeChatModel = _FakeChatClaudeCode
    try:
        _clear_tool_event_marker(_FakeChatClaudeCode)

        install_tool_event_hooks()

        instance = _FakeChatClaudeCode()
        chunks = [c async for c in instance._astream()]

        # First chunk is text — untouched by the hook wrapper.
        assert chunks[0].generation_info is None
        assert chunks[0].message.content == "streaming text"

        # Final chunk MUST include the captured tool_events list.
        gi = chunks[-1].generation_info
        assert gi is not None
        assert "tool_events" in gi
        assert len(gi["tool_events"]) == 1
        assert gi["tool_events"][0]["type"] == "tool_call"
        assert gi["tool_events"][0]["name"] == "Bash"
        assert gi["tool_events"][0]["tool_use_id"] == "toolu_combo"
        # Original keys preserved.
        assert gi["finish_reason"] == "stop"
        assert gi["total_cost_usd"] == 0.0
    finally:
        ccm.ClaudeCodeChatModel = _orig
