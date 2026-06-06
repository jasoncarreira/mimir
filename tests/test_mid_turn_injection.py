"""Tests for mimir.mid_turn_injection (issue #376, PR 1 — registry + middleware).

The middleware reads the channel id via ``get_config()`` (it can't come off
``runtime`` — see the spec / mimir's review of #589), so the before_model tests
monkeypatch ``mid_turn_injection.get_config`` to stand in for the live graph
config.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from mimir import mid_turn_injection as mti


@pytest.fixture(autouse=True)
def _clear_registry():
    mti._REGISTRY.clear()
    yield
    mti._REGISTRY.clear()


def _patch_channel(monkeypatch, channel_id):
    monkeypatch.setattr(
        mti, "get_config",
        lambda: {"configurable": {"channel_id": channel_id}},
    )


# ─── registry / inject_message ───────────────────────────────────────


def test_inject_message_injected_when_active():
    mti.register_inflight("ch1")
    assert mti.inject_message("ch1", "hello") == "injected"
    assert mti._drain("ch1") == ["hello"]


def test_inject_message_no_active_turn_when_unregistered():
    assert mti.inject_message("ch1", "hello") == "no_active_turn"


def test_deactivate_rejects_later_inject():
    mti.register_inflight("ch1")
    assert mti.deactivate("ch1") == []
    # After the turn ends, a late inject must be rejected (the routing race
    # the dispatcher will rely on in PR 2).
    assert mti.inject_message("ch1", "late") == "no_active_turn"


def test_deactivate_returns_unfolded_queue():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", "never folded")
    assert mti.deactivate("ch1") == ["never folded"]


def test_register_overwrites_stale_entry():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", "old")
    mti.register_inflight("ch1")  # a new turn on the same channel
    assert mti._drain("ch1") == []  # fresh queue, stale entry self-healed


def test_none_channel_is_a_safe_noop():
    mti.register_inflight(None)  # no crash, no entry
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
    mti.inject_message("ch1", "first")
    mti.inject_message("ch1", "second")
    mw = mti.MidTurnInjectionMiddleware()

    out = mw.before_model(state={}, runtime=None)
    assert out is not None
    msgs = out["messages"]
    assert all(isinstance(m, HumanMessage) for m in msgs)
    assert [m.content for m in msgs] == ["first", "second"]  # FIFO

    # Drained — the next boundary is a no-op.
    assert mw.before_model(state={}, runtime=None) is None


def test_before_model_reads_channel_id_from_get_config(monkeypatch):
    """Guards mimir's finding #1: the hook keys off get_config()'s channel_id,
    not the runtime, and only drains that channel's queue."""
    _patch_channel(monkeypatch, "ch-A")
    mti.register_inflight("ch-A")
    mti.inject_message("ch-A", "for A")
    mti.register_inflight("ch-B")
    mti.inject_message("ch-B", "for B")

    mw = mti.MidTurnInjectionMiddleware()
    out = mw.before_model(state={}, runtime=None)
    assert [m.content for m in out["messages"]] == ["for A"]
    # ch-B's queue is untouched by the ch-A turn.
    assert mti._drain("ch-B") == ["for B"]


def test_before_model_noop_when_get_config_unavailable(monkeypatch):
    """Outside a graph run context get_config() raises — degrade to a no-op
    rather than erroring the model call."""
    def _raise():
        raise RuntimeError("get_config() called outside of a runnable context")
    monkeypatch.setattr(mti, "get_config", _raise)
    mti.register_inflight("ch1")
    mti.inject_message("ch1", "x")
    mw = mti.MidTurnInjectionMiddleware()
    assert mw.before_model(state={}, runtime=None) is None
