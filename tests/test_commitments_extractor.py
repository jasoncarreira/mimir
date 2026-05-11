"""Commitments extractor (Phase 2a).

The extractor calls ``claude_agent_sdk.query()`` under the hood; tests
monkeypatch it to return a canned async iterator over a fake
``AssistantMessage`` carrying the JSON payload. The actual LLM call is
exercised by ``scratch/commitments_backtest.py`` against real session
data; these tests pin the parser + record-coercion contract.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mimir.commitments.extractor import (
    EXTRACTION_PROMPT_VERSION,
    MIN_OUTPUT_LEN,
    _coerce_to_record,
    _parse_extraction_json,
    _strip_code_fence,
    extract_commitments,
)
from mimir.commitments.models import (
    CommitmentKind,
    CommitmentSensitivity,
)


# ─── helper: stub the SDK query() to yield a canned response ────────


class _FakeTextBlock:
    """Mimics ``claude_agent_sdk.TextBlock`` for tests; isinstance check
    against the real type is what extractor cares about, so we patch
    both ``query`` AND ``AssistantMessage``/``TextBlock`` at the module
    boundary."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, response_text: str,
) -> list[dict]:
    """Patch ``claude_agent_sdk.query`` to yield a single fake
    AssistantMessage with the given text. Returns a captured-args list
    so tests can assert the prompt/options shape."""
    captured: list[dict] = []

    async def fake_query(*, prompt: str, options: Any = None, transport=None):
        captured.append({"prompt": prompt, "options": options})
        yield _FakeAssistantMessage(response_text)

    # Patch BOTH the symbol the extractor imports AND the message types
    # so the isinstance checks pass.
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_agent_sdk, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(claude_agent_sdk, "TextBlock", _FakeTextBlock)
    return captured


# ─── parser unit tests (no SDK needed) ──────────────────────────────


def test_strip_code_fence_handles_fenced_json():
    body = "```json\n{\"commitments\": []}\n```"
    assert _strip_code_fence(body) == '{"commitments": []}'


def test_strip_code_fence_handles_bare_fence():
    body = "```\n{\"commitments\": []}\n```"
    assert _strip_code_fence(body) == '{"commitments": []}'


def test_strip_code_fence_passthrough_no_fence():
    body = '{"commitments": []}'
    assert _strip_code_fence(body) == '{"commitments": []}'


def test_parse_extraction_json_happy_path():
    parsed = _parse_extraction_json('{"commitments": [{"text": "X"}]}')
    assert parsed == {"commitments": [{"text": "X"}]}


def test_parse_extraction_json_handles_fence():
    raw = '```json\n{"commitments": [{"text": "X"}]}\n```'
    parsed = _parse_extraction_json(raw)
    assert parsed is not None
    assert parsed["commitments"][0]["text"] == "X"


def test_parse_extraction_json_returns_none_on_bad_json():
    assert _parse_extraction_json("not json at all") is None
    assert _parse_extraction_json("") is None


# ─── _coerce_to_record validation ───────────────────────────────────


def test_coerce_record_full_happy_path():
    rec = _coerce_to_record(
        {
            "text": "Review PR #111",
            "kind": "agent_promise",
            "sensitivity": "routine",
            "confidence": 0.85,
            "suggested_reminder": "PR #111 needs review",
            "channel_bound": True,
        },
        channel_id="chan-1",
        saga_session_id="saga-xyz",
        source_turn_id="t-abc",
    )
    assert rec is not None
    assert rec.text == "Review PR #111"
    assert rec.kind == "agent_promise"
    assert rec.confidence == 0.85
    assert rec.channel_id == "chan-1"  # bound → carries channel
    assert rec.source_turn_id == "t-abc"
    assert rec.saga_session_id == "saga-xyz"
    assert rec.dedupe_key  # auto-generated


def test_coerce_record_channel_unbound_strips_channel():
    """``channel_bound=False`` → record's channel_id is None even when
    the extraction was called with a channel."""
    rec = _coerce_to_record(
        {
            "text": "Read paper",
            "confidence": 0.7,
            "channel_bound": False,
        },
        channel_id="chan-1",
        saga_session_id=None,
        source_turn_id=None,
    )
    assert rec is not None
    assert rec.channel_id is None


def test_coerce_record_drops_below_confidence_floor():
    """Sub-0.4 confidence items are silently dropped — they were
    mostly false positives in the Phase 0 backtest."""
    rec = _coerce_to_record(
        {"text": "vague intent", "confidence": 0.3},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is None


def test_coerce_record_drops_missing_text():
    rec = _coerce_to_record(
        {"text": "", "confidence": 0.9},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is None
    rec = _coerce_to_record(
        {"confidence": 0.9},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is None


def test_coerce_record_defaults_unknown_kind_to_open_loop():
    rec = _coerce_to_record(
        {"text": "X", "kind": "unrecognized_kind", "confidence": 0.7},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is not None
    assert rec.kind == CommitmentKind.OPEN_LOOP.value


def test_coerce_record_defaults_unknown_sensitivity_to_routine():
    rec = _coerce_to_record(
        {"text": "X", "sensitivity": "extreme", "confidence": 0.7},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is not None
    assert rec.sensitivity == CommitmentSensitivity.ROUTINE.value


def test_coerce_record_text_capped_at_200():
    """The prompt asks for ≤120 chars; we cap at 200 as a safety margin
    against models that ignore length instructions."""
    long_text = "x" * 300
    rec = _coerce_to_record(
        {"text": long_text, "confidence": 0.7},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is not None
    assert len(rec.text) == 200


def test_coerce_record_suggested_reminder_falls_back_to_text():
    rec = _coerce_to_record(
        {"text": "Review PR #99", "confidence": 0.8},
        channel_id="c1", saga_session_id=None, source_turn_id=None,
    )
    assert rec is not None
    assert rec.suggested_reminder == "Review PR #99"


# ─── extract_commitments end-to-end (with stubbed SDK) ──────────────


@pytest.mark.asyncio
async def test_extract_returns_records_from_canned_response(
    monkeypatch: pytest.MonkeyPatch,
):
    """Happy path: SDK returns valid JSON → extractor returns
    CommitmentRecord list ready for store.add()."""
    response = json.dumps({
        "commitments": [
            {
                "text": "Apply Item 7 fix for PR #111",
                "kind": "agent_promise",
                "sensitivity": "routine",
                "confidence": 0.9,
                "suggested_reminder": "PR #111 Item 7 fix pending",
                "channel_bound": True,
            },
            {
                "text": "Read the paper Mary recommended",
                "kind": "open_loop",
                "sensitivity": "routine",
                "confidence": 0.65,
                "channel_bound": False,
            },
        ]
    })
    _install_fake_sdk(monkeypatch, response)

    output = (
        "Boundary recorded. Two unfinished items carried forward: "
        "Apply Item 7 fix for PR #111 (still gated on test push), "
        "and follow up on Mary's paper recommendation."
    )
    out = await extract_commitments(
        output,
        channel_id="poller:github-activity",
        saga_session_id="saga-xyz",
        source_turn_id="t-abc12",
    )

    assert len(out) == 2
    by_text = {r.text: r for r in out}
    pr_rec = by_text["Apply Item 7 fix for PR #111"]
    assert pr_rec.kind == "agent_promise"
    assert pr_rec.channel_id == "poller:github-activity"  # bound
    assert pr_rec.source_turn_id == "t-abc12"
    paper_rec = by_text["Read the paper Mary recommended"]
    assert paper_rec.channel_id is None  # unbound


@pytest.mark.asyncio
async def test_extract_skips_short_output_without_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """Outputs <MIN_OUTPUT_LEN must short-circuit to ``[]`` without
    calling the SDK — cheap on the 'boundary recorded, nothing to
    capture' single-turn no-op pattern."""
    sdk_called: list[bool] = []

    async def fake_query(**kwargs):
        sdk_called.append(True)
        yield  # pragma: no cover

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    short = "Boundary recorded."  # well under 100 chars
    assert len(short) < MIN_OUTPUT_LEN
    out = await extract_commitments(
        short, channel_id="c1", saga_session_id=None, source_turn_id="t-1",
    )
    assert out == []
    assert sdk_called == []  # never invoked


@pytest.mark.asyncio
async def test_extract_returns_empty_on_empty_json_response(
    monkeypatch: pytest.MonkeyPatch,
):
    """LLM returns ``{"commitments": []}`` (the most common case per
    backtest: ~67% of sessions) → ``[]``, no errors."""
    _install_fake_sdk(monkeypatch, '{"commitments": []}')
    out = await extract_commitments(
        "x" * 200,  # long enough to invoke SDK
        channel_id="c1",
        saga_session_id=None,
        source_turn_id="t-1",
    )
    assert out == []


@pytest.mark.asyncio
async def test_extract_handles_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
):
    """LLM returns garbage / not-JSON → parser returns None → extractor
    returns ``[]`` without raising."""
    _install_fake_sdk(monkeypatch, "not actually json")
    out = await extract_commitments(
        "x" * 200,
        channel_id="c1",
        saga_session_id=None,
        source_turn_id="t-1",
    )
    assert out == []


@pytest.mark.asyncio
async def test_extract_handles_sdk_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    """SDK ``query()`` raises (timeout, rate limit, etc.) → extractor
    catches, logs, returns ``[]``. The finalize hook can't recover from
    a failed extraction so bubbling is pointless; the same commitments
    will resurface in a future session-end output."""

    async def raising_query(**kwargs):
        raise RuntimeError("SDK is angry")
        yield  # pragma: no cover

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", raising_query)

    out = await extract_commitments(
        "x" * 200,
        channel_id="c1",
        saga_session_id=None,
        source_turn_id="t-1",
    )
    assert out == []


@pytest.mark.asyncio
async def test_extract_passes_prompt_with_session_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    """The user prompt sent to the LLM must include the channel /
    session id so the model has the metadata the system prompt asks
    it to honor (channel_bound default, recipient inference, etc.)."""
    captured = _install_fake_sdk(monkeypatch, '{"commitments": []}')
    await extract_commitments(
        "x" * 200,
        channel_id="discord-123",
        saga_session_id="saga-fooo",
        source_turn_id="t-bar",
    )
    assert len(captured) == 1
    prompt = captured[0]["prompt"]
    assert "discord-123" in prompt
    assert "saga-fooo" in prompt
    # Output content lives inside the <synthesis> tags.
    assert "<synthesis>" in prompt


def test_extraction_prompt_version_constant_present():
    """``EXTRACTION_PROMPT_VERSION`` is the gate the backtest script
    uses to detect when the prompt has changed. Keep it stable as a
    public attribute."""
    assert EXTRACTION_PROMPT_VERSION  # non-empty
