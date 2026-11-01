"""Offline acceptance checks at the real turn and bounded SDK export boundaries."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
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
async def test_batch_export_failure_does_not_change_turn(tmp_path, caplog, monkeypatch):
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

    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: FailingExporter())
    controller = TraceController.create(enabled=True, endpoint="http://localhost:4318")
    assert controller is not None
    try:
        with caplog.at_level(logging.WARNING):
            record = await _agent(tmp_path, controller).run_turn(_event())
            assert record.output == "expected reply" and record.error is None
            assert controller.provider.force_flush(timeout_millis=3000)
            assert exported.wait(3), "production SDK batch worker never attempted export"
        assert secret not in caplog.text
    finally:
        controller.provider.shutdown()


@pytest.mark.asyncio
async def test_saturated_batch_queue_never_blocks_turn(tmp_path, caplog):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    secret = "ghp_" + "Q" * 36
    entered = threading.Event()
    release = threading.Event()

    class BlockedExporter:
        def export(self, spans):
            entered.set()
            assert release.wait(20), "exporter not released"
            return SpanExportResult.SUCCESS

        def shutdown(self, timeout_millis=30000):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        patch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: BlockedExporter())
        controller = TraceController.create(enabled=True, endpoint="http://localhost:4318")
    assert controller is not None
    processor = controller.provider._active_span_processor._span_processors[0]
    try:
        first = await _agent(tmp_path / "first", controller).run_turn(_event())
        assert first.output == "expected reply"
        for index in range(256):
            with controller.turn(conversation_id=str(index), agent_name="mimir", model="fake"):
                pass
        assert await asyncio.to_thread(entered.wait, 3)
        for index in range(2200):
            with controller.turn(conversation_id=str(index), agent_name="mimir", model="fake"):
                pass
        assert len(processor._batch_processor._queue) == 2048, "production SDK queue was not filled"
        with caplog.at_level(logging.WARNING):
            record = await asyncio.wait_for(
                _agent(tmp_path / "saturated", controller).run_turn(_event()), timeout=2,
            )
        assert record.output == first.output and record.error is None
        assert secret not in caplog.text
    finally:
        release.set()
        controller.provider.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_bounded_while_real_batch_export_is_stalled(tmp_path, monkeypatch, caplog):
    """The SDK worker is stuck in export, not a stubbed provider.force_flush."""
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    entered, release = threading.Event(), threading.Event()
    secret = "ghp_" + "T" * 36

    class StalledExporter:
        def export(self, spans):
            entered.set()
            release.wait(12)
            return SpanExportResult.SUCCESS

        def shutdown(self, timeout_millis=30000):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: StalledExporter())
    controller = TraceController.create(enabled=True, endpoint="http://localhost:4318")
    assert controller is not None
    try:
        record = await _agent(tmp_path, controller).run_turn(_event())
        assert record.output == "expected reply"
        # Fill the configured production batch size to wake the real SDK worker.
        for index in range(256):
            with controller.turn(conversation_id=str(index), agent_name="mimir", model="fake"):
                pass
        assert await asyncio.to_thread(entered.wait, 3)
        start = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(asyncio.to_thread(controller.shutdown), 7)
        assert time.monotonic() - start < 6.5
        assert secret not in caplog.text
    finally:
        release.set()


@pytest.mark.asyncio
async def test_raising_callback_during_real_model_invocation_keeps_reply(tmp_path, monkeypatch, caplog):
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from mimir import tracing

    secret = "ghp_" + "H" * 36

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Model(messages=iter([AIMessage(content="expected reply")]), disable_streaming=True)
    graph = create_deep_agent(model=model, tools=[], system_prompt="trace test")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = _build_agent(tmp_path, fake_agent=graph)
    agent._config = replace(agent._config, tracing_enabled=True)
    agent._trace_controller = TraceController(provider)

    def broken(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(tracing.TurnTrace, "_start", broken)
    try:
        with caplog.at_level(logging.WARNING):
            record = await agent.run_turn(_event())
        assert record.output == "expected reply" and record.error is None
        assert [span.name for span in exporter.get_finished_spans()] == ["invoke_agent"]
        assert "Tracing callback failed" in caplog.text
        assert secret not in caplog.text
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
async def test_agent_initiated_saga_rewrite_is_child_chat(tmp_path, monkeypatch, capture):
    from mimir.saga.client import SagaStore
    from tests.test_saga_store import _patch_provider_with_rewrite_flag
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    _patch_provider_with_rewrite_flag(monkeypatch, rewrite_enabled=True)
    monkeypatch.setattr("mimir.saga._config_io.resolve_llm_config", lambda section: {"model": "rewrite-fake"})
    secret = "ghp_" + "W" * 36
    calls = []

    async def fake_llm(config, *, prompt, **kwargs):
        calls.append(prompt)
        return f"rewritten visible {secret}"

    monkeypatch.setattr("mimir.saga._llm.call_llm", fake_llm)
    saga = SagaStore(db_path=tmp_path / "saga.db")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = _build_agent(tmp_path, fake_agent=_FakeAgent([AIMessage(content="expected reply")]), fake_saga=saga)
    agent._config = replace(agent._config, tracing_enabled=True)
    agent._trace_controller = TraceController(provider, capture_content=capture)
    try:
        record = await agent.run_turn(AgentEvent(
            trigger="user_message", channel_id="rewrite-channel", content=f"what about it? {secret}",
        ))
        assert record.output == "expected reply" and record.error is None
        assert len(calls) == 1, "turn must initiate the contextual Saga rewrite"
        spans = exporter.get_finished_spans()
        [root] = [span for span in spans if span.name == "invoke_agent"]
        [chat] = [span for span in spans if span.name == "chat"]
        assert chat.parent.span_id == root.context.span_id
        assert chat.attributes["gen_ai.request.model"] == "rewrite-fake"
        assert ("gen_ai.input.messages" in chat.attributes) is capture
        assert ("gen_ai.output.messages" in chat.attributes) is capture
        assert secret not in str([dict(span.attributes) for span in spans])
        if capture:
            assert "rewritten visible" in chat.attributes["gen_ai.output.messages"]
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
async def test_acp_prompt_enters_one_real_turn_root(tmp_path, capture):
    from mimir.acp import sdk
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from tests.test_agent import _refusal_test_acp

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    acp, session_id, _client = await _refusal_test_acp(tmp_path, "expected reply")
    core = acp._bundle.agent
    core._config = replace(core._config, tracing_enabled=True)
    core._trace_controller = TraceController(provider, capture_content=capture)
    secret = "ghp_" + "A" * 36
    try:
        reply = await acp.prompt(session_id, [sdk.TextContentBlock(type="text", text=f"visible {secret}")])
        assert reply.stop_reason == "end_turn"
        [root] = exporter.get_finished_spans()
        assert root.name == "invoke_agent"
        assert root.attributes["gen_ai.conversation.id"] == f"acp:{session_id}"
        assert ("gen_ai.input.messages" in root.attributes) is capture
        assert ("gen_ai.output.messages" in root.attributes) is capture
        assert secret not in str(root.attributes)
        if capture:
            assert "visible" in root.attributes["gen_ai.input.messages"]
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("owner", ["plugin", "mcp"])
async def test_agent_registered_tool_execution_is_traced(tmp_path, monkeypatch, capture, owner):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool
    from mimir.plugins.registry import PolicyRegistry
    from mimir.tool_descriptors import ResultOriginKind
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    secret = "ghp_" + "P" * 36

    @tool
    async def registered_lookup(query: str) -> str:
        """Look up a local fixture."""
        return f"visible-result {secret}"

    policy = PolicyRegistry()
    if owner == "plugin":
        policy.registrar("fixture_plugin").register_tool(
            name="registered_lookup", factory=lambda: registered_lookup,
            flow="source", tier="scope-contained", operation_class="open",
            result_domain=None, result_origin=ResultOriginKind.NONE,
            sink_category=None, sink_destination=None, sink_target_extractor=None,
            sink_payload_extractor=None, writes_code_or_state=False, profiles=(), read_backend=False,
        )
    policy.freeze()
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Model(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": "registered_lookup", "args": {"query": f"visible-query {secret}"},
            "id": "registered-call-id", "type": "tool_call",
        }]),
        AIMessage(content="expected reply"),
    ]), disable_streaming=True)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = _build_agent(tmp_path, fake_agent=_FakeAgent([]))
    monkeypatch.setattr("mimir.plugins.core.core_runtime_names", lambda surface: ())
    monkeypatch.setattr("mimir.tools.web.web_tools_enabled", lambda spec: (False, False))
    monkeypatch.setattr("mimir.tools.mcp.get_mcp_tools",
                        lambda: [registered_lookup] if owner == "mcp" else [])
    monkeypatch.setattr("mimir.subagents.build_mimir_subagents", lambda **kwargs: [])
    agent._agent = None
    agent._agent_model = model
    agent._agent_middleware = ()
    agent._config = replace(agent._config, tracing_enabled=True,
                            plugin_manager=SimpleNamespace(policy=policy, plugins=()))
    agent._trace_controller = TraceController(provider, capture_content=capture)
    monkeypatch.setattr(agent, "_current_system_prompt", lambda **kwargs: "fixture prompt")
    monkeypatch.setattr(agent, "_current_skill_sources", lambda: ())
    monkeypatch.setattr(agent, "_skill_catalog_fingerprint", lambda sources: asyncio.sleep(0, result="fixture"))
    try:
        record = await agent.run_turn(_event())
        assert record.output == "expected reply" and record.error is None
        spans = exporter.get_finished_spans()
        [root] = [s for s in spans if s.name == "invoke_agent"]
        [child] = [s for s in spans if s.name == "execute_tool" and
                   s.attributes.get("gen_ai.tool.name") == "registered_lookup"]
        assert child.attributes["gen_ai.tool.call.id"] == "registered-call-id"
        assert child.context.trace_id == root.context.trace_id
        assert child.parent.span_id == root.context.span_id or child.parent.span_id in {
            s.context.span_id for s in spans if s.name == "chat"
        }
        assert ("gen_ai.tool.call.arguments" in child.attributes) is capture
        assert ("gen_ai.tool.call.result" in child.attributes) is capture
        assert secret not in str([dict(s.attributes) for s in spans])
        if capture:
            assert "visible-query" in child.attributes["gen_ai.tool.call.arguments"]
            assert "visible-result" in child.attributes["gen_ai.tool.call.result"]
    finally:
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
