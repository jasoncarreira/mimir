"""Static discovery of installed, operator-enabled Mimir plugins."""

from __future__ import annotations

from .discovery import PluginCandidate, PluginDiscoveryError, discover_plugins, enabled_plugins
from .manifest import PluginManifest, PluginManifestError

__all__ = [
    "PluginCandidate",
    "PluginDiscoveryError",
    "PluginManifest",
    "PluginManifestError",
    "discover_plugins",
    "enabled_plugins",
]
