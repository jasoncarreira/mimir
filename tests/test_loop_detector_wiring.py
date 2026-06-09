"""181-J regression: ``send_message`` consults the per-turn ``LoopDetector``.

Pre-181-J the deepagents agent constructed no LoopDetector — the
send-loop circuit breaker (SPEC §7.2.4) was disarmed and the agent
could ship near-duplicate sends indefinitely. ``LoopDetector`` lives
in ``mimir/loop_detector.py`` but no call site referenced it.

Restoration ties two ends together: agent.run_turn attaches a fresh
LoopDetector to the active TurnContext for every turn; the
send_message tool fetches it via ``_context.get_current_turn()`` and
branches on the BreakerDecision.

Tests drive send_message directly with a fake ChannelRegistry +
matching TurnContext so we don't need a real LLM or live bridge.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.loop_detector import LoopDetector
from mimir.models import TurnContext
from mimir.tools import registry as tool_registry
from mimir.tools.registry import react, send_message


class _FakeBridge:
    """Minimal bridge: records ``send`` calls; returns a synthetic id."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._counter = 0

    async def send(self, channel_id: str, text: str, *, final: bool = True) -> str:
        self.sent.append((channel_id, text))
        self._counter += 1
        return f"msg-{self._counter}"

    async def react(self, channel_id: str, message_id: str | None, emoji: str) -> None:
        pass


class _FakeRegistry:
    """Minimal ChannelRegistry stand-in for the tool's lookup path."""

    def __init__(self, bridge: _FakeBridge) -> None:
        self._bridge = bridge

    def find(self, channel_id: str):
        return self._bridge


@pytest.fixture(autouse=True)
def _reset_state_after_each() -> None:
    """The module-global ``_STATE["current_channel_id"]`` leaks between
    tests; reset it so dispatching order doesn't perturb assertions."""
    yield
    tool_registry.set_current_channel_id(None)


@pytest.fixture
def fake_bridge(monkeypatch: pytest.MonkeyPatch) -> _FakeBridge:
    """Install a fake channel registry on the tools._STATE dict for the
    duration of the test. send_message reads the registry from there."""
    bridge = _FakeBridge()
    reg = _FakeRegistry(bridge)
    # Restore the previous registry after the test.
    prev = tool_registry._STATE.get("channel_registry")
    tool_registry._STATE["channel_registry"] = reg
    yield bridge
    tool_registry._STATE["channel_registry"] = prev


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace log_event in the registry module's namespace so we can
    assert on circuit-breaker events without writing events.jsonl."""
    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    # send_message imports log_event inside the function via
    # ``from ..event_logger import log_event as _log_event`` — patch
    # the source module so the import resolves to the capture.
    monkeypatch.setattr("mimir.event_logger.log_event", _capture)
    return events


def _ctx(detector: LoopDetector, channel: str = "ch-1") -> TurnContext:
    return TurnContext(
        turn_id="t-1",
        session_id=channel,
        trigger="user_message",
        channel_id=channel,
        started_at=time.monotonic(),
        loop_detector=detector,
    )


def _set_channel(channel: str = "ch-1") -> None:
    """Populate the module-global channel fallback so send_message can
    resolve the channel without an explicit ``channel_id`` arg or a
    RunnableConfig configurable. Matches what
    ``Agent.run_turn → set_current_channel_id`` does in production."""
    tool_registry.set_current_channel_id(channel)


# ─── OK / under-threshold sends pass through ───────────────────────


@pytest.mark.asyncio
async def test_sends_below_soft_limit_pass_through(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    detector = LoopDetector(soft_limit=5, hard_limit=10, similarity_threshold=0.9)
    ctx = _ctx(detector)
    _set_channel("ch-1")
    token = set_current_turn(ctx)
    try:
        out = await send_message.ainvoke({"text": "first send"})
    finally:
        reset_current_turn(token)
    assert "ok" in out.lower()
    assert fake_bridge.sent == [("ch-1", "first send")]
    # Nothing should have been logged — the detector said OK.
    kinds = [k for k, _ in captured_events]
    assert "send_message_loop_hard_stop" not in kinds
    assert "send_message_loop_warning" not in kinds


# ─── HARD_STOP refuses the send ───────────────────────────────────


@pytest.mark.asyncio
async def test_hard_stop_refuses_and_logs_event(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Force a HARD_STOP by sending the same text past the hard limit."""
    detector = LoopDetector(soft_limit=2, hard_limit=3, similarity_threshold=0.9)
    ctx = _ctx(detector)
    _set_channel("ch-1")
    token = set_current_turn(ctx)
    try:
        # First two sends: streak 1, 2 — both should pass through with
        # SOFT_WARN at streak=2 (>=soft_limit=2). Third send: streak=3
        # >= hard_limit=3 → HARD_STOP.
        await send_message.ainvoke({"text": "duplicate"})
        await send_message.ainvoke({"text": "duplicate"})
        out = await send_message.ainvoke({"text": "duplicate"})
    finally:
        reset_current_turn(token)

    # The hard-stop body is the verbatim refusal text from the tool.
    assert "hard stop" in out.lower()
    assert "refused" in out.lower()
    # The third call DID NOT reach the bridge.
    assert len(fake_bridge.sent) == 2

    # The HARD_STOP event fired with the streak count.
    kinds_with_streak = [
        (k, kw.get("streak")) for k, kw in captured_events
    ]
    assert ("send_message_loop_hard_stop", 3) in kinds_with_streak


# ─── SOFT_WARN logs once + still sends ─────────────────────────────


@pytest.mark.asyncio
async def test_soft_warn_logs_once_and_still_sends(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    """At/above soft_limit but below hard_limit: send goes through,
    one warning event fires per turn (subsequent identical sends
    re-evaluate but don't re-emit the warning)."""
    detector = LoopDetector(soft_limit=2, hard_limit=10, similarity_threshold=0.9)
    ctx = _ctx(detector)
    _set_channel("ch-1")
    token = set_current_turn(ctx)
    try:
        await send_message.ainvoke({"text": "match"})
        out2 = await send_message.ainvoke({"text": "match"})
        out3 = await send_message.ainvoke({"text": "match"})
    finally:
        reset_current_turn(token)

    # Both 2nd and 3rd sends went through (SOFT_WARN, not HARD_STOP).
    assert "ok" in out2.lower()
    assert "ok" in out3.lower()
    assert len(fake_bridge.sent) == 3
    # Exactly one warning event fires per turn.
    warns = [kw for k, kw in captured_events if k == "send_message_loop_warning"]
    assert len(warns) == 1


# ─── No detector on the context — back-compat ─────────────────────


@pytest.mark.asyncio
async def test_no_detector_on_context_does_not_crash(
    fake_bridge: _FakeBridge,
) -> None:
    """send_message must remain callable without a TurnContext or
    when the context has no loop_detector — test harnesses + the
    benchmark adapter both hit this path."""
    out = await send_message.ainvoke({"text": "no-ctx send", "channel_id": "ch-1"})
    assert "ok" in out.lower()
    assert fake_bridge.sent == [("ch-1", "no-ctx send")]


# ─── Distinct text resets the streak ──────────────────────────────


@pytest.mark.asyncio
async def test_distinct_text_resets_streak(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    """A dissimilar send breaks the near-dup chain — the next time
    we re-emit the same text, the streak should start over at 1."""
    detector = LoopDetector(soft_limit=2, hard_limit=3, similarity_threshold=0.9)
    ctx = _ctx(detector)
    _set_channel("ch-1")
    token = set_current_turn(ctx)
    try:
        await send_message.ainvoke({"text": "same text"})
        await send_message.ainvoke({"text": "same text"})
        # Distinct: similarity < 0.9 → streak resets to 1.
        await send_message.ainvoke({"text": "completely different content here"})
        # Now back to "same text" — streak should be 1 again, NOT
        # the previous 2+1=3 HARD_STOP.
        out = await send_message.ainvoke({"text": "same text"})
    finally:
        reset_current_turn(token)
    assert "ok" in out.lower()
    # We never hit HARD_STOP.
    assert not any(k == "send_message_loop_hard_stop" for k, _ in captured_events)


# ─── react bumps react_count (0.3.2 — react counts as a reply) ───────


@pytest.mark.asyncio
async def test_react_increments_react_count_on_turn(
    fake_bridge: _FakeBridge,
) -> None:
    """A successful react bumps ctx.react_count so the forgot-to-send guard
    treats a react-only acknowledgment as a delivered response (0.3.2)."""
    detector = LoopDetector(soft_limit=5, hard_limit=10, similarity_threshold=0.9)
    ctx = _ctx(detector)
    _set_channel("ch-1")
    token = set_current_turn(ctx)
    try:
        out = await react.ainvoke({"emoji": "👍", "message_id": "m-1"})
    finally:
        reset_current_turn(token)
    assert "react ok" in out.lower()
    assert ctx.react_count == 1
