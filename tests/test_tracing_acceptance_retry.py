"""Offline acceptance checks at the real turn and bounded SDK export boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mimir.models import AgentEvent
from mimir.tracing import TraceController
from tests.test_agent import _FakeAgent, _build_agent


def _agent(tmp_path, controller):
    agent = _build_agent(tmp_path, fake_agent=_FakeAgent([AIMessage(content="expected reply")]))
    agent._config = replace(agent._config, tracing_enabled=True)
    agent._trace_controller = controller
    return agent


def _event():
    return AgentEvent(trigger="user_message", channel_id="trace-channel", content="ordinary prompt")


@pytest.mark.asyncio
async def test_batch_export_failure_does_not_change_turn(tmp_path, caplog):
    """An actual SDK worker calls a failing exporter; no network or secret-bearing log."""
    secret = "ghp_" + "S" * 36
    exported = threading.Event()

    class FailingExporter:
        def export(self, spans):
            exported.set()
            raise RuntimeError(secret)

        def shutdown(self, timeout_millis=30000):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(
        FailingExporter(), max_queue_size=8, max_export_batch_size=1,
        schedule_delay_millis=10, export_timeout_millis=100,
    ))
    try:
        with caplog.at_level(logging.WARNING):
            record = await _agent(tmp_path, TraceController(provider)).run_turn(_event())
            assert record.output == "expected reply" and record.error is None
            assert exported.wait(3), "SDK batch worker never attempted the export"
        assert secret not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_saturated_batch_queue_never_blocks_turn(tmp_path, caplog):
    secret = "ghp_" + "Q" * 36
    entered = threading.Event()
    release = threading.Event()

    class BlockedExporter:
        def export(self, spans):
            entered.set()
            assert release.wait(4), "exporter not released"
            raise RuntimeError(secret)

        def shutdown(self, timeout_millis=30000):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    provider = TracerProvider()
    processor = BatchSpanProcessor(BlockedExporter(), max_queue_size=2,
                                   max_export_batch_size=1, schedule_delay_millis=10)
    provider.add_span_processor(processor)
    controller = TraceController(provider)
    try:
        first = await _agent(tmp_path / "first", controller).run_turn(_event())
        assert first.output == "expected reply"
        assert await asyncio.to_thread(entered.wait, 3)
        for index in range(6):
            with controller.turn(conversation_id=str(index), agent_name="mimir", model="fake"):
                pass
        assert len(processor._batch_processor._queue) == 2, "bounded SDK queue was not filled"
        with caplog.at_level(logging.WARNING):
            record = await asyncio.wait_for(
                _agent(tmp_path / "saturated", controller).run_turn(_event()), timeout=2,
            )
        assert record.output == first.output and record.error is None
        assert secret not in caplog.text
    finally:
        release.set()
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redact", "serialize"])
async def test_capture_failure_omits_raw_content_during_real_turn(tmp_path, monkeypatch, failure):
    from mimir import tracing

    secret = "ghp_" + "R" * 36
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    controller = TraceController(provider, capture_content=True)
    agent = _agent(tmp_path, controller)
    event = AgentEvent(trigger="user_message", channel_id="trace-channel", content=secret)

    def broken(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(tracing, "redact_payload" if failure == "redact" else "json",
                        broken if failure == "redact" else type("BadJSON", (), {"dumps": staticmethod(broken)})())
    try:
        record = await agent.run_turn(event)
        assert record.output == "expected reply" and record.error is None
        [root] = exporter.get_finished_spans()
        assert root.name == "invoke_agent"
        assert "gen_ai.input.messages" not in root.attributes
        assert "gen_ai.output.messages" not in root.attributes
        assert secret not in str(root.attributes)
    finally:
        provider.shutdown()
