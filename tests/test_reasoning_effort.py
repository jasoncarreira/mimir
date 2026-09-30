"""Tests for provider-specific reasoning-effort validation."""

from __future__ import annotations

import pytest

from mimir._reasoning_effort import validate_effort


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_codex_plus_accepts_new_effort_levels(effort: str) -> None:
    assert validate_effort("codex-plus", effort) == effort


@pytest.mark.parametrize("effort", [None, ""])
def test_codex_plus_treats_empty_effort_as_unset(effort: str | None) -> None:
    assert validate_effort("codex-plus", effort) is None


def test_codex_plus_rejects_unknown_effort() -> None:
    with pytest.raises(ValueError, match="turbo"):
        validate_effort("codex-plus", "turbo")


def test_other_provider_effort_levels_are_unchanged() -> None:
    assert validate_effort("openai", "minimal") == "minimal"
    assert validate_effort("anthropic", "max") == "max"
    assert validate_effort("claude-code", "max") == "max"

    for provider in ("openai", "anthropic", "claude-code"):
        with pytest.raises(ValueError, match=provider):
            validate_effort(provider, "ultra")
