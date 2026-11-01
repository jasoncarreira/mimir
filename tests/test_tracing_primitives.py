"""Controller-level tracing tests; turn and server wiring live in their owning slices."""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
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
    ("file:///tmp/socket", None),
    (None, None),
])
def test_base_endpoint(base, expected):
    assert trace_endpoint(base) == expected


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
