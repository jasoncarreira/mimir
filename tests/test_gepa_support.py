"""Tests for the gepa skill wiring (chainlink #405).

The mimir-side glue (the ``reflection_lm`` callable) is unit-tested with a
stub ChatModel, so it runs with or without the optional ``gepa`` extra. The
gepa-import smoke uses ``importorskip`` so the core suite stays green when
gepa isn't installed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mimir.gepa_support import (
    _content_to_text,
    chat_model_as_reflection_lm,
    reflection_lm_from_config,
)


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubModel:
    """Minimal BaseChatModel stand-in: records prompts, returns a fixed message."""

    def __init__(self, content):
        self._content = content
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return _StubMessage(self._content)


# ── _content_to_text ────────────────────────────────────────────────


def test_content_to_text_plain_string():
    assert _content_to_text("hello") == "hello"


def test_content_to_text_anthropic_block_list():
    blocks = [{"type": "text", "text": "alpha"}, {"type": "text", "text": "beta"}]
    assert _content_to_text(blocks) == "alphabeta"


def test_content_to_text_mixed_blocks_and_non_string_fallback():
    assert _content_to_text(["a", {"text": "b"}, {"noise": 1}]) == "ab"
    assert _content_to_text(123) == "123"


# ── chat_model_as_reflection_lm ──────────────────────────────────────


def test_reflection_lm_returns_text_for_str_content():
    model = _StubModel("improved instruction")
    lm = chat_model_as_reflection_lm(model)
    assert lm("reflect on this") == "improved instruction"
    assert model.prompts == ["reflect on this"]


def test_reflection_lm_flattens_block_content():
    model = _StubModel([{"text": "part1"}, {"text": "part2"}])
    lm = chat_model_as_reflection_lm(model)
    assert lm("x") == "part1part2"


# ── reflection_lm_from_config ────────────────────────────────────────


def test_reflection_lm_from_config_uses_config_model_helper(monkeypatch):
    """``reflection_lm_from_config`` uses the same Config-based model helper
    as the main agent instead of re-deriving model kwargs locally."""
    stub = _StubModel("from-config")
    captured: dict = {}

    def fake_resolve(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return stub

    monkeypatch.setattr("mimir.agent.resolve_model_from_config", fake_resolve)

    class _Cfg:
        model_spec = "codex-plus:gpt-5.5"
        model_max_retries = 4
        model_max_tokens = 1234
        model_reasoning_effort = "medium"

    cfg = _Cfg()
    lm = reflection_lm_from_config(config=cfg)

    assert lm("hi") == "from-config"
    assert captured == {"config": cfg, "kwargs": {}}


# ── packaging: gepa is opt-in, version-pinned, never core ─────────────


def test_gepa_is_pinned_optin_extra_not_core():
    """gepa ships as a version-pinned opt-in extra (chainlink #405) — never a
    bare/floating dep, never a core/default dependency."""
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]
    extras = project["optional-dependencies"]

    assert "gepa" in extras, "expected an opt-in [gepa] extra"
    gepa_dep = next(
        (d for d in extras["gepa"] if d.replace(" ", "").startswith("gepa")), None
    )
    assert gepa_dep, "gepa extra must install the gepa package"
    assert any(op in gepa_dep for op in ("==", ">=", "~=")), (
        f"gepa must be version-pinned, got {gepa_dep!r}"
    )
    # Never a core/default dependency — the base runtime stays lean.
    assert not any("gepa" in d for d in project["dependencies"])


# ── gepa-present smoke (skips when the extra isn't installed) ─────────


def test_gepa_optimize_accepts_reflection_lm_callable():
    """Smoke: with the gepa extra installed, gepa imports and our
    ``reflection_lm`` callable satisfies the ``str -> str`` contract gepa
    drives. The full optimize run against a real evaluator is the pilot's
    job (chainlink #404)."""
    gepa = pytest.importorskip("gepa")
    assert hasattr(gepa, "optimize")
    lm = chat_model_as_reflection_lm(_StubModel("ok"))
    assert callable(lm)
    assert lm("propose a better prompt") == "ok"
