"""Controller-level tracing tests; turn and server wiring live in their owning slices."""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from mimir.tracing import (
    CACHE_CREATION, CACHE_READ, INPUT, OUTPUT, TOOL_CALL_ID, TraceController,
    capture_chat_output, in_turn_chat, trace_endpoint,
)


@pytest.fixture
def traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    controller = TraceController(provider)
    yield controller, exporter
    provider.shutdown()


@pytest.mark.parametrize("base,expected", [
    ("https://collector.example", "https://collector.example/v1/traces"),
    ("https://collector.example/prefix/", "https://collector.example/prefix/v1/traces"),
    ("http://localhost:4318/", "http://localhost:4318/v1/traces"),
    ("https://collector.example/v1/traces", None),
    ("https://collector.example/v1/traces/", None),
    ("https://user:password@collector.example", None),
    ("https://user%40name@collector.example", None),
    ("https://collector.example/?token=abc", None),
    ("https://collector.example/#secret", None),
    ("https://collector.example:bad", None),
    ("https://collector.example:", None),
    ("file:///tmp/socket", None),
    (None, None),
])
def test_base_endpoint(base, expected, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    assert trace_endpoint(base) == expected


@pytest.mark.parametrize("carrier", [
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
])
@pytest.mark.parametrize("endpoint,allowed", [
    ("http://collector.example:4318", False),
    ("http://192.0.2.1:4318", False),
    ("http://[2001:db8::1]:4318", False),
    ("http://localhost.attacker.example:4318", False),
    ("http://127.0.0.1.attacker.example:4318", False),
    ("http://localhost:4318", True),
    ("http://LOCALHOST:4318", True),
    ("http://127.0.0.1:4318", True),
    ("http://127.23.45.67:4318", True),
    ("http://[::1]:4318", True),
    ("https://collector.example:4318", True),
])
def test_header_transport_policy(endpoint, allowed, carrier, monkeypatch, caplog):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.setenv(carrier, "x-honeycomb-team=private-canary")
    expected = endpoint + "/v1/traces" if allowed else None
    assert trace_endpoint(endpoint) == expected
    if not allowed:
        constructed = []
        monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: constructed.append(kwargs))
        with caplog.at_level(logging.WARNING, logger="mimir.tracing"):
            assert TraceController.create(enabled=True, endpoint=endpoint) is None
        assert constructed == []
        assert [r.message for r in caplog.records if r.name == "mimir.tracing"] == [
            "Tracing disabled: configure a valid OTLP base endpoint without credentials or /v1/traces."
        ]
        assert "private-canary" not in caplog.text
        assert endpoint not in caplog.text


def test_headerless_remote_http_collector_is_allowed(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    assert trace_endpoint("http://collector.example:4318") == "http://collector.example:4318/v1/traces"


def test_disabled_never_imports_sdk_or_constructs_exporter(monkeypatch):
    # Assert the module's own imports, not the ambient process module registry.
    source = Path(__file__).parents[1] / "mimir" / "tracing.py"
    tree = ast.parse(source.read_text())
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                   any(alias.name.startswith("opentelemetry") for alias in
                       (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")]))
                   for node in tree.body)
    assert TraceController.create(enabled=False, endpoint="https://collector.example") is None


def test_invalid_endpoint_warns_once_without_secret(caplog):
    with caplog.at_level(logging.WARNING):
        assert TraceController.create(enabled=True, endpoint="https://user:canary@host/?key=canary") is None
    assert len(caplog.records) == 1
    assert "canary" not in caplog.text


def test_empty_port_disables_tracing_without_constructing_exporter(monkeypatch, caplog):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    constructed = []
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: constructed.append(kwargs))
    with caplog.at_level(logging.WARNING):
        controller = TraceController.create(enabled=True, endpoint="https://collector.example:")
    assert controller is None
    assert constructed == []
    assert [record.message for record in caplog.records] == [
        "Tracing disabled: configure a valid OTLP base endpoint without credentials or /v1/traces."
    ]


@pytest.mark.asyncio
async def test_batch_export_exception_does_not_log_credentials_or_fail_turn(
    tmp_path, monkeypatch, caplog,
):
    from langchain_core.messages import AIMessage
    from mimir.models import AgentEvent
    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    from tests.test_agent import _FakeAgent, _build_agent

    secret = "ghp_" + "S" * 36
    exported = threading.Event()
    attributes = []

    class FailingExporter:
        def export(self, spans):
            attributes.extend(dict(span.attributes) for span in spans)
            exported.set()
            raise RuntimeError(secret)

        def shutdown(self, timeout_millis=30000):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kwargs: FailingExporter())
    controller = TraceController.create(enabled=True, endpoint="http://localhost:4318")
    assert controller is not None
    agent = _build_agent(tmp_path, fake_agent=_FakeAgent([AIMessage(content="expected reply")]))
    agent._config = replace(agent._config, tracing_enabled=True)
    agent._trace_controller = controller
    try:
        with caplog.at_level(logging.WARNING):
            record = await agent.run_turn(AgentEvent(
                trigger="user_message", channel_id="trace-channel", content="ordinary prompt",
            ))
            assert record.output == "expected reply" and record.error is None
            assert controller.provider.force_flush(timeout_millis=3000)
            assert exported.wait(3)
        assert secret not in caplog.text
        assert "Tracing export failed." in caplog.text
        assert attributes and secret not in str(attributes)
    finally:
        controller.provider.shutdown()


def test_turn_hierarchy_usage_and_direct_helper(traces):
    controller, exporter = traces
    run_id, tool_id = uuid4(), uuid4()
    with controller.turn(conversation_id="channel/session", agent_name="mimir", model="fake") as turn:
        callback = turn.callback
        callback.on_chat_model_start({"name": "fake"}, [["prompt"]], run_id=run_id)
        callback.on_tool_start({"name": "search"}, "query", run_id=tool_id,
                               parent_run_id=run_id, tool_call_id="call-1")
        callback.on_tool_end("result", run_id=tool_id)

        class Message:
            usage_metadata = {"input_tokens": 12, "output_tokens": 3,
                              "input_token_details": {"cache_read": 5, "cache_creation": 2}}

        class Generation:
            message = Message()

        class Result:
            generations = [[Generation()]]

        callback.on_llm_end(Result(), run_id=run_id)
        with in_turn_chat(model="rewrite"):
            pass
    with in_turn_chat(model="outside") as span:
        assert span is None
        capture_chat_output(span, "not recorded")
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == ["chat", "chat", "execute_tool", "invoke_agent"]
    spans = {span.name if span.name != "chat" else span.attributes["gen_ai.request.model"]: span
             for span in spans}
    parent = spans["invoke_agent"]
    assert spans["fake"].parent.span_id == parent.context.span_id
    assert spans["rewrite"].parent.span_id == parent.context.span_id
    assert spans["execute_tool"].parent.span_id == spans["fake"].context.span_id
    assert spans["execute_tool"].attributes[TOOL_CALL_ID] == "call-1"
    assert {key: parent.attributes[key] for key in (INPUT, OUTPUT, CACHE_READ, CACHE_CREATION)} == {
        INPUT: 12, OUTPUT: 3, CACHE_READ: 5, CACHE_CREATION: 2,
    }
    assert not any("messages" in key or "arguments" in key or "result" in key
                   for span in spans.values() for key in span.attributes)


def test_capture_redacts_before_export(traces):
    controller, exporter = traces
    controller.capture_content = True
    with controller.turn(conversation_id="safe", agent_name="mimir", model="fake",
                         input_content={"api_key": "canary", "text": "hello"}) as turn:
        callback = turn.callback
        run_id = uuid4()
        callback.on_tool_start({"name": "search"}, "hello", run_id=run_id)
        callback.on_tool_end({"api_key": "canary", "text": "hello"}, run_id=run_id)
        with in_turn_chat(model="helper", input_content={"api_key": "canary", "text": "hello"}) as span:
            capture_chat_output(span, {"api_key": "canary", "text": "hello"})
    values = str([dict(span.attributes) for span in exporter.get_finished_spans()])
    assert "hello" in values
    assert "canary" not in values


def test_errors_are_status_only_and_turn_outcome_unchanged(traces):
    controller, exporter = traces
    with pytest.raises(ValueError, match="secret-value"):
        with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
            callback = turn.callback
            run_id = uuid4()
            callback.on_llm_start({"name": "fake"}, ["prompt"], run_id=run_id)
            callback.on_llm_error(ValueError("secret-value"), run_id=run_id)
            raise ValueError("secret-value")
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(span.status.status_code == StatusCode.ERROR for span in spans)
    assert "secret-value" not in str([dict(span.attributes) for span in spans])


def test_callback_failure_does_not_change_turn(traces, monkeypatch, caplog):
    controller, exporter = traces
    with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
        def fail(*args, **kwargs):
            raise RuntimeError("private-canary")

        monkeypatch.setattr(turn, "_start", fail)
        turn.callback.on_tool_start({"name": "search"}, "input", run_id=uuid4())
    assert [span.name for span in exporter.get_finished_spans()] == ["invoke_agent"]
    assert "private-canary" not in caplog.text


def test_root_start_failure_preserves_turn_outcome(traces, monkeypatch, caplog):
    controller, exporter = traces

    def fail(*args, **kwargs):
        raise RuntimeError("private-canary")

    monkeypatch.setattr(controller.tracer, "start_span", fail)
    with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
        turn.callback.on_tool_start({"name": "search"}, "input", run_id=uuid4())
        with in_turn_chat(model="rewrite") as span:
            assert span is None
    with pytest.raises(ValueError, match="original failure"):
        with controller.turn(conversation_id="safe", agent_name="mimir", model="fake"):
            raise ValueError("original failure")
    assert exporter.get_finished_spans() == ()
    assert "private-canary" not in caplog.text


def test_direct_start_failure_preserves_turn_outcome(traces, monkeypatch, caplog):
    controller, exporter = traces
    with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
        def fail(*args, **kwargs):
            raise RuntimeError("private-canary")

        monkeypatch.setattr(turn, "_start", fail)
        with in_turn_chat(model="rewrite") as span:
            assert span is None
        with pytest.raises(ValueError, match="original failure"):
            with in_turn_chat(model="rewrite"):
                raise ValueError("original failure")
    assert [span.name for span in exporter.get_finished_spans()] == ["invoke_agent"]
    assert "private-canary" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_turn_sets_error_and_ends_span(traces):
    controller, exporter = traces
    entered = asyncio.Event()

    async def run():
        with controller.turn(conversation_id="safe", agent_name="mimir", model="fake"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        [span] = exporter.get_finished_spans()
        assert span.name == "invoke_agent"
        assert span.status.status_code == StatusCode.ERROR
        assert span.end_time is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_direct_chat_sets_error_and_ends_both_spans(traces):
    controller, exporter = traces
    entered = asyncio.Event()

    async def run():
        with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
            with turn.chat(model="direct"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(run())
    try:
        # Cancel only after the task is blocked inside the direct chat context.
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == ["chat", "invoke_agent"]
        chat, parent = spans
        assert chat.parent.span_id == parent.context.span_id
        assert chat.context.trace_id == parent.context.trace_id
        assert chat.status.status_code == StatusCode.ERROR
        assert chat.end_time is not None
        assert parent.status.status_code == StatusCode.ERROR
        assert parent.end_time is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("broken", [False, True])
def test_usage_observer_preserves_accounting_and_is_turn_local(traces, broken):
    from types import SimpleNamespace

    controller, exporter = traces
    observed = []
    message = SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 3})

    def observe(value):
        observed.append(value)
        if broken:
            raise RuntimeError("observer failure")

    with controller.turn(conversation_id="first", agent_name="mimir", model="fake") as turn:
        turn.set_usage_observer(observe)
        turn.record_usage(message)
        assert "record_usage" not in vars(turn)
    with controller.turn(conversation_id="second", agent_name="mimir", model="fake") as turn:
        turn.record_usage(message)
    assert observed == [message]
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(span.attributes[INPUT] == 12 and span.attributes[OUTPUT] == 3 for span in spans)


def test_callback_delayed_after_cancelled_turn_does_not_start_span(traces):
    controller, exporter = traces
    ready = threading.Event()
    release = threading.Event()
    done = threading.Event()
    failures = []
    with pytest.raises(asyncio.CancelledError):
        with controller.turn(conversation_id="safe", agent_name="mimir", model="fake") as turn:
            callback = turn.callback
            run_id = uuid4()

            def delayed_start():
                ready.set()
                if not release.wait(5):
                    failures.append("callback was not released")
                else:
                    try:
                        callback.on_tool_start({"name": "search"}, "input", run_id=run_id)
                    except Exception as exc:
                        failures.append(exc)
                done.set()

            worker = threading.Thread(target=delayed_start)
            worker.start()
            assert ready.wait(5)
            raise asyncio.CancelledError()
    release.set()
    assert done.wait(5)
    worker.join(timeout=5)
    assert not failures
    assert not turn._spans
    assert [span.name for span in exporter.get_finished_spans()] == ["invoke_agent"]


def test_shutdown_contains_exporter_failure(caplog):
    class FailingProvider:
        def force_flush(self, *, timeout_millis):
            assert timeout_millis == 5000
            raise RuntimeError("private-canary")

    controller = TraceController.__new__(TraceController)
    controller.provider = FailingProvider()
    controller.shutdown()
    assert "private-canary" not in caplog.text
    assert "Tracing shutdown failed" in caplog.text
