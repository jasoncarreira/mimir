"""Optional, per-runtime OpenTelemetry tracing for agent turns."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mimir.otel_credentials import sensitive_otlp_endpoint
from mimir.redaction import redact_payload


OPERATION = "gen_ai.operation.name"
CONVERSATION = "gen_ai.conversation.id"
AGENT = "gen_ai.agent.name"
MODEL = "gen_ai.request.model"
TOOL = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
INPUT = "gen_ai.usage.input_tokens"
OUTPUT = "gen_ai.usage.output_tokens"
CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"

_active_turn: contextvars.ContextVar[TurnTrace | None] = contextvars.ContextVar(
    "mimir_tracing_turn", default=None
)
_log = logging.getLogger(__name__)


def trace_endpoint(base: str | None) -> str | None:
    """Accept only an HTTP(S) base URL without credential-bearing components."""
    if sensitive_otlp_endpoint(base):
        return None
    assert base is not None
    if urlsplit(base).path.rstrip("/").endswith("/v1/traces"):
        return None
    return base.rstrip("/") + "/v1/traces"


def _content(value: Any) -> str | None:
    try:
        safe = redact_payload(value)
        return json.dumps(safe, ensure_ascii=True, default=str)[:4096]
    except Exception:
        return None


def _usage(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    if not usage:
        usage = (getattr(message, "response_metadata", None) or {}).get("usage", {}) or {}
    details = usage.get("input_token_details", {}) or {}
    return {
        INPUT: usage.get("input_tokens", 0) or 0,
        OUTPUT: usage.get("output_tokens", 0) or 0,
        CACHE_READ: details.get("cache_read", 0) or 0,
        CACHE_CREATION: details.get("cache_creation", 0) or 0,
    }


class TraceController:
    """Owns a provider; never installs it as the global OTel provider."""

    def __init__(self, provider: Any, *, capture_content: bool = False) -> None:
        self.provider = provider
        self.tracer = provider.get_tracer("mimir")
        self.capture_content = capture_content

    @classmethod
    def create(
        cls, *, enabled: bool, endpoint: str | None, service_name: str = "mimir",
        capture_content: bool = False, logger: logging.Logger | None = None,
    ) -> TraceController | None:
        if not enabled:
            return None
        log = logger or _log
        target = trace_endpoint(endpoint)
        if target is None:
            log.warning("Tracing disabled: configure a valid OTLP base endpoint without credentials or /v1/traces.")
            return None
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

            class SafeExporter(SpanExporter):
                def __init__(self, delegate: SpanExporter) -> None:
                    self.delegate = delegate

                def export(self, spans: Any) -> SpanExportResult:
                    try:
                        return self.delegate.export(spans)
                    except Exception:
                        log.warning("Tracing export failed.")
                        return SpanExportResult.FAILURE

                def shutdown(self) -> None:
                    try:
                        self.delegate.shutdown()
                    except Exception:
                        log.warning("Tracing exporter shutdown failed.")

                def force_flush(self, timeout_millis: int = 30000) -> bool:
                    try:
                        return self.delegate.force_flush(timeout_millis=timeout_millis)
                    except Exception:
                        log.warning("Tracing exporter flush failed.")
                        return False

            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            exporter = SafeExporter(OTLPSpanExporter(endpoint=target, timeout=5))
            provider.add_span_processor(BatchSpanProcessor(
                exporter, max_queue_size=2048, max_export_batch_size=256,
                schedule_delay_millis=5000, export_timeout_millis=5000,
            ))
            return cls(provider, capture_content=capture_content)
        except Exception:
            log.warning("Tracing disabled: OTLP exporter unavailable.")
            return None

    @contextlib.contextmanager
    def turn(
        self, *, conversation_id: str, agent_name: str, model: str,
        input_content: Any = None,
    ) -> Iterator[TurnTrace]:
        from opentelemetry.trace import Status, StatusCode

        try:
            span = self.tracer.start_span("invoke_agent", attributes={
                OPERATION: "invoke_agent", CONVERSATION: conversation_id,
                AGENT: agent_name, MODEL: model,
            })
        except Exception:
            _log.warning("Tracing root span creation failed.")
            span = None
        turn = TurnTrace(self, span)
        if span is not None and self.capture_content and input_content is not None:
            turn._set_content(span, "gen_ai.input.messages", input_content)
        token = _active_turn.set(turn)
        try:
            yield turn
        except BaseException:
            if span is not None:
                turn._safe(lambda: span.set_status(Status(StatusCode.ERROR)))
            raise
        finally:
            _active_turn.reset(token)
            turn.finish()

    def shutdown(self) -> None:
        """Give SDK flush and shutdown at most five seconds together."""
        def close() -> None:
            try:
                self.provider.force_flush(timeout_millis=5000)
                self.provider.shutdown()
            except Exception:
                _log.warning("Tracing shutdown failed.")

        worker = threading.Thread(target=close, name="mimir-otel-shutdown", daemon=True)
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            _log.warning("Tracing shutdown timed out.")


class TurnTrace:
    """Turn-local spans and usage; callback run IDs never cross turn boundaries."""

    def __init__(self, controller: TraceController, span: Any) -> None:
        self.controller = controller
        self.span = span
        self._spans: dict[UUID, Any] = {}
        self._lock = threading.RLock()
        self._totals = {INPUT: 0, OUTPUT: 0, CACHE_READ: 0, CACHE_CREATION: 0}
        self._finished = False

    def _safe(self, action: Any) -> None:
        try:
            action()
        except Exception:
            _log.warning("Tracing callback failed.")

    def _set_content(self, span: Any, key: str, value: Any) -> None:
        content = _content(value)
        if content is not None:
            self._safe(lambda: span.set_attribute(key, content))

    def record_usage(self, message: Any, *, span: Any = None) -> None:
        values = _usage(message)
        with self._lock:
            for key, count in values.items():
                if isinstance(count, int) and count >= 0:
                    self._totals[key] += count
                    if span is not None:
                        self._safe(lambda k=key, n=count: span.set_attribute(k, n))

    def mark_error(self) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._safe(lambda: self.span.set_status(Status(StatusCode.ERROR)))

    def finish(self, *, output_content: Any = None, error: bool = False) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self.span is None:
                return
            if error:
                self.mark_error()
            if self.controller.capture_content and output_content is not None:
                self._set_content(self.span, "gen_ai.output.messages", output_content)
            for child in self._spans.values():
                self._safe(child.end)
            self._spans.clear()
            for key, count in self._totals.items():
                self._safe(lambda k=key, n=count: self.span.set_attribute(k, n))
            self._safe(self.span.end)

    def _start(self, name: str, attributes: Mapping[str, Any], parent: Any = None) -> Any:
        from opentelemetry.trace import set_span_in_context

        context = set_span_in_context(parent or self.span)
        return self.controller.tracer.start_span(name, context=context, attributes=dict(attributes))

    @contextlib.contextmanager
    def chat(self, *, model: str, input_content: Any = None) -> Iterator[Any]:
        """Caller-owned span for direct in-turn model calls outside LangGraph."""
        span = None
        if self.span is not None and not self._finished:
            try:
                span = self._start("chat", {OPERATION: "chat", MODEL: model})
            except Exception:
                _log.warning("Tracing direct span creation failed.")
        if span is not None and self.controller.capture_content and input_content is not None:
            self._set_content(span, "gen_ai.input.messages", input_content)
        try:
            yield span
        except BaseException:
            if span is not None:
                from opentelemetry.trace import Status, StatusCode

                self._safe(lambda: span.set_status(Status(StatusCode.ERROR)))
            raise
        finally:
            if span is not None:
                self._safe(span.end)

    @property
    def callback(self) -> Any:
        from langchain_core.callbacks import BaseCallbackHandler

        turn = self

        class TurnCallback(BaseCallbackHandler):
            def _begin(self, operation: str, run_id: UUID, parent_run_id: UUID | None,
                       attributes: dict[str, Any], content: Any = None) -> None:
                def start() -> None:
                    with turn._lock:
                        if turn._finished or turn.span is None:
                            return
                        parent = turn._spans.get(parent_run_id, turn.span)
                        span = turn._start(operation, {OPERATION: operation, **attributes}, parent)
                        turn._spans[run_id] = span
                        if turn.controller.capture_content and content is not None:
                            turn._set_content(span, "gen_ai.input.messages" if operation == "chat" else "gen_ai.tool.call.arguments", content)

                turn._safe(start)

            def _end(self, run_id: UUID, *, output: Any = None, error: bool = False,
                     model: bool = False) -> None:
                def end() -> None:
                    with turn._lock:
                        span = turn._spans.pop(run_id, None)
                        if span is None:
                            return
                        try:
                            if error:
                                from opentelemetry.trace import Status, StatusCode

                                span.set_status(Status(StatusCode.ERROR))
                            if model and output is not None:
                                for generation in getattr(output, "generations", ()):
                                    for item in generation:
                                        message = getattr(item, "message", None)
                                        if message is not None:
                                            turn.record_usage(message, span=span)
                            if turn.controller.capture_content and output is not None:
                                turn._set_content(span, "gen_ai.output.messages" if model else "gen_ai.tool.call.result", output)
                        finally:
                            span.end()

                turn._safe(end)

            def on_chat_model_start(self, serialized: dict, messages: Any, *, run_id: UUID,
                                    parent_run_id: UUID | None = None, **kwargs: Any) -> None:
                self._begin("chat", run_id, parent_run_id, {MODEL: str(serialized.get("name") or serialized.get("id") or "unknown")}, messages)

            def on_llm_start(self, serialized: dict, prompts: Any, *, run_id: UUID,
                             parent_run_id: UUID | None = None, **kwargs: Any) -> None:
                self.on_chat_model_start(serialized, prompts, run_id=run_id, parent_run_id=parent_run_id)

            def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
                self._end(run_id, output=response, model=True)

            def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
                self._end(run_id, error=True)

            def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID,
                              parent_run_id: UUID | None = None, **kwargs: Any) -> None:
                self._begin("execute_tool", run_id, parent_run_id, {
                    TOOL: str(serialized.get("name") or "unknown"),
                    TOOL_CALL_ID: str(kwargs.get("tool_call_id") or run_id),
                }, input_str)

            def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
                self._end(run_id, output=output)

            def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
                self._end(run_id, error=True)

        return TurnCallback()


@contextlib.contextmanager
def in_turn_chat(*, model: str, input_content: Any = None) -> Iterator[Any | None]:
    """No-op outside an active traced turn (including out-of-turn maintenance)."""
    turn = _active_turn.get()
    if turn is None:
        yield None
    else:
        with turn.chat(model=model, input_content=input_content) as span:
            yield span


def capture_chat_output(span: Any, output: Any) -> None:
    """Capture a direct call's response only while content capture is enabled."""
    turn = _active_turn.get()
    if turn is not None and span is not None and turn.controller.capture_content:
        turn._set_content(span, "gen_ai.output.messages", output)
