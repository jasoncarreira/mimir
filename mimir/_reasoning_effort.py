"""Provider-specific reasoning-effort validation."""

from __future__ import annotations


# Mirrors the provider packages' accepted values. "none" is Codex-only.
EFFORT_LEVELS: dict[str, frozenset[str]] = {
    "codex-plus": frozenset({"none", "low", "medium", "high", "xhigh"}),
    "openai": frozenset({"minimal", "low", "medium", "high"}),
    "anthropic": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude-code": frozenset({"low", "medium", "high", "max"}),
}


def validate_effort(provider: str, effort: str) -> str:
    """Return a valid effort unchanged; reject invalid provider values."""
    valid = EFFORT_LEVELS.get(provider)
    if valid is not None and effort not in valid:
        raise ValueError(
            f"reasoning effort {effort!r} is not valid for provider {provider!r}; "
            f"choose one of {sorted(valid)}"
        )
    return effort
