"""Synthetic subscription stores for OpenCode integration tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path


def seed_codex_subscription(home: Path, refresh: str = "subscription") -> None:
    """Create a usable OAuth bundle and plugin registration without literal tokens."""
    segment = base64.urlsafe_b64encode(json.dumps({"exp": 300}).encode()).decode().rstrip("=")
    codex = home / ".codex" / "auth.json"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": ".".join(("header", segment, "signature")),
        "refresh_token": refresh,
    }}))
    codex.chmod(0o600)
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    if auth.exists():
        entries = json.loads(auth.read_text())
        entry = entries.get("openai")
        if isinstance(entry, dict) and entry.get("type") == "oauth" and not entry.get("access"):
            entry["access"] = ".".join(("header", segment, "signature"))
            auth.write_text(json.dumps(entries))
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads(config.read_text()) if config.exists() else {}
    document["plugin"] = ["opencode-openai-codex-auth"]
    config.write_text(json.dumps(document))
