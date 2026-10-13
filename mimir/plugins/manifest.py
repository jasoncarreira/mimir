"""Validate the inert metadata shipped by a plugin's owning distribution."""

from __future__ import annotations

import re
import json
import tomllib
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from packaging import licenses
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

PLUGIN_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
_FIELDS = frozenset({"name", "version", "license", "requires_mimir"})
_RESERVED = frozenset({"dockerfile", "themes", "frontend", "defaults", "config"})
_SKIN_TOKENS = frozenset("--mimir-" + token for token in (
    "color-text color-text-muted color-background color-chrome-background color-chrome-border "
    "color-chrome-accent color-chrome-accent-text color-panel-background color-panel-background-muted "
    "color-panel-border color-panel-border-hover color-panel-shadow color-status-info "
    "color-status-info-background color-status-success color-status-success-background "
    "color-status-warning color-status-warning-background color-status-danger color-status-danger-background "
    "color-timeline-reasoning color-timeline-reasoning-background color-timeline-tool-call "
    "color-timeline-tool-call-background color-timeline-tool-result color-timeline-tool-result-background "
    "color-trigger-saga-session-end color-trigger-scheduled-tick color-trigger-poller "
    "color-trigger-user-message color-trigger-synthesis color-trigger-heartbeat "
    "color-trigger-claude-code-spawn color-trigger-shell-job-complete color-code-background "
    "color-code-text color-focus-ring font-family-base font-family-mono font-size-xs font-size-sm "
    "font-size-md font-size-lg font-weight-regular font-weight-strong line-height-tight line-height-body "
    "radius-panel radius-control space-2xs space-xs space-sm space-md space-lg space-xl "
    "space-shell-inline space-shell-block elevation-panel elevation-overlay border-width-hairline "
    "border-width-chrome interaction-hover-background interaction-active-background "
    "interaction-disabled-opacity motion-duration-fast motion-duration-normal"
).split())


class PluginManifestError(ValueError):
    """The provider's static manifest is malformed or incompatible."""


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    license: str
    requires_mimir: str
    dockerfile: Mapping[str, Any] = field(default_factory=dict)
    themes: tuple[str, ...] = ()
    skin_defaults: tuple[str, ...] = ()
    frontend: Mapping[str, Any] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)


def _resource(value: str, *, key: str, root: Path | None, files: frozenset[Path] | None,
              directory: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PluginManifestError(f"{key}: invalid package resource")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or "//" in value:
        raise PluginManifestError(f"{key}: resource must be package-relative: {value!r}")
    if root is not None and files is not None:
        target = root / value
        matches = [item for item in files if item == target or directory and target in item.parents]
        if not matches or any(item.is_symlink() for item in (target, *target.parents, *matches)) or any(
            not item.resolve().is_relative_to(root.resolve()) for item in matches
        ):
            raise PluginManifestError(f"{key}: missing or escaping package resource: {value!r}")
    return value


def _table(raw: dict[str, Any], name: str, fields: set[str]) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict) or value.keys() - fields:
        raise PluginManifestError(f"{name}: invalid fields")
    return value


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PluginManifestError(f"{key}: expected nonempty strings in an array")
    return tuple(value)


def parse_manifest(data: bytes, *, entry_name: str, resource_root: Path | None = None,
                   distribution_files: frozenset[Path] | None = None) -> PluginManifest:
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
    docker = _table(raw, "dockerfile", {"system_packages", "build_args", "users", "directories", "root_run", "s6_services"})
    for key in ("system_packages", "users", "directories", "root_run", "s6_services"):
        if key in docker:
            values = _strings(docker[key], f"dockerfile.{key}")
            if key == "system_packages" and any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", item) for item in values):
                raise PluginManifestError("dockerfile.system_packages: invalid package name")
            if key == "users" and any(not re.fullmatch(r"[a-z_][a-z0-9_-]*", item) for item in values):
                raise PluginManifestError("dockerfile.users: invalid user name")
            if key == "directories" and any(not item.startswith("/") or ".." in item.split("/") for item in values):
                raise PluginManifestError("dockerfile.directories: absolute safe paths required")
            if key == "s6_services":
                for path in values:
                    _resource(path, key=key, root=resource_root, files=distribution_files, directory=True)
                    if resource_root is not None:
                        service = resource_root / path
                        run = service / "run"
                        names = {item.name for item in service.iterdir()}
                        if (distribution_files is not None and not {run, service / "type"} <= distribution_files
                                or not run.is_file() or run.is_symlink() or not run.stat().st_mode & 0o111
                                or not (service / "type").is_file()
                                or (service / "type").read_text().strip() != "longrun"
                                or names != {"run", "type"}):
                            raise PluginManifestError(f"dockerfile.s6_services: invalid longrun service {path!r}")
    args = docker.get("build_args", {})
    if not isinstance(args, dict) or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) or not isinstance(v, str)
                                         for k, v in args.items()):
        raise PluginManifestError("dockerfile.build_args: invalid build argument")
    themes = raw.get("themes", [])
    if not isinstance(themes, list):
        raise PluginManifestError("themes: expected an array of package-relative JSON paths")
    themes = _strings(themes, "themes")
    skin_defaults = []
    for path in themes:
        if not path.endswith(".json"):
            raise PluginManifestError("themes: expected JSON resource")
        _resource(path, key="themes", root=resource_root, files=distribution_files)
        if resource_root is not None:
            try:
                skin = json.loads((resource_root / path).read_text())
            except (OSError, ValueError) as exc:
                raise PluginManifestError(f"themes: invalid JSON: {path}") from exc
            if not isinstance(skin, dict) or not isinstance(skin.get("id"), str) or not skin["id"] \
                    or not isinstance(skin.get("label"), str) or not skin["label"]:
                raise PluginManifestError(f"themes: id and label required: {path}")
            tokens = skin.get("tokens")
            if not isinstance(tokens, dict) or tokens.keys() != _SKIN_TOKENS or any(
                not isinstance(value, str) or not value or re.search(r"url\s*\(|@import|https?://", value, re.I)
                for value in tokens.values()
            ):
                raise PluginManifestError(f"themes: complete local --mimir-* token set required: {path}")
            for asset in skin.get("assets", []):
                _resource(asset, key="themes.assets", root=resource_root, files=distribution_files)
            if skin.get("default", False) is True:
                skin_defaults.append(skin["id"])
            elif skin.get("default", False) is not False:
                raise PluginManifestError("themes: default must be boolean")
    if len(skin_defaults) > 1:
        raise PluginManifestError("themes: only one default skin per plugin")
    frontend = _table(raw, "frontend", {"api_major", "module", "css", "assets"})
    if frontend:
        if type(frontend.get("api_major")) is not int or frontend["api_major"] < 1 or "module" not in frontend:
            raise PluginManifestError("frontend: api_major and module required")
        for key in ("module", "css"):
            if key in frontend:
                _resource(frontend[key], key=f"frontend.{key}", root=resource_root, files=distribution_files)
        for path in _strings(frontend.get("assets", []), "frontend.assets"):
            _resource(path, key="frontend.assets", root=resource_root, files=distribution_files)
    defaults = _table(raw, "defaults", {"version", "skills", "prompts", "scheduler_jobs"})
    if defaults:
        if not isinstance(defaults.get("version"), str) or not defaults["version"]:
            raise PluginManifestError("defaults: version required")
        for key in ("skills", "prompts", "scheduler_jobs"):
            for path in _strings(defaults.get(key, []), f"defaults.{key}"):
                _resource(path, key=f"defaults.{key}", root=resource_root, files=distribution_files,
                          directory=key == "skills")
    config = _table(raw, "config", set(raw.get("config", {})) if isinstance(raw.get("config", {}), dict) else set())
    prefix = f"MIMIR_PLUGIN_{entry_name.upper().replace('-', '_')}_"
    for key, definition in config.items():
        if not key.startswith(prefix) or not key[len(prefix):] or not re.fullmatch(r"[A-Z0-9_]+", key):
            raise PluginManifestError(f"config: key {key!r} must be under {prefix}")
        if not isinstance(definition, dict) or definition.keys() - {"type", "description", "secret", "security"} \
                or definition.get("type") not in {"string", "integer", "boolean"} \
                or any(type(definition.get(flag, False)) is not bool for flag in ("secret", "security")):
            raise PluginManifestError(f"config: invalid schema for {key}")
    return PluginManifest(*(raw[key] for key in ("name", "version", "license", "requires_mimir")),
                          MappingProxyType(docker), themes, tuple(skin_defaults), MappingProxyType(frontend),
                          MappingProxyType(defaults), MappingProxyType(config))
