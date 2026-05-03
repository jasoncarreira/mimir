"""Bundled skills (SPEC §8.4).

Mimir ships a curated set of skills (mostly ports from open-strix-base)
under ``mimir/skills/<name>/`` in the package. They get copied to
``<home>/.claude/skills/<name>/`` at startup so the CLI's filesystem-skill
loader picks them up via ``cwd=<home>``.

Existing user customizations are preserved — ``seed_skills`` only writes
files that are missing. To force a re-seed, delete the relevant skill
directory in the agent home before startup.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_BUNDLED_ROOT = Path(__file__).parent / "skills"


def _bundled_skill_names() -> list[str]:
    """List all skill folders that ship with the package."""
    if not _BUNDLED_ROOT.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _BUNDLED_ROOT.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def installed_skill_names(home: Path) -> list[str]:
    """Enumerate every skill currently installed under
    ``<home>/.claude/skills/`` — bundled + user-added. Used by the
    §12.3 ranker so user-installed skills appear in the prompt's
    ``## Skills`` block alongside bundled ones."""
    skills_dir = home / ".claude" / "skills"
    if not skills_dir.is_dir():
        return sorted(_bundled_skill_names())
    on_disk = {
        entry.name
        for entry in skills_dir.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }
    # Union with bundled names so a fresh-install home (skills not yet
    # seeded) still shows the bundled set rather than nothing.
    return sorted(on_disk | set(_bundled_skill_names()))


def seed_skills(home: Path) -> dict[str, str]:
    """Copy missing skill folders to ``<home>/.claude/skills/<name>/``.

    Returns a ``{name: status}`` map where status is ``"created"``,
    ``"present"``, or ``"skipped"`` (copy failed or destination is broken).
    User-installed skills already in the target dir are left alone — we
    only create *new* folders, never overwrite existing files.

    Atomicity: each copy lands in a sibling ``<name>.tmp`` directory and is
    renamed into place only after the copy completes. A crash mid-copy
    leaves the half-copy in ``.tmp`` (cleaned up on the next run); the
    canonical ``<name>/`` either fully exists or doesn't. A pre-existing
    ``<name>/`` missing its ``SKILL.md`` is treated as broken and re-seeded.
    """
    target_root = home / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in _bundled_skill_names():
        src = _BUNDLED_ROOT / name
        dst = target_root / name
        tmp = target_root / f"{name}.tmp"

        # Clean up any half-copy from a prior crashed run.
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

        if dst.exists():
            # Heuristic integrity check: a complete bundled skill has a
            # SKILL.md at the top. If it's missing, the dir is poisoned —
            # likely a partial copy from before the atomic-rename fix.
            # Replace it from the bundle.
            if (dst / "SKILL.md").is_file():
                out[name] = "present"
                continue
            log.warning(
                "seed_skills: %s missing SKILL.md, re-seeding from bundle",
                name,
            )
            shutil.rmtree(dst, ignore_errors=True)

        try:
            shutil.copytree(src, tmp)
            tmp.rename(dst)
            out[name] = "created"
        except OSError as exc:
            log.warning("seed_skills: failed to copy %s: %s", name, exc)
            shutil.rmtree(tmp, ignore_errors=True)
            out[name] = "skipped"
    return out
