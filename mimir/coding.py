"""Canonical deployment switch for coding capabilities."""

from __future__ import annotations

import logging

from .env import env_bool


log = logging.getLogger(__name__)


def coding_enabled() -> bool:
    """Return the operator-configured coding capability state."""
    return env_bool("MIMIR_CODING_ENABLED", False, logger=log)
