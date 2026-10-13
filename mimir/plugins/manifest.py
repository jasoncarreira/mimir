"""Validate the inert metadata shipped by a plugin's owning distribution."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import metadata

from packaging import licenses
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

PLUGIN_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
_FIELDS = frozenset({"name", "version", "license", "requires_mimir"})
_RESERVED = frozenset({"dockerfile", "themes", "frontend", "defaults", "config"})


class PluginManifestError(ValueError):
    """The provider's static manifest is malformed or incompatible."""


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    license: str
    requires_mimir: str


def parse_manifest(data: bytes, *, entry_name: str) -> PluginManifest:
    """Parse a provider-owned TOML file without executing provider code."""
    if entry_name == "core":
        raise PluginManifestError("plugin name 'core' is reserved")
    if not PLUGIN_NAME.fullmatch(entry_name):
        raise PluginManifestError(f"invalid entry-point name {entry_name!r}")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PluginManifestError("invalid mimir-plugin.toml") from exc
    unexpected = sorted(raw.keys() - _FIELDS - _RESERVED)
    if unexpected:
        raise PluginManifestError(f"unknown manifest keys: {', '.join(unexpected)}")
    missing = sorted(_FIELDS - raw.keys())
    if missing:
        raise PluginManifestError(f"missing manifest keys: {', '.join(missing)}")
    for key in _FIELDS:
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise PluginManifestError(f"{key} must be a nonempty string")
    if raw["name"] == "core":
        raise PluginManifestError("plugin name 'core' is reserved")
    if not PLUGIN_NAME.fullmatch(raw["name"]) or raw["name"] != entry_name:
        raise PluginManifestError(f"manifest name {raw['name']!r} does not match entry point {entry_name!r}")
    try:
        Version(raw["version"])
    except InvalidVersion as exc:
        raise PluginManifestError(f"invalid version {raw['version']!r}") from exc
    try:
        licenses.canonicalize_license_expression(raw["license"])
    except licenses.InvalidLicenseExpression as exc:
        raise PluginManifestError(f"invalid SPDX license {raw['license']!r}") from exc
    try:
        requirement = SpecifierSet(raw["requires_mimir"])
    except InvalidSpecifier as exc:
        raise PluginManifestError(f"invalid requires_mimir {raw['requires_mimir']!r}") from exc
    try:
        installed = Version(metadata.version("mimir-agent"))
    except metadata.PackageNotFoundError as exc:
        raise PluginManifestError("mimir-agent distribution is not installed") from exc
    if installed not in requirement:
        raise PluginManifestError(
            f"requires_mimir {raw['requires_mimir']!r} does not include installed Mimir {installed}"
        )
    return PluginManifest(*(raw[key] for key in ("name", "version", "license", "requires_mimir")))
