"""Dashboard sink telemetry must preserve the census without blocking the loop."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from mimir import event_logger
from mimir.access_control import SinkGate
from mimir.harness_egress import harness_sink_allowed
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    SourceLabel,
    TurnInteractivity,
)
from mimir.turn_event_bus import TurnEventBus


@pytest.fixture
def private_labels() -> InformationFlowLabels:
    return InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"web-private"}),
        sources=(SourceLabel(
            principal="alice", domain="channel", resource_id="web-private",
            bridge_instance="web", sensitivity="private",
            authorized_principals=frozenset({"alice"}),
        ),),
    )


@pytest.fixture
def owned_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> event_logger.EventLogger:
    logger = event_logger.EventLogger(tmp_path / "events.jsonl", "egress-test")
    monkeypatch.setattr(event_logger, "_logger", logger)
    return logger


@pytest.mark.parametrize("enforced", [False, True], ids=["shadow", "enforced"])
async def test_dashboard_sink_census_is_nonblocking_under_io_contention(
    enforced: bool,
    private_labels: InformationFlowLabels,
    owned_logger: event_logger.EventLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The carrier, rather than the process fallback, owns this turn's mode.
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", str(not enforced).lower())
    auth = AuthContext(
        principal="alice", canonical_principal="alice", roles=(),
        event_ingress=None, trigger="user_message", channel_id="web-alice",
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=enforced, domain="channel",
        resource_id="web-alice", bridge_instance="web",
    )
    decision = SinkGate.check_sink_flow(
        "web_turn_events", "web-alice", private_labels, auth, enforce=enforced,
    )
    assert decision.allowed is (not enforced)
    assert decision.is_shadow_decision is (not enforced)
    assert decision.reason == "ifc_label_blocked:same_channel"

    bus = TurnEventBus()
    subscribers = [bus.subscribe("web-alice"), bus.subscribe("*")]
    held = threading.Event()
    release = threading.Event()
    watchdog_fired = threading.Event()

    def hold_io_lock() -> None:
        with owned_logger._io_lock:
            held.set()
            # An asyncio timeout cannot rescue a loop blocked in log_sync.
            if not release.wait(5):
                watchdog_fired.set()

    holder = threading.Thread(target=hold_io_lock)
    holder.start()
    heartbeats: list[int] = []
    results: list[bool] = []
    refusals: list[str] = []
    try:
        assert await asyncio.to_thread(held.wait, 2)
        for seq in range(40):
            bus.publish({
                "type": "tool_result", "phase": "chunk", "turn_id": "private-turn",
                "channel_id": "web-alice", "seq": seq,
                "content_delta": "private output",
                "_ifc_labels": private_labels, "_auth_context": auth,
            })
            for subscriber in subscribers:
                event = subscriber.get_nowait()
                assert event["seq"] == seq
                results.append(harness_sink_allowed(
                    "web_turn_events", event["channel_id"],
                    event["_ifc_labels"], event["_auth_context"],
                    on_refusal=refusals.append,
                ))
            asyncio.get_running_loop().call_soon(heartbeats.append, seq)
            await asyncio.sleep(0)
            assert heartbeats == list(range(seq + 1))
            assert not watchdog_fired.is_set(), "sink logging blocked the event loop"
            assert owned_logger._io_lock.locked()
        assert results == [not enforced] * 80
        assert refusals == ([decision.reason] * 80 if enforced else [])
    finally:
        release.set()
        await asyncio.to_thread(holder.join)
        bus.unsubscribe("web-alice", subscribers[0])
        bus.unsubscribe("*", subscribers[1])
        await asyncio.to_thread(owned_logger.flush_sync)

    records = [json.loads(line) for line in owned_logger._path.read_text().splitlines()]
    assert len(records) == 80
    for record in records:
        assert record.pop("timestamp")
        assert record == {
            "type": "sink_blocked", "session_id": "egress-test",
            "sink": "web_turn_events", "sink_category": "same_channel",
            "target_channel": "web-alice", "reason": decision.reason,
            "allowed": not enforced,
            "status": "denied" if enforced else "would_block",
            "enforcement_enabled": enforced, "is_shadow_decision": not enforced,
        }


@pytest.mark.parametrize("refusal_raises", [False, True])
async def test_dashboard_enforced_denial_survives_telemetry_failure(
    private_labels: InformationFlowLabels,
    owned_logger: event_logger.EventLogger,
    monkeypatch: pytest.MonkeyPatch,
    refusal_raises: bool,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    decision = SinkGate.check_sink_flow(
        "web_turn_events", "web-alice", private_labels, None, enforce=True,
    )
    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:same_channel"
    refusals: list[str] = []
    attempts: list[str] = []

    def refuse(reason: str) -> None:
        refusals.append(reason)
        if refusal_raises:
            raise RuntimeError("refusal reporting unavailable")

    def fail_logging(event_type: str, **payload: object) -> None:
        attempts.append(event_type)
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(owned_logger, "log_sync", fail_logging)
    # Audit fields alone cannot detect an accidentally fail-open final return.
    assert harness_sink_allowed(
        "web_turn_events", "web-alice", private_labels, None, on_refusal=refuse,
    ) is False
    assert refusals == [decision.reason]
    assert attempts == ["sink_blocked"]
