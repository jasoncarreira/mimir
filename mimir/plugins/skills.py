"""Selected plugins' installable package skills; no provider module imports."""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .defaults import plugin_default_files, safe_destination
from .discovery import PluginCandidate, discover_plugins, enabled_plugins


@dataclass(frozen=True)
class PluginSkill:
    candidate: PluginCandidate
    name: str
    forge: str | None
    default: bool

    @property
    def label(self) -> str:
        return f"{self.candidate.name}:{self.name}"

    @property
    def dirname(self) -> str:
        return f"{self.candidate.name}-{self.name}"


def selected_skills() -> list[PluginSkill]:
    result = []
    for candidate in enabled_plugins():
        manifest = candidate.manifest
        for path in manifest.defaults.get("skills", ()):
            result.append(PluginSkill(candidate, Path(path).name, None, True))
        for path, forge in manifest.optional_skills:
            result.append(PluginSkill(candidate, Path(path).name, forge, False))
    return result


def skill_files(skill: PluginSkill) -> dict[Path, str]:
    return plugin_default_files(skill.candidate, skill=skill.name)


def skill_state(home: Path, skill: PluginSkill) -> str:
    """Match the whole installed tree against the inventoried package files."""
    try:
        root = safe_destination(home, Path("skills") / skill.dirname)
    except ValueError:
        return "modified"
    if not root.exists():
        return "not installed"
    if not root.is_dir():
        return "modified"
    expected = {path.relative_to(Path("skills") / skill.dirname): text.encode("utf-8")
                for path, text in skill_files(skill).items()}
    actual: dict[Path, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            return "modified"
        if path.is_file():
            actual[path.relative_to(root)] = path.read_bytes()
    return "installed" if actual == expected else "modified"


def resolve_skill(name: str, core_names: set[str]) -> PluginSkill | None:
    from mimir.skill_install import _validate_skill_name

    if ":" in name:
        owner, _, local = name.partition(":")
        _validate_skill_name(owner)
        _validate_skill_name(local)
    else:
        _validate_skill_name(name)
        owner, local = None, name
    matches = [s for s in selected_skills() if s.name == local and (owner is None or s.candidate.name == owner)]
    if (owner is None and local in core_names and matches) or len(matches) > 1:
        raise ValueError(f"ambiguous skill {name!r}; use <plugin>:<skill>")
    if matches:
        return matches[0]
    if owner is not None or local not in core_names:
        unavailable = [c.name for c in discover_plugins(enable="") if c.manifest and
                       (owner is None or c.name == owner) and
                       local in {Path(p).name for p in (*c.manifest.defaults.get("skills", ()),
                                                       *(p for p, _ in c.manifest.optional_skills))}]
        if unavailable:
            raise ValueError(f"skill {name!r} belongs to an unselected plugin ({', '.join(unavailable)}); "
                             "select it with MIMIR_PLUGINS")
        if owner is None:
            return None  # core installer supplies its usual missing-source diagnostic
        raise ValueError(f"plugin skill {name!r} unavailable; check MIMIR_PLUGINS and `mimir skills list-optional`")
    return None


def install_skill(home: Path, skill: PluginSkill, *, restore: bool = False) -> tuple[Path, Path | None]:
    """Install an inventoried skill; on restore preserve the old tree first."""
    root = safe_destination(home, Path("skills"))
    dest_rel = Path("skills") / skill.dirname
    dest = safe_destination(home, dest_rel)
    if dest.exists() and not restore:
        raise FileExistsError(f"{dest} already exists ({skill_state(home, skill)}); "
                              "use --restore to back up and replace it")
    if dest.is_symlink():
        raise ValueError(f"symlink in plugin skill destination: {dest}")
    if restore and not dest.exists():
        raise ValueError(f"{dest} is not installed; omit --restore")
    files = skill_files(skill)
    for rel in files:
        safe_destination(home, rel)
    root.mkdir(parents=True, exist_ok=True)
    safe_destination(home, dest_rel)
    stage = Path(tempfile.mkdtemp(prefix=f".{skill.dirname}-", dir=root))
    backup = None
    try:
        for rel, text in files.items():
            target = safe_destination(stage, rel.relative_to(dest_rel))
            target.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644),
                           "w", encoding="utf-8") as stream:
                stream.write(text)
        # Only defaults seeding may mark initial scheduler jobs as seeded.
        stage.chmod(0o755)
        safe_destination(home, dest_rel)
        if restore:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_rel = Path(".mimir/plugin-skill-backups") / f"{skill.dirname}-{stamp}"
            backup = safe_destination(home, backup_rel)
            backup.parent.mkdir(parents=True, exist_ok=True)
            safe_destination(home, backup_rel)
            if backup.exists():
                raise FileExistsError(f"backup already exists: {backup}")
            dest.rename(backup)
        try:
            stage.rename(dest)
        except BaseException:
            if backup is not None:
                backup.rename(dest)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return dest, backup


def log_optional_hint(home: Path, candidates, logger) -> None:
    """One boot hint for missing optionals, using the frozen selection only."""
    missing = uninstalled_optional(home, [
        PluginSkill(candidate, Path(path).name, forge, False)
        for candidate in candidates
        for path, forge in (candidate.manifest.optional_skills if candidate.manifest else ())
    ])
    if missing:
        logger.info("optional plugin skills available: %s; install with `mimir skills install <plugin>:<skill>` "
                    "or `mimir skills install --forge <name>`",
                    ", ".join(skill.label for skill in missing))


def uninstalled_optional(home: Path, selected: list[PluginSkill] | None = None) -> list[PluginSkill]:
    return [skill for skill in (selected if selected is not None else selected_skills())
            if not skill.default and skill_state(home, skill) == "not installed"]
