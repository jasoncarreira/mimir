"""Enumerate provider package metadata without loading entry-point modules."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from importlib import metadata

from .manifest import PluginManifest, PluginManifestError, parse_manifest


class PluginDiscoveryError(ValueError):
    """An operator-enabled plugin cannot be safely loaded."""


@dataclass(frozen=True)
class PluginCandidate:
    name: str
    distribution: str
    entry_point: metadata.EntryPoint
    manifest: PluginManifest | None
    enabled: bool
    diagnostic: str | None = None

    @property
    def version(self) -> str | None:
        return self.manifest.version if self.manifest else None

    @property
    def license(self) -> str | None:
        return self.manifest.license if self.manifest else None


def _selection(value: str) -> tuple[bool, set[str]]:
    names = {part.strip() for part in value.split(",") if part.strip()}
    if "all" in names and len(names) != 1:
        raise PluginDiscoveryError("MIMIR_PLUGINS: 'all' cannot be combined with names")
    return "all" in names, names - {"all"}


def discover_plugins(*, enable: str | None = None) -> tuple[PluginCandidate, ...]:
    """List all installed candidates, including invalid disabled distributions.

    `enable` overrides the process environment for callers that need a snapshot.
    A candidate's entry point is metadata only; loading it is the manager's job.
    """
    all_enabled, names = _selection(os.environ.get("MIMIR_PLUGINS", "") if enable is None else enable)
    candidates = []
    for ep in metadata.entry_points(group="mimir.plugins"):
        dist = ep.dist
        source = dist.metadata.get("Name", "<unknown>") if dist else "<unknown>"
        manifest = None
        diagnostic = None
        try:
            if dist is None or dist.files is None:
                raise PluginManifestError("owning distribution has no file inventory")
            files = [file for file in dist.files if file.name == "mimir-plugin.toml"]
            if len(files) != 1:
                raise PluginManifestError(f"expected exactly one mimir-plugin.toml, found {len(files)}")
            manifest = parse_manifest(dist.locate_file(files[0]).read_bytes(), entry_name=ep.name)
        except (PluginManifestError, OSError) as exc:
            diagnostic = str(exc)
        candidates.append(PluginCandidate(ep.name, source, ep, manifest, all_enabled or ep.name in names, diagnostic))

    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.name] = counts.get(candidate.name, 0) + 1
    candidates = [
        replace(candidate, diagnostic="duplicate entry-point name" if counts[candidate.name] > 1 else candidate.diagnostic)
        for candidate in candidates
    ]
    candidates.sort(key=lambda item: (item.name, item.distribution, item.entry_point.value))
    missing = names - counts.keys()
    if missing:
        raise PluginDiscoveryError(f"MIMIR_PLUGINS: missing plugin(s): {', '.join(sorted(missing))}")
    return tuple(candidates)


def enabled_plugins(*, enable: str | None = None) -> tuple[PluginCandidate, ...]:
    """Fail boot for any selected invalid provider; do not import it here."""
    candidates = discover_plugins(enable=enable)
    for candidate in candidates:
        if candidate.enabled and candidate.diagnostic:
            raise PluginDiscoveryError(
                f"plugin {candidate.name!r} from {candidate.distribution}: {candidate.diagnostic}"
            )
    return tuple(candidate for candidate in candidates if candidate.enabled)
