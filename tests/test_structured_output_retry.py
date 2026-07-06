from __future__ import annotations

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage
import pytest

from mimir.structured_output_retry import StructuredOutputRetryMiddleware


def _empty_structured_error() -> StructuredOutputValidationError:
    return StructuredOutputValidationError(
        "CriticFindings",
        ValueError("Native structured output expected valid JSON for CriticFindings"),
        AIMessage(content=""),
    )


def test_structured_output_retry_reinvokes_empty_native_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")
    middleware = StructuredOutputRetryMiddleware()
    request = ModelRequest(model=object(), messages=[])
    calls = 0

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _empty_structured_error()
        return ModelResponse(result=[AIMessage(content="ok")], structured_response={"ok": True})

    response = middleware.wrap_model_call(request, handler)

    assert response.structured_response == {"ok": True}
    assert calls == 3


def test_structured_output_retry_does_not_retry_schema_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")
    middleware = StructuredOutputRetryMiddleware()
    request = ModelRequest(model=object(), messages=[])
    calls = 0
    exc = StructuredOutputValidationError(
        "CriticFindings",
        ValueError("missing verdict"),
        AIMessage(content='{"summary":"missing verdict"}'),
    )

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(StructuredOutputValidationError):
        middleware.wrap_model_call(request, handler)

    assert calls == 1


@pytest.mark.asyncio
async def test_structured_output_retry_async_reinvokes_empty_native_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")
    middleware = StructuredOutputRetryMiddleware()
    request = ModelRequest(model=object(), messages=[])
    calls = 0

    async def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _empty_structured_error()
        return ModelResponse(result=[AIMessage(content="ok")], structured_response={"ok": True})

    response = await middleware.awrap_model_call(request, handler)

    assert response.structured_response == {"ok": True}
    assert calls == 2
