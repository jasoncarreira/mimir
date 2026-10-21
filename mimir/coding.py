"""Coding availability from installed, process-selected plugin metadata."""

from __future__ import annotations

from .plugins.discovery import enabled_plugins


def coding_enabled() -> bool:
    """Check selection without importing a disabled provider."""
    return any(candidate.name == "cultivate" for candidate in enabled_plugins())
