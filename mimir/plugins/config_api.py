"""Owner-scoped, read-only access to declared plugin environment config."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from mimir.admin_config import _is_secret_env_name, _redacted_value

from .manifest import PluginManifest


def plugin_config_rows(owner: str, manifests: Mapping[str, PluginManifest]) -> list[dict[str, Any]]:
    """Only expose keys declared by the selected owner's validated manifest."""
    manifest = manifests.get(owner)
    if manifest is None:
        raise ValueError(f"plugin {owner!r} is not enabled")
    rows = []
    for name, definition in sorted(manifest.config.items()):
        value = os.environ.get(name)
        secret = bool(definition.get("secret") or definition.get("security")) or _is_secret_env_name(name)
        rows.append({
            "name": name, "category": owner, "present": bool(value),
            "secret": secret,
            "value": "[REDACTED]" if secret and value is not None else _redacted_value(name, value),
            "mutable": False,
        })
    return rows


def validate_plugin_config_write(owner: str, name: str, manifests: Mapping[str, PluginManifest]) -> None:
    """Check host-mediated writes; security settings have no writable web path."""
    manifest = manifests.get(owner)
    if manifest is None or name not in manifest.config or manifest.config[name].get("security"):
        raise ValueError(f"{owner}: config key not writable: {name}")
