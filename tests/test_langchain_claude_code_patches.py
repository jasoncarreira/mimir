"""Tests for the runtime patches in
``mimir/_langchain_claude_code_patches.py``.

Covers the two monkey-patches:
  - ``apply_patches`` (the ``_arun`` config-kwarg fix; primary
    coverage is implicit via the rest of the suite — every tool
    invocation relies on it).
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

from mimir._langchain_claude_code_patches import (
    enrich_streaming_metadata,
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
