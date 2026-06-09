"""Regression tests for PR #210 — observability for silent failures.

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
    four (``claude_code`` / ``codex_plus`` / ``anthropic`` /
    ``openai_compat``), the fallback to openai_compat must emit a
    warning so the operator can spot the typo."""
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
    """The fallback warning must NOT fire for the four documented
    provider names — including ``openai_compat`` itself."""
    from mimir.saga import _llm

    def _stub_openai_compat(llm, *, prompt, max_tokens, temperature, system):
        return ""

    async def _stub_codex_plus(llm, *, prompt, max_tokens, temperature, system):
        return ""

    monkeypatch.setattr(_llm, "_call_openai_compat", _stub_openai_compat)
    monkeypatch.setattr(_llm, "_call_codex_plus_async", _stub_codex_plus)

    with caplog.at_level(logging.WARNING, logger="mimir.saga._llm"):
        # openai_compat — the documented default; should not warn.
        await _llm.call_llm(
            {"provider": "openai_compat", "url": "http://stub"},
            prompt="hello",
        )
        # codex_plus — recognized; dispatches to ChatCodexPlus, no warn.
        await _llm.call_llm(
            {"provider": "codex_plus", "model": "gpt-5.4-mini"},
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
