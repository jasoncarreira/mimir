"""Tests for the TurnHook chain re-introduced in PR #213.

The SDK-era hook chain was dropped during the deepagents migration
(PR #181); this PR restores it as ``mimir/turn_hooks.py`` with
``TurnHook`` abstract base + ``fire_hooks`` dispatcher.

Coverage:
- ``fire_hooks`` runs registered hooks in order
- Per-hook exception isolation — one failure doesn't stop the chain
- Exceptions emit a ``turn_hook_failed`` event
- Hooks that don't override a stage are skipped silently
- ``CommitmentExtractionHook.finalize`` parity with the migrated
  ``_maybe_extract_commitments`` (the wider contract is in
  ``tests/test_commitment_extraction_wiring.py``)
"""

from __future__ import annotations

from typing import Any

import pytest

from mimir.turn_hooks import CommitmentExtractionHook, TurnHook, fire_hooks


# ── fire_hooks dispatcher ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_hooks_calls_hooks_in_registration_order():
    """Hooks fire in the order they were registered. Important for
    finalize ordering — e.g. wiki_backlinks must run AFTER
    commitments_extraction so the wiki snapshot reflects what
    extraction wrote to state/."""
    call_order: list[str] = []

    class _A(TurnHook):
        async def finalize(self, ctx, event, record):
            call_order.append("a")

    class _B(TurnHook):
        async def finalize(self, ctx, event, record):
            call_order.append("b")

    class _C(TurnHook):
        async def finalize(self, ctx, event, record):
            call_order.append("c")

    await fire_hooks("finalize", [_A(), _B(), _C()], None, None, None)
    assert call_order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_fire_hooks_skips_hooks_without_the_stage():
    """A hook that doesn't override a stage is silently skipped —
    no error, no event. Critical: it means a hook that only cares
    about finalize doesn't have to define no-op pre_query /
    post_query stubs."""

    class _OnlyPre(TurnHook):
        async def pre_query(self, ctx, event):
            pass  # implemented

    class _OnlyFinalize(TurnHook):
        called = False

        async def finalize(self, ctx, event, record):
            type(self).called = True

    # Both hooks registered, fire only finalize — only _OnlyFinalize
    # runs; _OnlyPre is skipped without error.
    await fire_hooks(
        "finalize", [_OnlyPre(), _OnlyFinalize()], None, None, None,
    )
    assert _OnlyFinalize.called is True


@pytest.mark.asyncio
async def test_fire_hooks_exception_isolation_continues_chain(
    monkeypatch: pytest.MonkeyPatch,
):
    """A hook that raises does NOT prevent subsequent hooks from
    running. This is the load-bearing invariant — one broken hook
    can't break the whole finalize chain."""

    # Stub log_event so we don't need event_logger initialized.
    events: list[dict[str, Any]] = []

    async def _capture(kind, **kw):
        events.append({"kind": kind, **kw})

    monkeypatch.setattr("mimir.turn_hooks.log_event", _capture)

    ran_after_failure = False

    class _Boom(TurnHook):
        async def finalize(self, ctx, event, record):
            raise RuntimeError("boom from hook A")

    class _Ok(TurnHook):
        async def finalize(self, ctx, event, record):
            nonlocal ran_after_failure
            ran_after_failure = True

    await fire_hooks("finalize", [_Boom(), _Ok()], None, None, None)
    assert ran_after_failure, (
        "subsequent hook did not run after preceding hook raised; "
        "the isolation invariant is broken"
    )


@pytest.mark.asyncio
async def test_fire_hooks_emits_turn_hook_failed_event(
    monkeypatch: pytest.MonkeyPatch,
):
    """When a hook raises, ``fire_hooks`` emits a
    ``turn_hook_failed`` event with ``hook``, ``stage``, and ``error``
    fields. This is the operator-visible signal that something in the
    chain regressed — PR #210 deferred this surface to here."""
    events: list[dict[str, Any]] = []

    async def _capture(kind, **kw):
        events.append({"kind": kind, **kw})

    monkeypatch.setattr("mimir.turn_hooks.log_event", _capture)

    class _Broken(TurnHook):
        async def finalize(self, ctx, event, record):
            raise ValueError("schema mismatch")

    await fire_hooks("finalize", [_Broken()], None, None, None)
    hook_failed = [e for e in events if e["kind"] == "turn_hook_failed"]
    assert len(hook_failed) == 1
    e = hook_failed[0]
    assert e["hook"] == "_Broken"
    assert e["stage"] == "finalize"
    assert "ValueError" in e["error"]
    assert "schema mismatch" in e["error"]


@pytest.mark.asyncio
async def test_fire_hooks_event_logging_failure_does_not_break_chain(
    monkeypatch: pytest.MonkeyPatch,
):
    """If the ``turn_hook_failed`` event emission ITSELF fails (e.g.
    event_logger isn't initialized in a test path that bypassed
    ``init_logger``), the chain must still continue. Belt-and-
    suspenders for the failure-during-failure case."""
    async def _broken_log_event(kind, **kw):
        raise RuntimeError("event_logger not initialized")

    monkeypatch.setattr("mimir.turn_hooks.log_event", _broken_log_event)

    ran_after = False

    class _Boom(TurnHook):
        async def finalize(self, ctx, event, record):
            raise RuntimeError("hook fail")

    class _Ok(TurnHook):
        async def finalize(self, ctx, event, record):
            nonlocal ran_after
            ran_after = True

    # Should not raise — event-logger failure swallowed inside
    # fire_hooks' inner try.
    await fire_hooks("finalize", [_Boom(), _Ok()], None, None, None)
    assert ran_after, "chain stopped on event-logger failure"


@pytest.mark.asyncio
async def test_fire_hooks_empty_hook_list_is_a_noop():
    """No hooks → no calls, no error. Important for test paths
    that construct Agent with ``turn_hooks=[]`` explicitly to opt
    out of the default chain."""
    await fire_hooks("finalize", [], None, None, None)


# ── CommitmentExtractionHook (migrated from agent._maybe_extract_commitments) ──


@pytest.mark.asyncio
async def test_commitment_extraction_hook_skips_non_synthesis_trigger(
    monkeypatch: pytest.MonkeyPatch,
):
    """Hook is synthesis-only — non-saga_session_end triggers skip
    entirely, no LLM call, no event. Parity with the pre-#213
    ``_maybe_extract_commitments`` behavior; the wider behavioral
    contract is tested in ``test_commitment_extraction_wiring.py``."""

    class _FakeStore:
        async def add(self, rec):
            raise AssertionError("store.add must not be called for non-synthesis trigger")

        def current_state(self):
            raise AssertionError("current_state must not be called")

    extracted_called = []

    async def _extract(*args, **kwargs):
        extracted_called.append(True)
        return []

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", _extract,
    )

    hook = CommitmentExtractionHook(_FakeStore())

    class _Ctx:
        trigger = "user_message"
        channel_id = "ch-1"
        saga_session_id = "s-1"
        turn_id = "turn-1"

    class _Record:
        output = "x" * 5000

    await hook.finalize(_Ctx(), None, _Record())
    assert not extracted_called


@pytest.mark.asyncio
async def test_commitment_extraction_hook_skips_when_store_is_none():
    """No store → hook is a no-op. Tests that construct Agent without
    a CommitmentsStore (the legacy bench harnesses) shouldn't trigger
    extraction errors."""
    hook = CommitmentExtractionHook(None)

    class _Ctx:
        trigger = "saga_session_end"
        channel_id = "ch-1"
        saga_session_id = "s-1"
        turn_id = "turn-1"

    class _Record:
        output = "x" * 5000

    # Should not raise.
    await hook.finalize(_Ctx(), None, _Record())


# ── Agent.add_hook registration path ─────────────────────────────────


def test_agent_add_hook_appends_to_chain(tmp_path):
    """``Agent.add_hook(hook)`` appends to the existing hook chain
    without rebuilding the constructor's list. Muninnbot will use
    this to layer its own finalize hook on top of the default
    ``CommitmentExtractionHook`` without re-passing the whole
    list."""
    from mimir.agent import Agent
    from mimir.config import Config
    from mimir.history import MessageBuffer
    from mimir.index import IndexGenerator
    from mimir.turn_logger import TurnLogger
    import os
    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    agent = Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=cfg.home / "messages.jsonl"),
        index_generator=IndexGenerator(cfg.home),
        # Explicit empty list so we start fresh.
        turn_hooks=[],
    )
    assert agent._hooks == []

    class _Custom(TurnHook):
        pass

    h1 = _Custom()
    h2 = _Custom()
    agent.add_hook(h1)
    agent.add_hook(h2)
    assert agent._hooks == [h1, h2]
