"""Commitments extractor tests.

The extractor routes through saga's ``call_llm`` — same dispatch as
consolidation / query rewrite. We monkey-patch ``saga._llm.call_llm``
to return a canned string response, exercising the parser +
record-coercion pipeline without any live LLM call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mimir.commitments.extractor import (
    EXTRACTION_PROMPT_VERSION,
    _coerce_to_record,
    _parse_extraction_json,
    _strip_code_fence,
    extract_commitments,
)
from mimir.commitments.models import (
    CommitmentKind,
    CommitmentSensitivity,
)


def _install_fake_call_llm(
    monkeypatch: pytest.MonkeyPatch, response_text: str,
) -> list[dict[str, Any]]:
    """Patch ``saga._llm.call_llm`` to return *response_text*. Returns
    a list that captures every call's kwargs for assertion.
    """
    captured: list[dict[str, Any]] = []

    async def _fake_call_llm(llm, **kwargs):
        captured.append({"llm": llm, **kwargs})
        return response_text

    # The extractor imports ``call_llm`` inside its function body; patch
    # the source module so the inner import resolves to the fake.
    monkeypatch.setattr(
        "mimir.saga._llm.call_llm", _fake_call_llm, raising=True,
    )
    # Stub ``resolve_llm_config`` to a minimal fixed dict so tests don't
    # depend on saga.toml resolution. The real value gets exercised in
    # integration; here we just want a deterministic ``llm`` dict for
    # the assertion in ``test_extract_routes_through_saga_call_llm``.
    monkeypatch.setattr(
        "mimir.saga._config_io.resolve_llm_config",
        lambda _section: {"provider": "stub", "model": "stub-haiku"},
        raising=True,
    )
    return captured


# ── _strip_code_fence / _parse_extraction_json ──────────────────────


def test_strip_code_fence_removes_triple_backticks_and_json_label():
    raw = '```json\n{"commitments": []}\n```'
    assert _strip_code_fence(raw) == '{"commitments": []}'


def test_strip_code_fence_passthrough_when_no_fence():
    assert _strip_code_fence('{"x": 1}') == '{"x": 1}'


def test_parse_extraction_json_returns_dict_for_valid_payload():
    payload = json.dumps({"commitments": [{"text": "ship the doc"}]})
    out = _parse_extraction_json(payload)
    assert out is not None
    assert out["commitments"][0]["text"] == "ship the doc"


def test_parse_extraction_json_returns_none_on_malformed_input():
    assert _parse_extraction_json("not json at all") is None


def test_parse_extraction_json_unwraps_code_fence():
    payload = '```json\n{"commitments": []}\n```'
    out = _parse_extraction_json(payload)
    assert out == {"commitments": []}


# ── _coerce_to_record ───────────────────────────────────────────────


def test_coerce_returns_none_when_text_missing():
    rec = _coerce_to_record(
        {"confidence": 0.9},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is None


def test_coerce_drops_below_confidence_floor():
    rec = _coerce_to_record(
        {"text": "maybe do thing", "confidence": 0.1},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is None


def test_coerce_normalizes_unknown_kind_to_open_loop():
    rec = _coerce_to_record(
        {"text": "follow up", "confidence": 0.8, "kind": "ZZZ_unknown"},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is not None
    assert rec.kind == CommitmentKind.OPEN_LOOP


def test_coerce_channel_bound_records_attach_to_channel():
    rec = _coerce_to_record(
        {"text": "respond to thread", "confidence": 0.9, "channel_bound": True},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is not None
    assert rec.channel_id == "ch-1"


def test_coerce_channel_unbound_records_have_no_channel():
    rec = _coerce_to_record(
        {"text": "read the paper", "confidence": 0.9, "channel_bound": False},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is not None
    assert rec.channel_id is None


def test_coerce_sensitivity_default_is_routine():
    rec = _coerce_to_record(
        {"text": "task", "confidence": 0.9},
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert rec is not None
    assert rec.sensitivity == CommitmentSensitivity.ROUTINE


# ── extract_commitments (end-to-end with stub model) ────────────────


async def test_extract_short_output_skips_llm_call(monkeypatch: pytest.MonkeyPatch):
    # Patch call_llm to a sentinel that would explode if called.
    async def _explode(*args, **kwargs):
        raise AssertionError("LLM should not be called for short outputs")
    monkeypatch.setattr(
        "mimir.saga._llm.call_llm", _explode, raising=True,
    )
    out = await extract_commitments(
        "tiny",
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert out == []


async def test_extract_happy_path_returns_records(monkeypatch: pytest.MonkeyPatch):
    payload = json.dumps({
        "commitments": [
            {"text": "send the spec by Thursday", "confidence": 0.92,
             "kind": "open_loop", "channel_bound": True,
             "suggested_reminder": "follow up on the spec"},
            {"text": "review PR 164", "confidence": 0.85,
             "kind": "open_loop"},
        ]
    })
    _install_fake_call_llm(monkeypatch, payload)
    out = await extract_commitments(
        "x" * 200,  # long enough to clear the MIN_OUTPUT_LEN gate
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert len(out) == 2
    assert out[0].text == "send the spec by Thursday"
    assert out[0].channel_id == "ch-1"
    assert out[1].text == "review PR 164"
    # Prompt version is recorded for cache busting.
    assert out[0].extraction_prompt_version == EXTRACTION_PROMPT_VERSION


async def test_extract_drops_low_confidence_items(monkeypatch: pytest.MonkeyPatch):
    payload = json.dumps({
        "commitments": [
            {"text": "real follow-up", "confidence": 0.7},
            {"text": "guessing", "confidence": 0.1},
        ]
    })
    _install_fake_call_llm(monkeypatch, payload)
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert len(out) == 1
    assert out[0].text == "real follow-up"


async def test_extract_returns_empty_on_llm_failure(monkeypatch: pytest.MonkeyPatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("upstream model down")
    monkeypatch.setattr(
        "mimir.saga._llm.call_llm", _boom, raising=True,
    )
    monkeypatch.setattr(
        "mimir.saga._config_io.resolve_llm_config",
        lambda _section: {"provider": "stub", "model": "stub-haiku"},
        raising=True,
    )
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert out == []


async def test_extract_returns_empty_on_unparseable_response(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_call_llm(monkeypatch, "<<not json at all>>")
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert out == []


async def test_extract_routes_through_saga_call_llm(
    monkeypatch: pytest.MonkeyPatch,
):
    """The extractor must dispatch via ``saga._llm.call_llm``, not via
    ``langchain.chat_models.init_chat_model`` directly. Regression
    guard for the auth bug fix: pre-fix the extractor hit
    ``langchain_anthropic.ChatAnthropic`` which required
    ``ANTHROPIC_API_KEY`` — fatal on OAuth-only deploys (mimirbot)
    where the rest of saga's LLM calls route via ``claude_code``.

    Asserts (a) call_llm WAS invoked, (b) system + prompt threaded
    through as separate kwargs, (c) resolved llm config passed in as
    the ``llm`` arg, (d) temperature is 0.0 (deterministic JSON).
    """
    payload = json.dumps({
        "commitments": [
            {"text": "ship the doc by EOW", "confidence": 0.9},
        ]
    })
    captured = _install_fake_call_llm(monkeypatch, payload)

    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-route", saga_session_id="s-route",
        source_turn_id="t-route",
    )
    assert len(out) == 1
    assert out[0].text == "ship the doc by EOW"
    assert len(captured) == 1
    call = captured[0]
    # llm dict came from resolve_llm_config — provider preserved.
    assert call["llm"]["provider"] == "stub"
    # System prompt threaded as ``system`` kwarg (not folded into prompt).
    assert call["system"]
    # User prompt contains the formatted output.
    assert "x" * 50 in call["prompt"]
    # Extraction wants deterministic JSON.
    assert call["temperature"] == 0.0


async def test_extract_model_override_strips_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
):
    """When a caller passes ``model`` with a ``provider:`` prefix, only
    the model-name part feeds into the llm config — provider stays
    whatever saga resolved. Prevents callers from accidentally
    redirecting to a provider that isn't configured in saga.toml.
    """
    captured = _install_fake_call_llm(
        monkeypatch, json.dumps({"commitments": []}),
    )
    await extract_commitments(
        "x" * 200,
        channel_id="c", saga_session_id="s", source_turn_id="t",
        model="anthropic:claude-haiku-4-5",
    )
    assert captured[0]["llm"]["model"] == "claude-haiku-4-5"
    # provider untouched — still what resolve_llm_config returned.
    assert captured[0]["llm"]["provider"] == "stub"


async def test_extract_no_model_override_keeps_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
):
    """Caller passes no ``model`` → resolved config is used verbatim.
    Default path when CommitmentExtractionHook fires from the agent."""
    captured = _install_fake_call_llm(
        monkeypatch, json.dumps({"commitments": []}),
    )
    await extract_commitments(
        "x" * 200,
        channel_id="c", saga_session_id="s", source_turn_id="t",
    )
    assert captured[0]["llm"]["model"] == "stub-haiku"
    assert captured[0]["llm"]["provider"] == "stub"


# ── coercion-pipeline regression tests ─────────────────────────────
# These tests guard the JSON-parse + _coerce_to_record pipeline against
# future regressions that would silently strip artifact identifiers or
# disposition flags from whatever text the LLM emits.  They do NOT
# validate that the v4 prompt convinces the LLM to include identifiers
# (that requires a live-LLM backtest; see PR #197 body).


async def test_coercion_preserves_artifact_identifiers_when_present(
    monkeypatch: pytest.MonkeyPatch,
):
    """Artifact identifiers (PR #, chainlink #) in LLM output survive
    coercion into the CommitmentRecord unchanged.

    Guards the coercion pipeline: if the LLM emits text containing
    PR/issue/chainlink numbers, those numbers must reach
    CommitmentRecord.text intact so future evaluations don't need to
    backtrack to the source turn.
    """
    payload = json.dumps({
        "commitments": [
            {
                "text": "Cluster B subissues #115/#116/#117 under chainlink #29 unimplemented",
                "confidence": 0.9,
                "kind": "open_loop",
            }
        ]
    })
    _install_fake_call_llm(monkeypatch, payload)
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert len(out) == 1
    assert "chainlink #29" in out[0].text
    assert "#115" in out[0].text
    assert "#116" in out[0].text
    assert "#117" in out[0].text


async def test_coercion_preserves_disposition_flags_when_present(
    monkeypatch: pytest.MonkeyPatch,
):
    """Disposition flags ("Optional") in LLM output survive coercion.

    Guards the coercion pipeline: if the LLM emits text containing
    "Optional", "blocker", or similar qualifiers, those strings must
    reach CommitmentRecord.text intact so the commitment can be
    evaluated correctly without backtracking to the source turn.
    """
    payload = json.dumps({
        "commitments": [
            {
                "text": "Optional: file chainlink for --no-bridges flag on mimir run",
                "confidence": 0.75,
                "kind": "open_loop",
            }
        ]
    })
    _install_fake_call_llm(monkeypatch, payload)
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert len(out) == 1
    assert "Optional" in out[0].text
    assert "--no-bridges" in out[0].text
