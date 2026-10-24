"""Copy selected plugins' validated package defaults into their home namespaces."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import yaml

from .discovery import PluginCandidate, discover_plugins, enabled_plugins

if TYPE_CHECKING:
    from .registry import PolicyRegistry


def safe_destination(root: Path, rel: Path) -> Path:
    """Reject symlinks in an operator-controlled destination, including parents."""
    root = Path(root)
    if root.is_symlink() or rel.is_absolute() or not rel.parts or any(part in (".", "..") for part in rel.parts):
        raise ValueError(f"unsafe plugin defaults destination: {root / rel}")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"symlink in plugin defaults destination: {path}")
    return path


#: Home-relative directories whose entries name every plugin that seeded the home.
SEED_MARKER_DIRS = (Path(".mimir") / "seed-plugin-defaults", Path(".mimir") / "upgrade-plugin-defaults")
#: First-party providers whose ``<name>-*`` home skills are always a seed namespace.
FIRST_PARTY_SEED_NAMESPACES = frozenset({"cultivate"})


@dataclass(frozen=True)
class SeedNamespaces:
    """Which home skills and scheduler jobs plugin defaults seeded.

    ``skills`` and ``jobs`` map an exact seeded skill-directory or job name to
    the installed plugin whose inventoried defaults declare it. ``prefixes``
    lists plugin names whose whole ``<name>-*`` namespace is a seed namespace:
    first-party providers, plus any plugin whose seed marker is in the home even
    when it is no longer installed. Longest names come first.
    """

    skills: Mapping[str, str]
    jobs: Mapping[str, str]
    prefixes: tuple[str, ...]

    def _owner(self, exact: Mapping[str, str], name: str) -> str | None:
        if name in exact:
            return exact[name]
        return next((plugin for plugin in self.prefixes if name.startswith(f"{plugin}-")), None)

    def skill_owner(self, skill_dir_name: str) -> str | None:
        """The plugin that seeds ``skills/<skill_dir_name>``, or ``None`` for core."""
        return self._owner(self.skills, skill_dir_name)

    def job_owner(self, job_name: str) -> str | None:
        """The plugin that seeds scheduler job ``job_name``, or ``None`` for core."""
        return self._owner(self.jobs, job_name)


def _seeded_job_names(candidate: PluginCandidate) -> set[str]:
    """Names seeding gives a plugin's declared jobs; reads package data only."""
    manifest = candidate.manifest
    dist = candidate.entry_point.dist
    if manifest is None or not manifest.defaults or dist is None or dist.files is None:
        return set()
    roots = [item for item in dist.files if item.name == "mimir-plugin.toml"]
    if len(roots) != 1:
        return set()
    root = Path(dist.locate_file(roots[0])).parent
    names: set[str] = set()
    for resource in manifest.defaults.get("scheduler_jobs", ()):
        try:
            raw = yaml.safe_load((root / resource).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        entries = raw.get("jobs") if isinstance(raw, dict) else raw
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(f"{candidate.name}-{entry['name'].strip()}")
    return names


def seed_namespaces(home: Path) -> SeedNamespaces:
    """Identify what plugin defaults seeded into ``home``, without imports.

    Only package metadata, package data and marker names are read; no provider
    module is imported, so a deselected provider stays unloaded.
    """
    skills: dict[str, str] = {}
    jobs: dict[str, str] = {}
    prefixes = set(FIRST_PARTY_SEED_NAMESPACES)
    for marker_dir in SEED_MARKER_DIRS:
        root = Path(home) / marker_dir
        try:
            prefixes.update(entry.name for entry in root.iterdir()
                            if entry.name and not entry.name.startswith("."))
        except OSError:
            continue
    try:
        candidates = discover_plugins(enable="")
    except Exception:  # metadata problems must not hide the marker namespaces
        candidates = ()
    for candidate in candidates:
        manifest = candidate.manifest
        for directory in ((*manifest.defaults.get("skills", ()), *(path for path, _ in manifest.optional_skills))
                          if manifest else ()):
            skills.setdefault(f"{candidate.name}-{Path(directory).name}", candidate.name)
        for name in _seeded_job_names(candidate):
            jobs.setdefault(name, candidate.name)
    return SeedNamespaces(MappingProxyType(skills), MappingProxyType(jobs),
                          tuple(sorted(prefixes, key=len, reverse=True)))


def selected_plugin_owners(policy: "PolicyRegistry | None") -> frozenset[str]:
    """Plugins whose seeds may run: selected, and first-party where that matters."""
    if policy is None:
        return frozenset()
    names = {candidate.name for candidate in policy.selected_plugin_candidates}
    if "cultivate" in names and not policy._selected_first_party("cultivate"):
        names.discard("cultivate")
    return frozenset(names)


def unselected_seeded_skills(home: Path, selected: frozenset[str]) -> dict[str, str]:
    """Map each ``<home>/skills/<dir>`` seeded by an unselected plugin to that plugin."""
    root = Path(home) / "skills"
    try:
        entries = [entry.name for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return {}
    namespaces = seed_namespaces(home)
    hidden = {}
    for name in entries:
        owner = namespaces.skill_owner(name)
        if owner is not None and owner not in selected:
            hidden[name] = owner
    return hidden


def plugin_default_files(candidate: PluginCandidate, *, skill: str | None = None) -> dict[Path, str]:
    """Read only inventoried package data, never the provider's Python module."""
    manifest = candidate.manifest
    if manifest is None or (not manifest.defaults and skill is None):
        return {}
    dist = candidate.entry_point.dist
    if dist is None or dist.files is None:
        raise ValueError(f"{candidate.name}: missing distribution inventory")
    manifests = [item for item in dist.files if item.name == "mimir-plugin.toml"]
    if len(manifests) != 1:
        raise ValueError(f"{candidate.name}: missing unique plugin manifest")
    root = Path(dist.locate_file(manifests[0])).parent.resolve()
    inventory = {Path(dist.locate_file(item)).resolve() for item in dist.files}
    output: dict[Path, str] = {}

    def read(path: Path) -> str:
        if path.resolve() not in inventory or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f"{candidate.name}: invalid defaults resource {path}")
        return path.read_text(encoding="utf-8")

    directories = (manifest.defaults.get("skills", ()) if skill is None else
                   [path for path in (*manifest.defaults.get("skills", ()),
                                      *(path for path, _ in manifest.optional_skills))
                    if Path(path).name == skill])
    if skill is not None and len(directories) != 1:
        raise ValueError(f"{candidate.name}: undeclared skill {skill!r}")
    for directory in directories:
        source = root / directory
        for path in sorted(source.rglob("*")):
            if {"__pycache__", ".pytest_cache"}.intersection(path.relative_to(source).parts):
                continue
            if path.is_file():
                rel = path.relative_to(source)
                dest = Path("skills") / f"{candidate.name}-{source.name}" / rel
                if dest in output:
                    raise ValueError(f"{candidate.name}: duplicate skill path {dest}")
                output[dest] = read(path)
    for resource in (manifest.defaults.get("prompts", ()) if skill is None else ()):
        path = root / resource
        rel = Path("prompts") / candidate.name / path.name
        if rel in output:
            raise ValueError(f"{candidate.name}: duplicate prompt name {path.name}")
        output[rel] = read(path)

    jobs = []
    from mimir.scheduler import load_jobs_from_text

    for resource in (manifest.defaults.get("scheduler_jobs", ()) if skill is None else ()):
        text = read(root / resource)
        parsed, rejections = load_jobs_from_text(text)
        if rejections:
            raise ValueError(f"{candidate.name}: invalid scheduler defaults: {rejections}")
        for job in parsed:
            entry = job.to_yaml_entry()
            entry["name"] = f"{candidate.name}-{job.name}"
            if job.prompt_file:
                if Path(job.prompt_file).name != job.prompt_file:
                    raise ValueError(f"{candidate.name}: scheduler prompt_file must be a basename")
                entry["prompt_file"] = f"{candidate.name}/{job.prompt_file}"
            jobs.append(entry)
    if len({job["name"] for job in jobs}) != len(jobs):
        raise ValueError(f"{candidate.name}: duplicate scheduler job")
    if jobs:
        output[Path("plugins") / candidate.name / "scheduler.yaml"] = yaml.safe_dump(jobs, sort_keys=False)
    return output


def seed_plugin_defaults(home: Path, *, selected: tuple[PluginCandidate, ...] | None = None) -> dict[str, str]:
    """Seed missing files and initial jobs; later jobs go to upgrade proposals."""
    if selected is None:
        selected = enabled_plugins()
    home = Path(home)
    result: dict[str, str] = {}
    owned_files: dict[Path, str] = {}
    packages = []
    for candidate in selected:
        files = plugin_default_files(candidate)
        for rel in files:
            previous = owned_files.setdefault(rel, candidate.name)
            if previous != candidate.name:
                raise ValueError(f"plugin defaults path {rel} claimed by {previous} and {candidate.name}")
        packages.append((candidate, files))
    # Validate every target before creating any directories or files. In
    # particular, never follow an operator symlink into core or outside home.
    for candidate, files in packages:
        for rel in files:
            if rel.parts[0] != "plugins":
                safe_destination(home, rel)
        if candidate.manifest and candidate.manifest.defaults:
            for rel in (
                Path("scheduler.yaml"),
                Path(".mimir") / "seed-plugin-defaults" / candidate.name,
                Path(".mimir") / "upgrade-plugin-defaults" / candidate.name,
            ):
                safe_destination(home, rel)
    for candidate, files in packages:
        for rel, text in files.items():
            if rel.parts[0] == "plugins":
                continue
            dest = safe_destination(home, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            safe_destination(home, rel)
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                result[rel.as_posix()] = "existing"
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                result[rel.as_posix()] = "created"
        jobs = files.get(Path("plugins") / candidate.name / "scheduler.yaml")
        if candidate.manifest and candidate.manifest.defaults:
            marker = safe_destination(home, Path(".mimir") / "seed-plugin-defaults" / candidate.name)
            synced = safe_destination(home, Path(".mimir") / "upgrade-plugin-defaults" / candidate.name)
            scheduler = safe_destination(home, Path("scheduler.yaml"))
        if candidate.manifest and candidate.manifest.defaults and not marker.exists() and not synced.exists():
            if jobs and scheduler.is_file():
                from mimir.defaults_upgrade import _merge_scheduler_defaults

                original = scheduler.read_text(encoding="utf-8")
                merged, added = _merge_scheduler_defaults(
                    base_text="[]\n", their_text=jobs, home_text=original,
                    version=str(candidate.manifest.defaults["version"]),
                )
                if merged is not None:
                    safe_destination(home, Path("scheduler.yaml"))
                    scheduler.write_text(merged, encoding="utf-8")
                for name in added:
                    result[f"scheduler.yaml:{name}"] = "created"
            marker.parent.mkdir(parents=True, exist_ok=True)
            safe_destination(home, Path(".mimir") / "seed-plugin-defaults" / candidate.name)
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(str(candidate.manifest.defaults["version"]) + "\n")
    return result
