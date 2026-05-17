"""Commitments extractor tests (post-deepagents cutover).

The extractor now uses ``langchain.chat_models.init_chat_model`` instead
of ``claude_agent_sdk.query``. We monkey-patch the chat model factory
to return a stub that yields a canned ``AIMessage`` payload — that
exercises the same parser + record-coercion contract the SDK tests
covered, with no live LLM call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

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


class _StubChat:
    """Stands in for whatever ``init_chat_model`` returns. ``ainvoke``
    yields a single ``AIMessage`` with the canned text body."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    async def ainvoke(self, msgs: list[Any]) -> AIMessage:
        return AIMessage(content=self._response_text)


def _install_fake_chat(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    def _factory(_model: str) -> _StubChat:
        return _StubChat(response_text)
    # init_chat_model is imported inside extract_commitments(); patch
    # the source module so the inner import resolves to our factory.
    monkeypatch.setattr(
        "langchain.chat_models.init_chat_model", _factory, raising=True,
    )


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
    # Patch init_chat_model to a sentinel that would explode if called.
    def _explode(_model: str):
        raise AssertionError("LLM should not be called for short outputs")
    monkeypatch.setattr(
        "langchain.chat_models.init_chat_model", _explode, raising=True,
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
    _install_fake_chat(monkeypatch, payload)
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
    _install_fake_chat(monkeypatch, payload)
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert len(out) == 1
    assert out[0].text == "real follow-up"


async def test_extract_returns_empty_on_llm_failure(monkeypatch: pytest.MonkeyPatch):
    def _boom(_model: str):
        raise RuntimeError("upstream model down")
    monkeypatch.setattr(
        "langchain.chat_models.init_chat_model", _boom, raising=True,
    )
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert out == []


async def test_extract_returns_empty_on_unparseable_response(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_chat(monkeypatch, "<<not json at all>>")
    out = await extract_commitments(
        "x" * 200,
        channel_id="ch-1", saga_session_id="s1", source_turn_id="t1",
    )
    assert out == []
