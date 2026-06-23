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
- ``commitments-review.md`` — scheduled commitments hygiene workflow.
- ``issues-audit.md`` — scheduled ``memory/issues`` notability and drift
  audit workflow.
- ``upgrade.md`` — version-triggered prompts/core defaults reconciliation
  turn for startup-opened upgrade-lane proposals.
- ``worklink-order.md`` — operator-run Worklink backend order template.
- ``decompose.md`` — Worklink planner/decomposer prompt template.

The intent: a fresh ``mimir setup`` produces a deployment that already
has a reasonable autonomous-work cadence wired up. Operators delete
or modify per their needs.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..worklink.planning import render_decompose_prompt

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


def _render_template(name: str, text: str) -> str:
    if name == "decompose.md":
        return render_decompose_prompt(text)
    return text


def bundled_defaults() -> dict[str, str]:
    """Bundled prompt defaults keyed by basename."""
    return {
        name: _render_template(name, (_TEMPLATES_ROOT / name).read_text(encoding="utf-8"))
        for name in _template_names()
    }


# Version-specific upgrade prompts (chainlink #645) live one level down so the
# flat seed/loader above ignores them (it only picks up top-level ``*.md``).
_UPGRADE_ROOT = _TEMPLATES_ROOT / "upgrades"


def bundled_upgrade_prompts() -> dict[str, str]:
    """One-shot upgrade prompts keyed by *target* mimir version.

    Files live at ``upgrades/<version>.md`` — the filename stem is the mimir
    version this prompt runs *on arriving at*. Unlike the templates above
    these are NOT seeded into ``<home>/prompts`` (they're framework migration
    nudges read from package data at dispatch time, not operator-owned files).
    The defaults-upgrade flow dispatches them once, cumulatively, for every
    target version crossed in a single upgrade. ``upgrades/README.md`` is
    skipped — it documents the authoring convention, it isn't a prompt.
    """
    if not _UPGRADE_ROOT.is_dir():
        return {}
    return {
        entry.stem: entry.read_text(encoding="utf-8")
        for entry in sorted(_UPGRADE_ROOT.iterdir())
        if entry.is_file() and entry.suffix == ".md" and entry.stem != "README"
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
            text = _render_template(name, src.read_text(encoding="utf-8"))
            dst.write_text(text, encoding="utf-8")
            out[name] = "created"
            log.info("seeded default prompt: %s", dst)
        except OSError as exc:
            log.warning("seed_prompts: failed to copy %s: %s", name, exc)
            out[name] = "skipped"
    return out
