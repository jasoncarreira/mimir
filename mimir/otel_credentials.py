"""First-party policy for OTLP credentials crossing process and log boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from mimir.shared_redaction_patterns import OTLP_ENV_NAME_PATTERN

_CARRIER_NAME = re.compile(OTLP_ENV_NAME_PATTERN, re.ASCII | re.IGNORECASE)


def sensitive_otlp_endpoint(value: object) -> bool:
    """Whether an endpoint is unsafe to disclose or pass to an agent child."""
    if not isinstance(value, str) or not value or value != value.strip():
        return True
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return True
    try:
        url = urlsplit(value)
        return (
            url.scheme.lower() not in {"http", "https"}
            or not url.netloc
            or not url.hostname
            or url.netloc.endswith(":")
            or url.username is not None
            or "?" in value
            or "#" in value
            or (url.port is not None and not 0 < url.port <= 65535)
            or any(char.isspace() for char in value)
            or "\\" in value
        )
    except ValueError:
        return True


def is_sensitive_otlp_env(name: object, value: object) -> bool:
    """Classify exact OTLP credential carriers, without blocking other OTEL settings."""
    if not isinstance(name, str):
        return False
    if _CARRIER_NAME.fullmatch(name) is None:
        return False
    return not name.upper().endswith("_ENDPOINT") or sensitive_otlp_endpoint(value)


def filter_otlp_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy safe to pass to an agent-visible subprocess."""
    return {name: value for name, value in env.items() if not is_sensitive_otlp_env(name, value)}
