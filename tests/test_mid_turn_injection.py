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


# ─── folded_records (PR 3/4 durable visibility + timing) ─────────────


def test_drain_records_folded_records_in_order_with_timing():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("first"))
    mti.inject_message("ch1", _ev("second"))
    mti._drain("ch1")  # the fold
    recs = mti.folded_records("ch1")
    assert [e.content for e, _t in recs] == ["first", "second"]
    # Each carries a monotonic fold timestamp (float) for t_ms computation.
    assert all(isinstance(t, float) for _e, t in recs)


def test_folded_records_excludes_unfolded_leftovers():
    """Folded (drained) and pending (still queued) are disjoint: folded_records
    reports only what a before_model boundary consumed; the rest is a leftover."""
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("folded-1"))
    mti._drain("ch1")                       # first boundary folds folded-1
    mti.inject_message("ch1", _ev("leftover"))  # arrives after the last boundary
    assert [e.content for e, _t in mti.folded_records("ch1")] == ["folded-1"]
    # The unfolded one comes back from deactivate as a leftover, not folded.
    assert [e.content for e in mti.deactivate("ch1")] == ["leftover"]


def test_folded_records_empty_without_active_turn():
    assert mti.folded_records("ch1") == []   # never registered
    assert mti.folded_records(None) == []


def test_folded_records_dropped_after_deactivate():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev("x"))
    mti._drain("ch1")
    mti.deactivate("ch1")                   # turn ended → entry popped
    assert mti.folded_records("ch1") == []


# ─── defer_message / deferred_records (chainlink #384) ───────────────


def _ev_id(content: str, source_id: str, channel_id: str = "ch1") -> AgentEvent:
    return AgentEvent(
        trigger="user_message", channel_id=channel_id, content=content,
        source_id=source_id,
    )


def test_defer_message_marks_folded_message():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev_id("a true topic switch", "m1"))
    mti._drain("ch1")  # fold it
    assert mti.defer_message("ch1", "m1", "unrelated work") == "deferred"
    recs = mti.deferred_records("ch1")
    assert [(e.source_id, r) for e, r in recs] == [("m1", "unrelated work")]


def test_defer_message_not_found_for_unfolded_id():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev_id("x", "m1"))
    mti._drain("ch1")
    # Only a message actually folded into THIS turn can be deferred.
    assert mti.defer_message("ch1", "m-nope", "r") == "not_found"
    assert mti.deferred_records("ch1") == []


def test_defer_message_idempotent_keeps_first_reason():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev_id("x", "m1"))
    mti._drain("ch1")
    assert mti.defer_message("ch1", "m1", "first") == "deferred"
    assert mti.defer_message("ch1", "m1", "second") == "already_deferred"
    assert mti.deferred_records("ch1")[0][1] == "first"


def test_defer_message_no_active_turn():
    assert mti.defer_message("ch1", "m1", "r") == "no_active_turn"


def test_deferred_records_empty_without_turn():
    assert mti.deferred_records("ch1") == []
    assert mti.deferred_records(None) == []


def test_defer_message_dropped_after_deactivate():
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev_id("x", "m1"))
    mti._drain("ch1")
    mti.defer_message("ch1", "m1", "r")
    mti.deactivate("ch1")
    assert mti.deferred_records("ch1") == []
    assert mti.defer_message("ch1", "m1", "r") == "no_active_turn"


def test_defer_injected_message_tool_maps_results():
    """The defer_injected_message tool resolves the channel from config and maps
    defer_message's status to clear agent-facing strings; invalid ids fail safely."""
    from mimir.tools.registry import defer_injected_message
    cfg = {"configurable": {"channel_id": "ch1"}}
    mti.register_inflight("ch1")
    mti.inject_message("ch1", _ev_id("topic switch", "m1"))
    mti._drain("ch1")

    ok = defer_injected_message.func(message_id="m1", reason="topic switch", config=cfg)
    assert "Deferred message m1" in ok
    assert [(e.source_id, r) for e, r in mti.deferred_records("ch1")] == [("m1", "topic switch")]

    # Invalid (non-folded) id fails safely, no state change.
    bad = defer_injected_message.func(message_id="m-nope", reason="x", config=cfg)
    assert "failed: no injected message" in bad

    # No channel context fails safely.
    none_ch = defer_injected_message.func(message_id="m1", reason="x", config={})
    assert "no current channel context" in none_ch


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
    # Rendered (header + body), so check containment + FIFO order.
    assert len(msgs) == 2
    assert "first" in msgs[0].content and "second" in msgs[1].content

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
    assert len(out["messages"]) == 1 and "for A" in out["messages"][0].content
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


# ─── render_injected_message (attachments + author/msg-id) ───────────


def test_render_injected_message_includes_attachments_and_author():
    ev = AgentEvent(
        trigger="user_message", channel_id="ch1", content="look at this",
        author_display="alice", source_id="m123",
        attachment_names=["attachments/foo.png", "attachments/bar.pdf"],
    )
    rendered = mti.render_injected_message(ev)
    assert "look at this" in rendered
    assert "alice" in rendered and "msg_id: m123" in rendered
    assert "Attachments:\n- attachments/foo.png\n- attachments/bar.pdf" in rendered


def test_before_model_folds_attachments_not_just_content(monkeypatch):
    """Guards mimir's #593 finding: a mid-turn message with an attachment must
    reach the model with its attachment paths, not a text-only HumanMessage."""
    _patch_channel(monkeypatch, "ch1")
    mti.register_inflight("ch1")
    mti.inject_message("ch1", AgentEvent(
        trigger="user_message", channel_id="ch1", content="see attached",
        author_display="bob", attachment_names=["attachments/x.png"],
    ))
    mw = mti.MidTurnInjectionMiddleware()
    folded = mw.before_model(state={}, runtime=None)["messages"][0].content
    assert "see attached" in folded
    assert "Attachments:\n- attachments/x.png" in folded  # not dropped
    assert "bob" in folded
