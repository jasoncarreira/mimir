"""Sanitization shared by client-controlled HTTP event ingresses."""

from __future__ import annotations

from typing import Any, Mapping


# These fields affect authorization or durable output visibility and may only
# be set by trusted bridge event constructors.
BRIDGE_AUTHORITY_EXTRA_KEYS = frozenset({
    "bridge_instance",
    "channel_visibility",
})


def strip_bridge_authority_extra(
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Drop bridge-owned authority metadata from client-controlled input."""
    if not extra:
        return {}
    return {
        key: value
        for key, value in extra.items()
        if key not in BRIDGE_AUTHORITY_EXTRA_KEYS
    }
