"""Bundled + operator skills location (SPEC §8.4).

**Architecture (post-2026-05-22, restoring open-strix-base's pattern):**

Skills live in two locations under ``<home>/``:

* ``<home>/skills/<name>/`` — **operator-installed and customized skills**.
  Operator-writable; tracked in deployment git (via the deployment's
  ``.gitignore`` allowlist). New skills land here via
  ``mimir skills install``; operator can edit freely.

* ``<home>/.mimir_builtin_skills/<name>/`` — **bundled skills, read-only**.
  Refreshed from ``mimir/skills/<name>/`` (in the mimir package) on every
  startup. Gitignored. The operator never edits these directly; to
  override a bundled skill, install a same-named skill in
  ``<home>/skills/`` (operator location wins on name collision per the
  framework's last-source-wins rule).

The dual-location split mirrors open-strix-base's pattern. Both paths
get passed to deepagents' ``SkillsMiddleware`` via the ``skills=`` kwarg
on ``create_deep_agent``; the framework discovers SKILL.md files in
both, with operator-location entries shadowing bundle entries when
names collide.

**Migration**: existing deployments with skills under
``<home>/.claude/skills/`` get their content moved to ``<home>/skills/``
on first run via :func:`migrate_legacy_skills_dir`. Idempotent (no-op
after the first run, since the source dir is gone). After migration the
legacy path is dead data the operator can delete.

The bundled refresh is **unconditional** (overwrites every startup) so
the read-only bundle always matches the mimir source. This differs from
the legacy ``seed_skills`` semantics which only-copied-if-missing —
operator customizations of bundled skills are no longer supported in
place. Customize by installing under ``<home>/skills/<name>/`` instead;
the framework's name-collision shadowing makes that the operator's
override.

**Poller workflows** live alongside skills under ``<home>/skills/``
with a ``pollers.json`` manifest as the polling-infra marker. They
appear in the discoverable catalog like any other skill (matches
open-strix-base's pattern — the catalog describes what each poller
does, while the poller infrastructure runs them on cron via the
manifest). The catalog reader and the poller scheduler are
independent of each other.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def reject_escaping_symlinks(src: Path) -> None:
    """Raise ``ValueError`` if any symlink under ``src`` resolves outside
    ``src`` (#500).

    Skill copy uses ``copytree(symlinks=True)`` to preserve links rather than
    dereference their contents at copy time, but a later skills-catalog read
    would still follow an escaping link (e.g. a bundle smuggling ``leak.txt ->
    /etc/passwd`` or ``-> <home>/.env``), pulling host-file contents into the
    model prompt. Reject such a bundle outright before copying. Links that stay
    within the bundle are allowed."""
    src_resolved = src.resolve()
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        for entry in (*dirnames, *filenames):
            path = Path(dirpath) / entry
            if not path.is_symlink():
                continue
            target = path.resolve()
            if not target.is_relative_to(src_resolved):
                raise ValueError(
                    f"skill bundle {src.name!r} contains a symlink escaping the "
                    f"bundle ({path} -> {target}); refusing install"
                )

#: Directory under the agent's home where bundled skills get refreshed
#: every startup. Read-only by convention; gitignored in deployments.
BUILTIN_SKILLS_DIR_NAME = ".mimir_builtin_skills"

#: Directory under the agent's home where operator-installed and
#: customized skills live. Tracked in deployment git (allowlisted).
SKILLS_DIR_NAME = "skills"

#: Legacy location from when skills lived under ``.claude/skills/``.
#: Read-only for migration; never written to in new code.
_LEGACY_SKILLS_DIR_NAME = ".claude/skills"

_BUNDLED_ROOT = Path(__file__).parent / "skills"

#: Default ``scheduler.yaml`` shipped with mimir. Seeded to the
#: deployment home on first setup so a fresh install has working
#: heartbeat + reflect entries out of the gate. Operators edit /
#: remove entries as their deployment evolves; subsequent runs
#: don't overwrite — :func:`seed_scheduler` only writes when the
#: target file is missing.
_BUNDLED_SCHEDULER = Path(__file__).parent / "scheduler_template.yaml"


def seed_scheduler(home: Path) -> str:
    """Seed ``<home>/scheduler.yaml`` from the bundled template if it
    doesn't already exist. Returns the status: ``"created"`` when
    seeded, ``"present"`` when the target was left alone, ``"skipped"``
    on copy failure or missing template.

    Operator customizations to ``<home>/scheduler.yaml`` are preserved
    across re-runs (the function only writes when target is absent).
    """
    target = home / "scheduler.yaml"
    if target.exists():
        return "present"
    if not _BUNDLED_SCHEDULER.is_file():
        log.warning("seed_scheduler: bundled template missing at %s", _BUNDLED_SCHEDULER)
        return "skipped"
    try:
        shutil.copy2(_BUNDLED_SCHEDULER, target)
        log.info("seeded default scheduler.yaml at %s", target)
        return "created"
    except OSError as exc:
        log.warning("seed_scheduler: copy failed: %s", exc)
        return "skipped"


def home_skills_dir(home: Path) -> Path:
    """The operator-writable skills directory: ``<home>/skills``."""
    return home / SKILLS_DIR_NAME


def home_builtin_skills_dir(home: Path) -> Path:
    """The read-only bundled skills directory:
    ``<home>/.mimir_builtin_skills``. Refreshed from the package on
    every startup."""
    return home / BUILTIN_SKILLS_DIR_NAME


def _bundled_skill_names() -> list[str]:
    """List skill folders that ship with the package."""
    if not _BUNDLED_ROOT.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _BUNDLED_ROOT.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def installed_skill_names(home: Path) -> list[str]:
    """Enumerate every skill currently available in the home —
    operator-installed (under ``<home>/skills/``) plus bundled (under
    ``<home>/.mimir_builtin_skills/``). Operator entries shadow bundled
    entries when names collide, so the returned set is the deduped
    union.

    Returns the bundled set alone when neither directory exists yet
    (fresh-install home before first ``refresh_builtin_skills`` runs).
    """
    names: set[str] = set()
    for d in (home_skills_dir(home), home_builtin_skills_dir(home)):
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
    if not names:
        return sorted(_bundled_skill_names())
    return sorted(names)


def migrate_legacy_skills_dir(home: Path) -> dict[str, str]:
    """One-shot migration: move ``<home>/.claude/skills/<name>/`` →
    ``<home>/skills/<name>/`` for any skill at the old path that isn't
    already at the new operator location.

    Idempotent — running again after the move is a no-op (the source is
    gone or empty). Returns a ``{name: status}`` map for telemetry.
    Statuses: ``"moved"``, ``"new_path_exists"`` (legacy entry left
    alone because the operator location already has this skill),
    ``"skipped"`` (move failed).

    Called from setup BEFORE :func:`refresh_builtin_skills` so the
    operator's customizations land at their canonical location before
    the bundled refresh touches the builtin directory.
    """
    legacy = home / _LEGACY_SKILLS_DIR_NAME
    target_root = home_skills_dir(home)
    if not legacy.is_dir():
        return {}
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for entry in legacy.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        dst = target_root / name
        if dst.exists():
            out[name] = "new_path_exists"
            continue
        try:
            shutil.move(str(entry), str(dst))
            out[name] = "moved"
            log.info(
                "migrated skill %s from .claude/skills/ to skills/", name,
            )
        except OSError as exc:
            log.warning(
                "migrate_legacy_skills_dir: failed to move %s: %s", name, exc,
            )
            out[name] = "skipped"
    return out


def refresh_builtin_skills(home: Path) -> dict[str, str]:
    """Sync bundled skills from ``mimir/skills/`` (the package) to
    ``<home>/.mimir_builtin_skills/`` (the operator's home, read-only
    by convention).

    Unlike the legacy ``seed_skills``, this **always overwrites** — the
    bundle is the source of truth, never operator-edited in place.
    Operators who want to customize a bundled skill install a
    same-named skill under ``<home>/skills/<name>/``; the framework's
    last-source-wins shadowing picks up the operator copy.

    Returns ``{name: status}`` where status is ``"refreshed"`` or
    ``"skipped"`` (copy failed). Per-skill copy is atomic: each lands
    in a sibling ``<name>.tmp`` directory then renames into place.
    """
    target_root = home_builtin_skills_dir(home)
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in _bundled_skill_names():
        src = _BUNDLED_ROOT / name
        dst = target_root / name
        tmp = target_root / f"{name}.tmp"

        # Clean up any half-copy from a prior crashed run.
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

        try:
            # Defense-in-depth (#500): bundled skills are trusted, but reject
            # any escaping symlink and preserve safe links instead of
            # dereferencing their contents.
            reject_escaping_symlinks(src)
            shutil.copytree(src, tmp, symlinks=True)
            # Atomic-replace: remove old, rename tmp into place.
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            tmp.rename(dst)
            out[name] = "refreshed"
        except (OSError, ValueError) as exc:
            log.warning(
                "refresh_builtin_skills: failed to copy %s: %s", name, exc,
            )
            shutil.rmtree(tmp, ignore_errors=True)
            out[name] = "skipped"
    return out


# Backward-compatibility alias for the legacy seed_skills name. New
# code should use refresh_builtin_skills directly; this alias exists
# so the migration PR can land without breaking external imports
# (e.g. test harnesses, ad-hoc scripts).
def seed_skills(home: Path) -> dict[str, str]:
    """Deprecated alias — calls :func:`refresh_builtin_skills`."""
    return refresh_builtin_skills(home)
