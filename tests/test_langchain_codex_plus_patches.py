from __future__ import annotations

import pytest

import httpx

from mimir._langchain_codex_plus_patches import (
    install_codex_plus_transient_retry_patch,
    reset_codex_plus_stream_buffering,
    set_codex_plus_stream_buffering,
)


_TransientReadError = httpx.ReadError


class RemoteProtocolError(Exception):
    pass


class _BadRequestError(Exception):
    status_code = 400


@pytest.mark.asyncio
async def test_codex_plus_astream_retries_transient_before_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                raise _TransientReadError("stream dropped")
            yield "ok"

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    chunks = [chunk async for chunk in FakeChatCodexPlus()._astream([])]

    assert chunks == ["ok"]
    assert FakeChatCodexPlus.calls == 2


@pytest.mark.asyncio
async def test_codex_plus_astream_does_not_retry_after_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            yield "partial"
            raise _TransientReadError("stream dropped after partial output")

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    with pytest.raises(_TransientReadError):
        [chunk async for chunk in FakeChatCodexPlus()._astream([])]

    assert FakeChatCodexPlus.calls == 1


@pytest.mark.asyncio
async def test_codex_plus_astream_buffers_and_retries_mid_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")
    events = []
    monkeypatch.setattr(
        "mimir._langchain_codex_plus_patches.log_event_sync",
        lambda event_type, **payload: events.append((event_type, payload)),
    )

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            prefix = f"attempt-{type(self).calls}"
            for index in range(3):
                yield f"{prefix}-{index}"
            if type(self).calls == 1:
                raise RemoteProtocolError("stream dropped")

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)
    token = set_codex_plus_stream_buffering("poller")
    try:
        chunks = [chunk async for chunk in FakeChatCodexPlus()._astream([])]
    finally:
        reset_codex_plus_stream_buffering(token)

    assert chunks == ["attempt-2-0", "attempt-2-1", "attempt-2-2"]
    assert FakeChatCodexPlus.calls == 2
    assert events == [(
        "codex_plus_stream_retry",
        {
            "attempt": 2,
            "exception_type": "RemoteProtocolError",
            "reason": "provider_error_transient:RemoteProtocolError",
            "discarded_chunks": 3,
        },
    )]
    assert "attempt-1-0" not in caplog.text
    assert "attempt-1-0" not in repr(events)


@pytest.mark.asyncio
async def test_codex_plus_astream_buffered_non_retryable_discards_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")
    monkeypatch.setattr(
        "mimir._langchain_codex_plus_patches.log_event_sync", lambda *args, **kwargs: None,
    )

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            yield "must-not-escape"
            raise _BadRequestError("bad request")

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)
    token = set_codex_plus_stream_buffering("poller")
    observed = []
    try:
        with pytest.raises(_BadRequestError):
            async for chunk in FakeChatCodexPlus()._astream([]):
                observed.append(chunk)
    finally:
        reset_codex_plus_stream_buffering(token)

    assert observed == []
    assert FakeChatCodexPlus.calls == 1


@pytest.mark.asyncio
async def test_codex_plus_astream_buffered_exhaustion_discards_all_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")
    monkeypatch.setattr(
        "mimir._langchain_codex_plus_patches.log_event_sync", lambda *args, **kwargs: None,
    )

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            yield f"partial-{type(self).calls}"
            raise RemoteProtocolError(f"drop-{type(self).calls}")

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)
    token = set_codex_plus_stream_buffering("poller")
    observed = []
    try:
        with pytest.raises(RemoteProtocolError, match="drop-2"):
            async for chunk in FakeChatCodexPlus()._astream([]):
                observed.append(chunk)
    finally:
        reset_codex_plus_stream_buffering(token)

    assert observed == []
    assert FakeChatCodexPlus.calls == 2


@pytest.mark.asyncio
async def test_codex_plus_astream_skips_partial_json_parse_during_chunk_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langchain_core.messages.ai as ai_mod
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    def fail_if_called(s: str, *args, **kwargs):
        raise AssertionError(f"parse_partial_json should not run for Codex delta: {s!r}")

    monkeypatch.setattr(ai_mod, "parse_partial_json", fail_if_called)

    class FakeChatCodexPlus:
        async def _astream(self, *args, **kwargs):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id="response-1",
                    tool_call_chunks=[{
                        "name": None,
                        "args": '{"incomplete"',
                        "id": None,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }],
                )
            )

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    chunks = [chunk async for chunk in FakeChatCodexPlus()._astream([])]

    assert chunks[0].message.tool_call_chunks[0]["args"] == '{"incomplete"'
    assert chunks[0].message.tool_calls == []
    with pytest.raises(AssertionError):
        ai_mod.parse_partial_json("{}")

def test_codex_plus_generate_retries_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):  # pragma: no cover - unused here
            yield "ok"

        def _generate(self, *args, **kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                raise _TransientReadError("sync stream dropped")
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    assert FakeChatCodexPlus()._generate([]) == "ok"
    assert FakeChatCodexPlus.calls == 2

def test_codex_plus_patch_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "0")

    class FakeChatCodexPlus:
        calls = 0

        async def _astream(self, *args, **kwargs):
            type(self).calls += 1
            raise _TransientReadError("still down")
            yield  # pragma: no cover - makes this an async generator

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)
    first = FakeChatCodexPlus._astream
    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    assert FakeChatCodexPlus._astream is first
