"""Runtime ownership of optional tracing, including partial startup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir import runtime
from tests.test_runtime import _adapters, _config, _core, _patch_factory


@pytest.mark.asyncio
async def test_runtime_passes_one_controller_to_agent_and_closes_it_once(tmp_path, monkeypatch):
    import mimir.tracing as tracing

    events = []
    _patch_factory(monkeypatch, events)

    class Controller:
        def shutdown(self):
            events.append(("close", "tracing"))

    controller = Controller()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return controller

    monkeypatch.setattr(tracing.TraceController, "create", create)
    config = _config(tmp_path)
    config.tracing_enabled = True
    config.otlp_endpoint = "http://localhost:4318/"
    config.otel_service_name = "test-service"
    config.tracing_capture_content = False
    bundle = await runtime.create_agent_runtime(config, _core(tmp_path), _adapters(events))
    assert calls == [{"enabled": True, "endpoint": "http://localhost:4318/",
                      "service_name": "test-service", "capture_content": False}]
    assert bundle.trace_controller is controller
    assert bundle.agent.kwargs["trace_controller"] is controller
    await bundle.aclose()
    await bundle.aclose()
    assert events.count(("close", "tracing")) == 1
    assert events.index(("close", "tracing")) > events.index(("close", "saga"))


@pytest.mark.asyncio
async def test_runtime_startup_failure_closes_tracing_without_losing_original_error(
    tmp_path, monkeypatch,
):
    import mimir.agent as agent_module
    import mimir.tracing as tracing

    events = []
    _patch_factory(monkeypatch, events)
    controller = SimpleNamespace(shutdown=lambda: events.append(("close", "tracing")))
    monkeypatch.setattr(tracing.TraceController, "create", lambda **kwargs: controller)

    def fail_agent(*args, **kwargs):
        raise RuntimeError("agent construction failed")

    monkeypatch.setattr(agent_module, "Agent", fail_agent)
    config = _config(tmp_path)
    config.tracing_enabled = True
    config.otlp_endpoint = "http://localhost:4318"
    with pytest.raises(RuntimeError, match="agent construction failed"):
        await runtime.create_agent_runtime(config, _core(tmp_path), _adapters(events))
    assert events.count(("close", "tracing")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [None, "https://user:canary@example.org",
    "https://example.org/?token=canary", "https://example.org/v1/traces"])
async def test_invalid_endpoint_warns_once_without_disclosing_value_or_blocking_runtime(
    tmp_path, monkeypatch, caplog, endpoint,
):
    events = []
    _patch_factory(monkeypatch, events)
    config = _config(tmp_path)
    config.tracing_enabled = True
    config.otlp_endpoint = endpoint
    with caplog.at_level("WARNING", logger="mimir.tracing"):
        bundle = await runtime.create_agent_runtime(config, _core(tmp_path), _adapters(events))
        try:
            assert bundle.trace_controller is None
            assert bundle.agent.kwargs["trace_controller"] is None
        finally:
            await bundle.aclose()
    warnings = [r.message for r in caplog.records if r.name == "mimir.tracing"]
    assert len(warnings) == 1
    assert "canary" not in caplog.text
