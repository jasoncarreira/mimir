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


def seed_skills(home: Path) -> dict[str, str]:
    """Copy missing skill folders to ``<home>/.claude/skills/<name>/``.

    Returns a ``{name: status}`` map where status is ``"created"``,
    ``"present"``, or ``"skipped"`` (no SKILL.md in the bundled folder).
    User-installed skills already in the target dir are left alone — we
    only create *new* folders, never overwrite existing files.
    """
    target_root = home / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in _bundled_skill_names():
        src = _BUNDLED_ROOT / name
        dst = target_root / name
        if dst.exists():
            out[name] = "present"
            continue
        try:
            shutil.copytree(src, dst)
            out[name] = "created"
        except OSError as exc:
            log.warning("seed_skills: failed to copy %s: %s", name, exc)
            out[name] = "skipped"
    return out
