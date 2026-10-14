"""Copy selected plugins' validated package defaults into their home namespaces."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .discovery import PluginCandidate, enabled_plugins


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


def plugin_default_files(candidate: PluginCandidate) -> dict[Path, str]:
    """Read only inventoried package data, never the provider's Python module."""
    manifest = candidate.manifest
    if manifest is None or not manifest.defaults:
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

    for directory in manifest.defaults.get("skills", ()):
        source = root / directory
        for path in sorted(source.rglob("*")):
            if path.is_file():
                rel = path.relative_to(source)
                dest = Path("skills") / f"{candidate.name}-{source.name}" / rel
                if dest in output:
                    raise ValueError(f"{candidate.name}: duplicate skill path {dest}")
                output[dest] = read(path)
    for resource in manifest.defaults.get("prompts", ()):
        path = root / resource
        rel = Path("prompts") / candidate.name / path.name
        if rel in output:
            raise ValueError(f"{candidate.name}: duplicate prompt name {path.name}")
        output[rel] = read(path)

    jobs = []
    from mimir.scheduler import load_jobs_from_text

    for resource in manifest.defaults.get("scheduler_jobs", ()):
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
