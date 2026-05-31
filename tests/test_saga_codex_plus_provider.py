"""Unit tests for saga's ``codex_plus`` LLM provider.

saga.call_llm gains a fourth provider, ``codex_plus``, routing saga's
internal LLM calls (triple extraction, consolidation, synthesis,
rerank) through ``langchain_codex_plus.ChatCodexPlus`` over the
operator's ChatGPT/Codex subscription — no API credit, shares the
plan-window quota with the main agent (like ``claude_code``, but no
subprocess: ChatCodexPlus is a native async chat model).

These tests mock ChatCodexPlus so they're hermetic — no ``~/.codex``
auth bundle and no network are required (CI has neither). The live
end-to-end check (real subscription, real gpt-5.4-mini) is run
out-of-band during the cutover, not here.
"""
from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest


# ── _flatten_text ────────────────────────────────────────────────────


def test_flatten_text_handles_str_list_dict_obj():
    from mimir.saga._llm import _flatten_text

    assert _flatten_text("  hi  ") == "hi"
    assert _flatten_text(["a", "b"]) == "ab"
    assert _flatten_text([{"text": "x"}, {"text": "y"}]) == "xy"

    class _Blk:
        def __init__(self, t: str):
            self.text = t

    assert _flatten_text([_Blk("p"), _Blk("q")]) == "pq"
    # None-ish block fields and mixed shapes are tolerated.
    assert _flatten_text([{"text": None}, "z"]) == "z"
    assert _flatten_text(None) == ""


# ── fakes ────────────────────────────────────────────────────────────


class _FakeMsg:
    def __init__(self, content: Any):
        self.content = content


def _install_fake_codex(monkeypatch, *, content: Any = "fake-reply", raise_exc=None):
    """Inject a fake ``langchain_codex_plus`` module exposing ChatCodexPlus.

    Returns a dict the fake populates with its construction kwargs +
    the messages it was invoked with, so tests can assert on them.
    """
    captured: dict[str, Any] = {}

    class _FakeChatCodexPlus:
        def __init__(self, **kwargs: Any):
            captured.update(kwargs)

        async def ainvoke(self, messages):
            captured["messages"] = messages
            if raise_exc is not None:
                raise raise_exc
            return _FakeMsg(content)

    monkeypatch.setitem(
        sys.modules,
        "langchain_codex_plus",
        types.SimpleNamespace(ChatCodexPlus=_FakeChatCodexPlus),
    )
    return captured


# ── _call_codex_plus_async ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_codex_plus_builds_with_model_and_reasoning_none(monkeypatch):
    from mimir.saga import _llm

    captured = _install_fake_codex(monkeypatch, content="S|P|O")
    out = await _llm._call_codex_plus_async(
        {"provider": "codex_plus", "model": "gpt-5.4-mini", "timeout": 45},
        prompt="extract", max_tokens=64, temperature=0.0, system="be precise",
    )
    assert out == "S|P|O"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["reasoning_effort"] == "none"
    assert captured["timeout_seconds"] == 45.0
    # Both system + user messages are passed through.
    assert len(captured["messages"]) == 2


@pytest.mark.asyncio
async def test_codex_plus_defaults_model_and_omits_system(monkeypatch):
    from mimir.saga import _llm

    captured = _install_fake_codex(monkeypatch)
    await _llm._call_codex_plus_async(
        {"provider": "codex_plus"},
        prompt="hi", max_tokens=10, temperature=0.0, system=None,
    )
    assert captured["model"] == "gpt-5.4"  # documented default
    assert len(captured["messages"]) == 1  # no system → user message only


@pytest.mark.asyncio
async def test_codex_plus_flattens_list_content(monkeypatch):
    from mimir.saga import _llm

    _install_fake_codex(monkeypatch, content=[{"text": "a"}, {"text": "b"}])
    out = await _llm._call_codex_plus_async(
        {"model": "gpt-5.4-mini"},
        prompt="x", max_tokens=10, temperature=0.0, system=None,
    )
    assert out == "ab"


@pytest.mark.asyncio
async def test_codex_plus_returns_empty_on_failure(monkeypatch, caplog):
    from mimir.saga import _llm

    _install_fake_codex(monkeypatch, raise_exc=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING, logger="mimir.saga._llm"):
        out = await _llm._call_codex_plus_async(
            {"model": "gpt-5.4-mini"},
            prompt="x", max_tokens=10, temperature=0.0, system=None,
        )
    assert out == ""
    assert any("codex_plus call failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_codex_plus_missing_extra_returns_empty(monkeypatch, caplog):
    from mimir.saga import _llm

    # Simulate the 'codex-plus' extra not installed: importing the package
    # raises ImportError (None in sys.modules halts the import).
    monkeypatch.setitem(sys.modules, "langchain_codex_plus", None)
    with caplog.at_level(logging.WARNING, logger="mimir.saga._llm"):
        out = await _llm._call_codex_plus_async(
            {"model": "gpt-5.4-mini"},
            prompt="x", max_tokens=10, temperature=0.0, system=None,
        )
    assert out == ""
    assert any(
        "langchain-codex-plus is not" in r.getMessage() for r in caplog.records
    )


# ── call_llm dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_llm_dispatches_codex_plus(monkeypatch):
    from mimir.saga import _llm

    seen: dict[str, Any] = {}

    async def _stub(llm, *, prompt, max_tokens, temperature, system):
        seen["llm"] = llm
        seen["prompt"] = prompt
        return "codex-out"

    monkeypatch.setattr(_llm, "_call_codex_plus_async", _stub)
    out = await _llm.call_llm(
        {"provider": "codex_plus", "model": "gpt-5.4-mini"},
        prompt="hello",
    )
    assert out == "codex-out"
    assert seen["prompt"] == "hello"
    assert seen["llm"]["model"] == "gpt-5.4-mini"
