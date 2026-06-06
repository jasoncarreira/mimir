"""Tests for mimir.mid_turn_injection (issue #376) — registry + middleware.

The middleware reads the channel id via ``get_config()`` (it can't come off the
``runtime`` arg — see the spec / mimir's #589 review), so the before_model tests
monkeypatch ``mid_turn_injection.get_config``. PR 2 stores whole ``AgentEvent``s
(not just text) so an un-folded leftover re-enqueues faithfully; tests assert on
the folded ``HumanMessage`` content.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from mimir import mid_turn_injection as mti
from mimir.models import AgentEvent


@pytest.fixture(autouse=True)
def _clear_registry():
    mti._REGISTRY.clear()
    yield
    mti._REGISTRY.clear()


def _ev(content: str, channel_id: str = "ch1") -> AgentEvent:
    return AgentEvent(trigger="user_message", channel_id=channel_id, content=content)


def _patch_channel(monkeypatch, channel_id):
    monkeypatch.setattr(
        mti, "get_config",
        lambda: {"configurable": {"channel_id": channel_id}},
    )


# ─── registry / inject_message ───────────────────────────────────────


def test_inject_message_injected_when_active():
    mti.register_inflight("ch1")
    assert mti.inject_message("ch1", _ev("hello")) == "injected"
    assert [e.content for e in mti._drain("ch1")] == ["hello"]


def test_inject_message_no_active_turn_when_unregistered():
    assert mti.inject_message("ch1", _ev("hello")) == "no_active_turn"


def test_deactivate_rejects_later_inject():
    mti.register_inflight("ch1")
    assert mti.deactivate("ch1") == []
    # After the turn ends, a late inject must be rejected (the routing race the
    # dispatcher relies on).
    assert mti.inject_message("ch1", _ev("late")) == "no_active_turn"


def test_deactivate_returns_unfolded_leftover_events():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("never folded"))
    leftovers = mti.deactivate("ch1")
    # Whole events come back so run_turn can re-enqueue them faithfully.
    assert [e.content for e in leftovers] == ["never folded"]
    assert all(isinstance(e, AgentEvent) for e in leftovers)


def test_register_overwrites_stale_entry():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("old"))
    mti.register_inflight("ch1")  # a new turn on the same channel
    assert mti._drain("ch1") == []  # fresh queue, stale entry self-healed


def test_none_channel_is_a_safe_noop():
    mti.register_inflight(None)
    assert mti.deactivate(None) == []
    assert mti._drain(None) == []


# ─── MidTurnInjectionMiddleware.before_model ─────────────────────────


def test_before_model_noop_on_empty_queue(monkeypatch):
    _patch_channel(monkeypatch, "ch1")
    mti.register_inflight("ch1")
    mw = mti.MidTurnInjectionMiddleware()
    assert mw.before_model(state={}, runtime=None) is None


def test_before_model_folds_queued_messages_fifo(monkeypatch):
    _patch_channel(monkeypatch, "ch1")
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("first"))
    mti.inject_message("ch1", _ev("second"))
    mw = mti.MidTurnInjectionMiddleware()

    out = mw.before_model(state={}, runtime=None)
    assert out is not None
    msgs = out["messages"]
    assert all(isinstance(m, HumanMessage) for m in msgs)
    assert [m.content for m in msgs] == ["first", "second"]  # FIFO

    assert mw.before_model(state={}, runtime=None) is None  # drained → no-op


def test_before_model_reads_channel_id_from_get_config(monkeypatch):
    """Guards mimir's finding #1: the hook keys off get_config()'s channel_id,
    not the runtime, and only drains that channel's queue."""
    _patch_channel(monkeypatch, "ch-A")
    mti.register_inflight("ch-A")
    mti.inject_message("ch-A", _ev("for A", "ch-A"))
    mti.register_inflight("ch-B")
    mti.inject_message("ch-B", _ev("for B", "ch-B"))

    mw = mti.MidTurnInjectionMiddleware()
    out = mw.before_model(state={}, runtime=None)
    assert [m.content for m in out["messages"]] == ["for A"]
    # ch-B's queue is untouched by the ch-A turn.
    assert [e.content for e in mti._drain("ch-B")] == ["for B"]


def test_before_model_noop_when_get_config_unavailable(monkeypatch):
    """Outside a graph run context get_config() raises — degrade to a no-op."""
    def _raise():
        raise RuntimeError("get_config() called outside of a runnable context")
    monkeypatch.setattr(mti, "get_config", _raise)
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("x"))
    mw = mti.MidTurnInjectionMiddleware()
    assert mw.before_model(state={}, runtime=None) is None
