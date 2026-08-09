"""Custom probe for Bluesky app passwords.

Used by ``credentials.yaml`` ``kind: python`` because the declarative
``format`` kind only handles a single env target, and social-cli
accepts either ``ATPROTO_APP_PASSWORD`` (canonical) or
``BSKY_APP_PASSWORD`` (compat alias).

Contract: zero-arg ``probe()`` returning ``(ok: bool, detail: str)``.
"""

from __future__ import annotations

import os


def probe() -> tuple[bool, str]:
    for env in ("ATPROTO_APP_PASSWORD", "BSKY_APP_PASSWORD"):
        value = os.environ.get(env, "").strip()
        if value:
            blocks = value.split("-")
            if len(blocks) != 4 or any(len(b) != 4 for b in blocks):
                return (False, f"{env} format wrong (expected xxxx-xxxx-xxxx-xxxx)")
            return (True, f"{env} format ok (live: ``social-cli whoami -p bsky``)")
    return (False, "unavailable: ATPROTO_APP_PASSWORD / BSKY_APP_PASSWORD not set")
