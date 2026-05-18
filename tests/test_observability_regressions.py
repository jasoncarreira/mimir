"""Regression tests for PR #209 — observability for silent failures.

Two narrow fixes:

1. ``saga._llm.call_llm`` warns when ``llm["provider"]`` is unknown
   (typo / stale enum value). Pre-fix it silently fell back to
   ``openai_compat`` — a typo'd "anthropic-sdk" would call
   api.openai.com under a different cost + token-accounting regime
   with no operator-visible signal.

2. The streaming dispatcher's ``parse_directives`` exception fallback
   emits a ``directive_parse_error`` event in events.jsonl. Pre-fix
   the failure was only visible as a ``log.exception`` line — and
   pre-PR-#206 there wasn't even cleaning at all (raw ``<actions>``
   markup reached users mid-stream). Belt + suspenders.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

# ── call_llm provider warning ────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_llm_warns_on_unknown_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """When ``llm["provider"]`` is something other than the documented
    three (``claude_code`` / ``anthropic`` / ``openai_compat``), the
    fallback to openai_compat must emit a warning so the operator
    can spot the typo."""
    from mimir.saga import _llm

    # Stub _call_openai_compat so we don't hit the network.
    captured: list[dict[str, Any]] = []

    def _stub_openai_compat(llm, *, prompt, max_tokens, temperature, system):
        captured.append({"llm": llm, "prompt": prompt})
        return "stub-reply"

    monkeypatch.setattr(_llm, "_call_openai_compat", _stub_openai_compat)

    with caplog.at_level(logging.WARNING, logger="mimir.saga._llm"):
        out = await _llm.call_llm(
            {"provider": "anthropic-sdk", "url": "http://stub", "api_key": "x"},
            prompt="hello",
        )

    assert out == "stub-reply"
    # The fallback path was taken.
    assert len(captured) == 1
    # A warning was emitted mentioning the bad provider name.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, (
        "expected a warning when an unknown provider falls back; "
        "got no WARNING records"
    )
    assert any("anthropic-sdk" in r.getMessage() for r in warnings), (
        f"warning did not mention the bad provider name; "
        f"got: {[r.getMessage() for r in warnings]}"
    )


@pytest.mark.asyncio
async def test_call_llm_no_warning_on_recognized_providers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """The fallback warning must NOT fire for the three documented
    provider names — including ``openai_compat`` itself."""
    from mimir.saga import _llm

    def _stub_openai_compat(llm, *, prompt, max_tokens, temperature, system):
        return ""

    monkeypatch.setattr(_llm, "_call_openai_compat", _stub_openai_compat)

    with caplog.at_level(logging.WARNING, logger="mimir.saga._llm"):
        # openai_compat — the documented default; should not warn.
        await _llm.call_llm(
            {"provider": "openai_compat", "url": "http://stub"},
            prompt="hello",
        )
        # No provider key at all — falls back to openai_compat; should not warn.
        await _llm.call_llm({"url": "http://stub"}, prompt="hello")

    # Filter warnings to the unknown-provider message specifically.
    unknown_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "unknown provider" in r.getMessage()
    ]
    assert not unknown_warnings, (
        f"unexpected 'unknown provider' warning for recognized name; "
        f"got: {[r.getMessage() for r in unknown_warnings]}"
    )


# ── streaming dispatcher directive_parse_error event ─────────────────


@pytest.mark.asyncio
async def test_streaming_dispatcher_emits_event_on_parse_directives_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """When ``parse_directives`` raises inside the plan flush, the
    dispatcher must (a) fall back to sending raw text (existing
    behavior preserved), AND (b) emit a ``directive_parse_error``
    event so the failure is visible in events.jsonl."""
    from langchain_core.messages import AIMessage
    from mimir._streaming_dispatch import StreamingAutoDispatcher

    # Stub log_event to capture emissions in-process.
    captured_events: list[dict[str, Any]] = []

    async def _capture_log_event(kind, **kwargs):
        captured_events.append({"kind": kind, **kwargs})

    # Patch the log_event referenced inside the dispatcher's import.
    import mimir.event_logger
    monkeypatch.setattr(mimir.event_logger, "log_event", _capture_log_event)

    # Force parse_directives to raise.
    def _broken_parse(_text):
        raise RuntimeError("parser blew up")

    import mimir.bridges._directives as _directives_mod
    monkeypatch.setattr(_directives_mod, "parse_directives", _broken_parse)

    class _CapBridge:
        name = "fake"
        def __init__(self) -> None:
            self.sends: list[tuple[str, str, bool]] = []

        async def send(
            self, channel_id, text, attachment_paths=None, *, final=True,
        ):
            self.sends.append((channel_id, text, final))
            class _R:
                sent = True
                message_id = "msg-1"
            return _R()

        async def react(self, *a, **kw):
            return True

    bridge = _CapBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)

    # Plan text contains an actions block — without a working parser,
    # raw markup would reach the bridge. Verify (a) the raw text is
    # what got sent (fallback preserved), (b) the event fired.
    await d.observe(AIMessage(content="hello\n<actions><react emoji=\"👍\" /></actions>"))
    await d.observe(AIMessage(
        content="",
        tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))

    # Fallback: raw text reached the bridge (because parse failed).
    assert len(bridge.sends) == 1
    sent_text = bridge.sends[0][1]
    assert "<actions>" in sent_text  # raw markup — parser failed
    # Event was emitted.
    parse_error_events = [
        e for e in captured_events if e["kind"] == "directive_parse_error"
    ]
    assert parse_error_events, (
        "expected a directive_parse_error event when parse_directives "
        "raises; got events: "
        f"{[e['kind'] for e in captured_events]}"
    )
    e = parse_error_events[0]
    assert e["where"] == "streaming_plan_flush"
    assert e["channel_id"] == "ch-1"
    assert "parser blew up" in e["error"]
    assert e["plan_chars"] > 0
