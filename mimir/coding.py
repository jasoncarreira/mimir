"""Canonical deployment switch for coding capabilities."""

from __future__ import annotations

import logging
import os


log = logging.getLogger(__name__)

_ENV_BOOL_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})
_ENV_BOOL_FALSY = frozenset({"0", "false", "no", "off", "n"})


def env_bool(name: str, default: bool, *, logger: logging.Logger | None = None) -> bool:
    """Parse an environment variable with Mimir's canonical boolean syntax."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in _ENV_BOOL_TRUTHY:
        return True
    if normalized in _ENV_BOOL_FALSY:
        return False
    if logger is not None:
        logger.warning(
            "%s=%r is not a recognised boolean (truthy=%s, falsy=%s); using default %r",
            name,
            raw,
            sorted(_ENV_BOOL_TRUTHY),
            sorted(_ENV_BOOL_FALSY),
            default,
        )
    return default


def coding_enabled() -> bool:
    """Return the operator-configured coding capability state."""
    return env_bool("MIMIR_CODING_ENABLED", False, logger=log)
