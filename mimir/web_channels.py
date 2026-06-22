"""Web channel id helpers shared by chat and dashboard routes."""

from __future__ import annotations

import base64

WEB_CHANNEL_PREFIX = "web-"
DEFAULT_WEB_CHANNEL = "web-default"
_USER_CHANNEL_ESCAPE_PREFIX = "web-user:"


def web_channel_for_identity(canonical: str) -> str:
    """Return the per-user web channel for an identity canonical.

    For existing normal canonicals this intentionally preserves the historical
    ``web-<canonical>`` mapping so persisted chat history remains reachable.
    Canonicals that would collide with the shared default channel, or with the
    reserved escape namespace itself, are encoded into ``web-user:<base64url>``.

    The encoding keeps the mapping injective without slugifying: distinct
    canonicals still get distinct channels, while the literal canonical
    ``"default"`` no longer aliases anonymous/dev-mode ``web-default`` traffic.
    """
    canonical = (canonical or "").strip()
    if not canonical:
        return DEFAULT_WEB_CHANNEL

    candidate = f"web-{canonical}"
    if candidate == DEFAULT_WEB_CHANNEL or candidate.startswith(
        _USER_CHANNEL_ESCAPE_PREFIX
    ):
        encoded = (
            base64.urlsafe_b64encode(canonical.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        return f"{_USER_CHANNEL_ESCAPE_PREFIX}{encoded}"
    return candidate
