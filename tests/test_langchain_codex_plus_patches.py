from __future__ import annotations

import pytest

import httpx

from mimir._langchain_codex_plus_patches import (
    install_codex_plus_transient_retry_patch,
)


_TransientReadError = httpx.ReadError


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

def test_codex_plus_patches_openai_sse_decoder_tolerates_non_utf8_bytes() -> None:
    import openai._streaming as openai_streaming

    class FakeChatCodexPlus:
        async def _astream(self, *args, **kwargs):  # pragma: no cover - unused here
            yield "ok"

        def _generate(self, *args, **kwargs):  # pragma: no cover - unused here
            return "ok"

    install_codex_plus_transient_retry_patch(FakeChatCodexPlus)

    events = list(openai_streaming.SSEDecoder().iter_bytes([
        b"event: response.output_text.delta\n",
        b"data: {\"delta\": \"ok \xa7\"}\n\n",
    ]))

    assert len(events) == 1
    assert events[0].event == "response.output_text.delta"
    assert "�" in events[0].data

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
