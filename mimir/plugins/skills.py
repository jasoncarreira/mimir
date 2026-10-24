"""Selected plugins' installable package skills; no provider module imports."""

from __future__ import annotations

import datetime
import hashlib
import json
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
    return skills_for_candidates(enabled_plugins())


def skills_for_candidates(candidates) -> list[PluginSkill]:
    result = []
    for candidate in candidates:
        manifest = candidate.manifest
        if manifest is None:
            continue
        for path in manifest.defaults.get("skills", ()):
            result.append(PluginSkill(candidate, Path(path).name, None, True))
        for path, forge in manifest.optional_skills:
            result.append(PluginSkill(candidate, Path(path).name, forge, False))
    return result


def skill_files(skill: PluginSkill) -> dict[Path, str]:
    return plugin_default_files(skill.candidate, skill=skill.name)


def skill_inventory(skill: PluginSkill) -> dict[str, str]:
    """Current package's complete relative-path/sha256 inventory."""
    prefix = Path("skills") / skill.dirname
    return {rel.relative_to(prefix).as_posix(): hashlib.sha256(text.encode("utf-8")).hexdigest()
            for rel, text in skill_files(skill).items()}


def previous_skill_inventories(skill: PluginSkill) -> dict[str, dict[str, str]]:
    """Read the selected distribution's shipped previous-release inventories."""
    dist = skill.candidate.entry_point.dist
    if dist is None or dist.files is None:
        return {}
    matches = [item for item in dist.files if item.as_posix().endswith("/defaults/skill_releases.json")]
    if not matches:
        return {}
    if len(matches) != 1:
        raise ValueError(f"{skill.label}: ambiguous release inventory")
    path = Path(dist.locate_file(matches[0]))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{skill.label}: invalid release inventory")
    releases = json.loads(path.read_text(encoding="utf-8"))
    return {version: files[skill.name] for version, files in releases.items()
            if skill.name in files}


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
    expected = skill_inventory(skill)
    actual: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            return "modified"
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual == expected:
        return "installed"
    return ("upgrade available" if any(actual == previous for previous in previous_skill_inventories(skill).values())
            else "modified")


def pending_skill_upgrades(home: Path, selected: list[PluginSkill] | None = None) -> list[PluginSkill]:
    return [skill for skill in (selected if selected is not None else selected_skills())
            if skill_state(home, skill) == "upgrade available"]


def upgrade_command(skill: PluginSkill) -> str:
    return f"mimir skills install {skill.label} --restore"


async def notify_skill_upgrades(home: Path, candidates, operator_channel: str, send) -> None:
    """Emit one content-free event and one operator alert per skill/release."""
    from mimir.event_logger import log_event

    for skill in pending_skill_upgrades(home, skills_for_candidates(candidates)):
        version = str(skill.candidate.manifest.defaults.get("version", skill.candidate.manifest.version))
        marker = Path(".mimir/plugin-skill-upgrades") / skill.dirname / version
        for kind in ("event", "alert"):
            if kind == "alert" and not operator_channel:
                continue
            path = safe_destination(home, marker / kind)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w"):
                    pass
            except FileExistsError:
                continue
            try:
                if kind == "event":
                    await log_event("plugin_skill_upgrade_available", skill=skill.label, version=version)
                else:
                    await send(operator_channel, f"Plugin skill upgrade available: {skill.label}. "
                               f"Run `{upgrade_command(skill)}` to apply it (backup kept).", final=True)
            except Exception:
                path.unlink(missing_ok=True)
                raise


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
        # Publish with the seeder's normal directory permissions, not mkdtemp's 0700.
        # Do not write seed-plugin-defaults: only the seeder owns scheduler state.
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
    for skill in pending_skill_upgrades(home, skills_for_candidates(candidates)):
        logger.info("plugin skill upgrade available: %s; run `%s`", skill.label, upgrade_command(skill))


def uninstalled_optional(home: Path, selected: list[PluginSkill] | None = None) -> list[PluginSkill]:
    return [skill for skill in (selected if selected is not None else selected_skills())
            if not skill.default and skill_state(home, skill) == "not installed"]
