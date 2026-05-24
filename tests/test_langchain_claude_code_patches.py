"""Tests for the runtime patches in
``mimir/_langchain_claude_code_patches.py``.

Covers the monkey-patches:
  - ``apply_patches``:
    - ``_get_tool_schema`` fix: uses ``tool_call_schema`` (excludes
      ``InjectedToolArg`` params like ``config``) instead of
      ``args_schema`` (includes them). Prevents the MCP schema from
      exposing ``config`` to Claude Code, which caused the "got multiple
      values for keyword argument 'config'" collision on
      ``send_message``, ``react``, and ``fetch_channel_history``.
    - ``_wrap_langchain_tool`` fix: passes ``config=RunnableConfig()``
      to ``_arun`` (langchain-core 1.x required-kwarg). Also strips
      ``config`` from caller ``args`` defensively.
  - ``enrich_streaming_metadata`` (preserves ``stop_reason`` /
    ``num_turns`` / ``is_error`` on the result chunk that upstream
    ``_astream`` drops).

The deepagents-base-prompt strip is covered separately in
``test_prompts.py`` via its observable effect on
``build_system_prompt``'s output.
"""
from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

# Skip the entire module if the optional ``langchain-claude-code`` fork
# isn't installed. ``pip install mimir-agent`` doesn't pull the fork by
# default (it's behind the ``claude-code`` extra); ``pip install -e
# ".[dev]"`` likewise omits it. Operators who want to develop the
# claude-code path install ``".[dev-claude-code]"`` which pulls both
# the dev toolchain and the fork. Pre-OSS hardening (review item #4).
pytest.importorskip("langchain_claude_code")

from mimir._langchain_claude_code_patches import (
    _post_tool_use_failure_hook,
    _post_tool_use_hook,
    _pre_tool_use_hook,
    _tool_events_var,
    apply_patches,
    enrich_streaming_metadata,
    install_tool_event_hooks,
)


def _make_dummy_chat_model_class() -> type:
    """Build a stand-in for ``ChatClaudeCode`` that exercises the
    same ``_astream`` shape upstream uses — an async generator that
    yields chunks, the last of which carries a ``generation_info``
    dict with ``finish_reason`` set. The original ResultMessage is
    stored on ``self._last_result`` exactly like the upstream code.

    Using a fake class instead of the real ChatClaudeCode keeps the
    test offline (no claude CLI subprocess spawn, no OAuth) and
    fully deterministic.
    """
    # We hot-swap this onto langchain_claude_code.claude_chat_model
    # so the patch function picks it up.
    import langchain_claude_code.claude_chat_model as ccm

    class _Chunk:
        def __init__(self, content: str = "", generation_info: dict | None = None):
            class _Msg:
                def __init__(self, c: str):
                    self.content = c
            self.message = _Msg(content)
            self.generation_info = generation_info

    class _FakeResultMessage:
        def __init__(
            self, stop_reason: str, num_turns: int, is_error: bool,
        ):
            self.stop_reason = stop_reason
            self.num_turns = num_turns
            self.is_error = is_error

    class _FakeChatClaudeCode:
        async def _astream(self, *args: Any, **kwargs: Any):
            # Simulate an assistant chunk + a result chunk (the
            # shape upstream emits).
            self._last_result = _FakeResultMessage(
                stop_reason="end_turn", num_turns=4, is_error=False,
            )
            yield _Chunk(content="hello", generation_info=None)
            yield _Chunk(
                content="",
                generation_info={
                    "total_cost_usd": 0.01,
                    "finish_reason": "stop",
                    # NOTE: upstream drops stop_reason/num_turns/is_error;
                    # the patch must add them back from _last_result.
                },
            )

    # Swap onto the package namespace so the patch finds it via
    # the same import path.
    _orig = ccm.ClaudeCodeChatModel
    ccm.ClaudeCodeChatModel = _FakeChatClaudeCode
    return _FakeChatClaudeCode, _orig


def _restore_chat_model(orig: type) -> None:
    import langchain_claude_code.claude_chat_model as ccm
    ccm.ClaudeCodeChatModel = orig


def _clear_patch_marker(cls: type) -> None:
    """Re-apply-ability — wipe the marker so patch can rerun on the
    new fake class. Each test uses its own fake class anyway."""
    if hasattr(cls, "_mimir_streaming_metadata_enriched"):
        delattr(cls, "_mimir_streaming_metadata_enriched")


@pytest.mark.asyncio
async def test_enrich_streaming_metadata_preserves_result_message_fields():
    """The patch wraps ``_astream``: any result chunk (identified by
    ``finish_reason`` in generation_info) gets enriched with
    ``stop_reason`` / ``num_turns`` / ``is_error`` pulled from the
    instance's ``_last_result``. Existing keys are not overwritten."""
    fake_cls, orig = _make_dummy_chat_model_class()
    try:
        _clear_patch_marker(fake_cls)
        enrich_streaming_metadata()

        instance = fake_cls()
        chunks = [c async for c in instance._astream()]

        # First chunk is text, no generation_info — untouched.
        assert chunks[0].generation_info is None
        assert chunks[0].message.content == "hello"

        # Second chunk is the result chunk — should have all three
        # fields copied over from _last_result.
        gi = chunks[1].generation_info
        assert gi is not None
        assert gi["finish_reason"] == "stop"   # original key preserved
        assert gi["total_cost_usd"] == 0.01    # original key preserved
        assert gi["stop_reason"] == "end_turn" # NEW — from _last_result
        assert gi["num_turns"] == 4            # NEW — from _last_result
        assert gi["is_error"] is False         # NEW — from _last_result
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_enrich_streaming_metadata_does_not_overwrite_existing():
    """If upstream eventually starts emitting these fields directly
    (or a future test/caller has already set them), the patch must
    NOT clobber the existing value."""
    fake_cls, orig = _make_dummy_chat_model_class()
    try:
        _clear_patch_marker(fake_cls)

        # Override _astream to pre-populate the fields in generation_info.
        original_astream = fake_cls._astream

        async def _astream_with_existing(self, *a, **kw):  # type: ignore[no-untyped-def]
            class _FakeRM:
                stop_reason = "max_turns"
                num_turns = 99
                is_error = True
            self._last_result = _FakeRM()
            # Yield a result chunk that already has stop_reason set
            # (simulating an upstream fix or a different code path).
            class _C:
                def __init__(self):
                    class _M: content = ""
                    self.message = _M()
                    self.generation_info = {
                        "finish_reason": "stop",
                        "stop_reason": "end_turn",  # pre-existing, should win
                    }
            yield _C()

        fake_cls._astream = _astream_with_existing
        enrich_streaming_metadata()

        instance = fake_cls()
        chunks = [c async for c in instance._astream()]
        gi = chunks[0].generation_info
        # Pre-existing stop_reason is preserved (NOT overwritten).
        assert gi["stop_reason"] == "end_turn"
        # Other fields, not pre-set, ARE filled in by the patch.
        assert gi["num_turns"] == 99
        assert gi["is_error"] is True
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_enrich_streaming_metadata_idempotent():
    """Re-applying the patch is a no-op — the marker prevents double-
    wrapping (which would cause N nested wrappers across N calls)."""
    fake_cls, orig = _make_dummy_chat_model_class()
    try:
        _clear_patch_marker(fake_cls)
        original = fake_cls._astream
        enrich_streaming_metadata()
        after_first = fake_cls._astream
        # Marker should be set; the wrap replaced the method.
        assert fake_cls._mimir_streaming_metadata_enriched is True
        assert after_first is not original
        # Second call must NOT re-wrap.
        enrich_streaming_metadata()
        after_second = fake_cls._astream
        assert after_second is after_first
    finally:
        _restore_chat_model(orig)


@pytest.mark.asyncio
async def test_enrich_streaming_metadata_safe_without_last_result():
    """If ``_last_result`` was never set (e.g. the SDK errored before
    yielding ResultMessage), the patch must not raise — it just leaves
    generation_info as-is."""
    fake_cls, orig = _make_dummy_chat_model_class()
    try:
        _clear_patch_marker(fake_cls)

        async def _astream_no_result(self, *a, **kw):  # type: ignore[no-untyped-def]
            # Deliberately no _last_result set.
            class _C:
                def __init__(self):
                    class _M: content = ""
                    self.message = _M()
                    self.generation_info = {"finish_reason": "error"}
            yield _C()

        fake_cls._astream = _astream_no_result
        enrich_streaming_metadata()

        instance = fake_cls()
        chunks = [c async for c in instance._astream()]
        gi = chunks[0].generation_info
        # finish_reason survives; no new fields added; no exception.
        assert gi == {"finish_reason": "error"}
    finally:
        _restore_chat_model(orig)


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
    import langchain_claude_code.claude_chat_model as ccm

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
    """True iff ``claude`` is on PATH and reports a version. The SDK
    spawns this binary as a subprocess; absence means the integration
    test is meaningless."""
    import shutil
    import subprocess
    if not shutil.which("claude"):
        return False
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.mark.skipif(
    not _claude_cli_available(),
    reason="claude CLI not available — integration test for built-in "
           "tool hook coverage requires the real subprocess",
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


# ── combined-patch interaction (enrich + hooks both applied) ────────


@pytest.mark.asyncio
async def test_enrich_and_hooks_both_land_on_final_chunk():
    """When ``enrich_streaming_metadata`` AND ``install_tool_event_hooks``
    are both applied (the actual production wiring — see ``agent.py``'s
    import-time patch invocations), the final result chunk must carry
    BOTH the ResultMessage enrichments (``stop_reason``/``num_turns``/
    ``is_error``) AND the captured ``tool_events`` list.

    Both patches wrap ``_astream``. Order of application matters: the
    one applied LAST runs first (its wrapper sees the other's wrapped
    method as ``_orig_astream``). Either ordering should yield the
    same final result chunk if both patches' mutations are purely
    additive (they are), but this test pins that invariant.
    """
    import langchain_claude_code.claude_chat_model as ccm
    from mimir._langchain_claude_code_patches import (
        enrich_streaming_metadata,
        install_tool_event_hooks,
    )

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

    class _FakeResult:
        stop_reason = "max_turns"
        num_turns = 7
        is_error = False

    class _FakeChatClaudeCode:
        def _build_options(self, **_kw):
            return _FakeOptions()

        async def _aquery(self, *args, **kwargs):
            # Hooks patch wraps this method too; provide a stub so the
            # patch can grab it. Not exercised by this test (we only
            # iterate _astream).
            return "", [], {}

        async def _astream(self, *args, **kwargs):
            self._last_result = _FakeResult()
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
        _clear_patch_marker(_FakeChatClaudeCode)
        _clear_tool_event_marker(_FakeChatClaudeCode)

        # Apply BOTH patches. Order chosen to match the production
        # wiring in ``agent.py``: enrich first, hooks second. The
        # outer (hooks) wrapper runs first; it must call through to
        # enrich's wrapper which then calls the original.
        enrich_streaming_metadata()
        install_tool_event_hooks()

        instance = _FakeChatClaudeCode()
        chunks = [c async for c in instance._astream()]

        # First chunk is text — untouched by either patch.
        assert chunks[0].generation_info is None
        assert chunks[0].message.content == "streaming text"

        # Final chunk MUST have both enriched ResultMessage fields...
        gi = chunks[-1].generation_info
        assert gi is not None
        assert gi["stop_reason"] == "max_turns"  # from enrich
        assert gi["num_turns"] == 7               # from enrich
        assert gi["is_error"] is False            # from enrich
        # ...AND the captured tool_events list.
        assert "tool_events" in gi                # from hooks
        assert len(gi["tool_events"]) == 1
        assert gi["tool_events"][0]["type"] == "tool_call"
        assert gi["tool_events"][0]["name"] == "Bash"
        assert gi["tool_events"][0]["tool_use_id"] == "toolu_combo"
        # Original keys preserved.
        assert gi["finish_reason"] == "stop"
        assert gi["total_cost_usd"] == 0.0
    finally:
        ccm.ClaudeCodeChatModel = _orig


# ── apply_patches: _get_tool_schema fix ────────────────────────────
#
# The root cause of the algedonic-tool config-collision:
#   _get_tool_schema used args_schema.model_json_schema() which INCLUDES
#   InjectedToolArg params like `config`. Claude Code saw `config` in the
#   MCP schema, passed it in args, and _wrap_langchain_tool then also
#   passed config=RunnableConfig() → "got multiple values for 'config'".
#
# The fix: _get_tool_schema now uses tool_call_schema.model_json_schema()
# which correctly excludes InjectedToolArg params. The three affected tools
# are send_message, react, and fetch_channel_history.


def _make_apply_patches_fake_class() -> tuple[type, type]:
    """Build a minimal ChatClaudeCode stand-in for apply_patches tests.

    Provides the two methods apply_patches monkey-patches:
    _get_tool_schema and _wrap_langchain_tool.
    """
    import langchain_claude_code.claude_chat_model as ccm

    class _FakeChatClaudeCode:
        _tool_results_var = None

        def _get_tool_schema(self, tool: Any) -> dict[str, Any]:
            # Simulate the upstream bug: use args_schema, which includes
            # InjectedToolArg params.
            if hasattr(tool, "args_schema") and tool.args_schema:
                try:
                    return tool.args_schema.model_json_schema()
                except Exception:
                    pass
            return {"type": "object", "properties": {}, "required": []}

        def _wrap_langchain_tool(
            self, tool: Any, schema: dict[str, Any]
        ) -> Any:
            # Upstream stub — not needed for schema tests.
            return None

    _orig = ccm.ClaudeCodeChatModel
    ccm.ClaudeCodeChatModel = _FakeChatClaudeCode
    return _FakeChatClaudeCode, _orig


def _clear_apply_patches_marker(cls: type) -> None:
    if hasattr(cls, "_mimir_arun_config_patched"):
        delattr(cls, "_mimir_arun_config_patched")


def test_apply_patches_get_tool_schema_uses_tool_call_schema() -> None:
    """After apply_patches(), _get_tool_schema must return the schema from
    tool_call_schema (which excludes InjectedToolArg fields like 'config'),
    not from args_schema (which includes them).

    Uses the real send_message, react, fetch_channel_history tools —
    the three tools with InjectedToolArg on their 'config' parameter.
    """
    from mimir.tools.registry import fetch_channel_history, react, send_message

    fake_cls, orig = _make_apply_patches_fake_class()
    try:
        _clear_apply_patches_marker(fake_cls)
        apply_patches()

        instance = fake_cls()
        for tool in (send_message, react, fetch_channel_history):
            schema = instance._get_tool_schema(tool)
            props = schema.get("properties", {})
            assert "config" not in props, (
                f"{tool.name}: 'config' must not appear in MCP schema after "
                f"apply_patches() — InjectedToolArg params should be hidden "
                f"from the caller. Got props: {list(props)}"
            )
    finally:
        import langchain_claude_code.claude_chat_model as ccm
        ccm.ClaudeCodeChatModel = orig


def test_apply_patches_get_tool_schema_args_schema_had_config() -> None:
    """Regression guard: confirm that args_schema DOES include 'config'
    (the pre-fix bug we're defending against). If this fails, InjectedToolArg
    behaviour changed upstream and the fix may be unnecessary.
    """
    from mimir.tools.registry import fetch_channel_history, react, send_message

    for tool in (send_message, react, fetch_channel_history):
        schema = tool.args_schema.model_json_schema()
        props = schema.get("properties", {})
        assert "config" in props, (
            f"{tool.name}: expected args_schema to include 'config' "
            f"(regression guard — if InjectedToolArg now excludes it from "
            f"args_schema too, the _get_tool_schema patch may be removable). "
            f"Got props: {list(props)}"
        )


def test_apply_patches_get_tool_schema_falls_back_when_no_tool_call_schema() -> None:
    """When a tool has no tool_call_schema attribute (or it is None),
    _get_tool_schema must fall back to the original (args_schema) path
    rather than returning an empty schema.

    Simulates this with a plain object that has args_schema but no
    tool_call_schema, avoiding Pydantic's read-only property restriction.
    """
    import langchain_claude_code.claude_chat_model as ccm

    class _FakeArgsSchema:
        @staticmethod
        def model_json_schema() -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            }

    class _NoCallSchemaTool:
        """Simulates a tool without tool_call_schema (e.g. older LangChain)."""
        name = "_no_call_schema"
        description = "test"
        args_schema = _FakeArgsSchema()
        # Deliberately no tool_call_schema attribute.

    fake_cls, orig = _make_apply_patches_fake_class()
    try:
        _clear_apply_patches_marker(fake_cls)
        apply_patches()

        instance = fake_cls()
        schema = instance._get_tool_schema(_NoCallSchemaTool())
        # Must fall through to args_schema, not return empty dict.
        assert isinstance(schema, dict)
        props = schema.get("properties", {})
        assert "x" in props, (
            f"Fallback to args_schema should include 'x'; got {list(props)}"
        )
    finally:
        ccm.ClaudeCodeChatModel = orig


@pytest.mark.asyncio
async def test_apply_patches_wrap_strips_config_from_args() -> None:
    """_wrap_langchain_tool's inner wrapped_tool must strip 'config' from
    caller args before passing to _arun (belt-and-suspenders for the
    _get_tool_schema fix). Even if the MCP schema somehow still includes
    'config', passing it in args must not cause 'got multiple values'.

    We bypass sdk_tool's SdkMcpTool wrapper (not directly callable) by
    mocking sdk_tool to be a transparent identity decorator so we can
    directly await the inner async function.
    """
    import langchain_claude_code.claude_chat_model as ccm
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import tool as lc_tool
    import unittest.mock as mock

    calls: list[dict[str, Any]] = []

    @lc_tool
    async def _recording_tool(x: str) -> str:
        """Records the kwargs _arun receives — should NOT include config."""
        calls.append({"x": x})
        return f"got:{x}"

    fake_cls, orig = _make_apply_patches_fake_class()
    try:
        _clear_apply_patches_marker(fake_cls)

        # Patch sdk_tool to be a transparent decorator so wrapped_tool
        # is returned as a plain async callable we can await directly.
        import claude_agent_sdk as sdk_mod
        _orig_sdk_tool = sdk_mod.tool

        def _transparent_sdk_tool(name: str, description: str, param_types: dict) -> Any:
            def _decorator(fn: Any) -> Any:
                return fn
            return _decorator

        with mock.patch.object(sdk_mod, "tool", _transparent_sdk_tool):
            apply_patches()

            instance = fake_cls()
            schema = {"properties": {
                "x": {"type": "string"},
                "config": {"type": "object"},  # simulate leaky schema
            }}
            wrapped = instance._wrap_langchain_tool(_recording_tool, schema)

            # Pass config in args — simulating pre-fix MCP call with config.
            result = await wrapped({"x": "hello", "config": {}})

        # Must succeed (no "got multiple values for 'config'" error).
        assert "is_error" not in result or not result.get("is_error"), (
            f"Expected clean result, got error: {result}"
        )
        assert "got:hello" in result["content"][0]["text"]
        # _arun received 'x' but NOT 'config' (stripped by the patch).
        assert len(calls) == 1
        assert calls[0] == {"x": "hello"}
    finally:
        ccm.ClaudeCodeChatModel = orig
