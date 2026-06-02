"""Default operator prompt templates shipped with mimir.

Bundled markdown files under this package get seeded into a deployment
home's ``<home>/prompts/`` directory on first setup. Operators are
expected to edit or remove them as their deployment evolves —
:func:`seed_prompts` only writes a target when it doesn't already
exist, so customizations persist.

Currently seeded:

- ``heartbeat.md`` — perch-tick workflow body (mirrors open-strix-base's
  perch-tick pattern). Operator pairs it with a ``scheduler.yaml``
  entry that fires this prompt on a cadence.
- ``reflect.md`` — weekly cross-session audit workflow. Wraps the
  ``mimir reflection`` CLI subcommands (``most-retrieved``,
  ``introspection-report``, ``mark-applied``).

The intent: a fresh ``mimir setup`` produces a deployment that already
has a reasonable autonomous-work cadence wired up. Operators delete
or modify per their needs.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_TEMPLATES_ROOT = Path(__file__).parent


def _template_names() -> list[str]:
    """Bundled prompt template basenames (e.g. ``heartbeat.md``)."""
    if not _TEMPLATES_ROOT.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _TEMPLATES_ROOT.iterdir()
        if entry.is_file() and entry.suffix == ".md"
    )


def bundled_defaults() -> dict[str, str]:
    """Bundled prompt defaults keyed by basename."""
    return {
        name: (_TEMPLATES_ROOT / name).read_text(encoding="utf-8")
        for name in _template_names()
    }


def seed_prompts(home: Path) -> dict[str, str]:
    """Copy missing prompt templates to ``<home>/prompts/<name>.md``.

    Only writes templates that don't already exist at the target path
    — operator customizations are preserved across re-runs. Returns a
    ``{name: status}`` map for telemetry. Statuses: ``"created"``,
    ``"present"`` (target already exists; left alone), ``"skipped"``
    (copy failed).

    Idempotent — a clean run after the first one is a no-op (everything
    reports ``"present"``).
    """
    target_root = home / "prompts"
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in _template_names():
        src = _TEMPLATES_ROOT / name
        dst = target_root / name
        if dst.exists():
            out[name] = "present"
            continue
        try:
            shutil.copy2(src, dst)
            out[name] = "created"
            log.info("seeded default prompt: %s", dst)
        except OSError as exc:
            log.warning("seed_prompts: failed to copy %s: %s", name, exc)
            out[name] = "skipped"
    return out
