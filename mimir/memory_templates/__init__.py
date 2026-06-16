"""Default memory templates shipped with mimir.

Bundled markdown files under this package get seeded into a deployment
home on first setup. Operators are expected to edit them as their
deployment evolves — seed helpers only write a target when it does not
already exist, so customizations persist.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_TEMPLATES_ROOT = Path(__file__).parent
_CORE_TEMPLATES_ROOT = _TEMPLATES_ROOT / "core"


def _core_template_names() -> list[str]:
    """Bundled core-memory template basenames (e.g. ``00-identity.md``)."""
    if not _CORE_TEMPLATES_ROOT.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _CORE_TEMPLATES_ROOT.iterdir()
        if entry.is_file() and entry.suffix == ".md"
    )


def core_template_text(name: str) -> str:
    """Return the bundled core-memory template text for ``name``."""
    if "/" in name or name.startswith("."):
        raise ValueError(f"invalid core-memory template name: {name!r}")
    path = _CORE_TEMPLATES_ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f"unknown core-memory template: {name}")
    return path.read_text(encoding="utf-8")


def bundled_defaults() -> dict[str, str]:
    """Bundled core-memory defaults keyed by basename."""
    return {
        name: core_template_text(name)
        for name in _core_template_names()
    }


def seed_core_memory(home: Path) -> dict[str, str]:
    """Copy missing core-memory templates to ``<home>/memory/core/``.

    Only writes templates that do not already exist at the target path
    — operator customizations are preserved across re-runs. Returns a
    ``{name: status}`` map for telemetry. Statuses: ``"created"``,
    ``"present"`` (target already exists; left alone), ``"skipped"``
    (copy failed).
    """
    target_root = home / "memory" / "core"
    target_root.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in _core_template_names():
        src = _CORE_TEMPLATES_ROOT / name
        dst = target_root / name
        if dst.exists():
            out[name] = "present"
            continue
        try:
            shutil.copy2(src, dst)
            out[name] = "created"
            log.info("seeded default core memory: %s", dst)
        except OSError as exc:
            log.warning("seed_core_memory: failed to copy %s: %s", name, exc)
            out[name] = "skipped"
    return out


# Onboarding bootstrap. NOT a ``core/`` template on purpose: the regular
# seed loop above is write-if-missing, which would recreate this block on
# every ``setup`` re-run — but the onboarding skill DELETES it when
# onboarding completes, and recreating it would re-trigger onboarding
# forever. So it lives here as a constant and is seeded only on a
# brand-new home (the caller gates on first-ever setup via
# ``seed_init_block``). It loads as ``memory/core/01-init.md`` — right
# after identity — so it's high in the every-turn system prompt.
INIT_BLOCK_NAME = "01-init.md"

INIT_BLOCK_TEXT = """\
<!-- desc: first-run bootstrap for a brand-new agent — load the onboarding skill, then delete this block. -->
# Init

You're a new agent and haven't been onboarded yet.

Load the **onboarding** skill (it's in your skill catalog) and follow it: have
real conversations with your human to learn who they are and how you should
operate, and from those write your `persona`, `communication`, and schedule
blocks under `memory/core/`.

When you have a persona, a schedule, and you're doing useful work, **delete this
file** (`memory/core/01-init.md`). It's a one-time bootstrap — once it's gone,
onboarding won't keep re-triggering.
"""


def seed_init_block(home: Path) -> str:
    """Write the first-run onboarding bootstrap to ``<home>/memory/core/01-init.md``.

    Write-if-missing, like the core templates — but the CALLER must gate
    this on a fresh home (first-ever setup). Do NOT call it on an
    established home: the onboarding skill deletes the block when done,
    and recreating it would re-trigger onboarding. Returns ``"created"``,
    ``"present"`` (already there; left alone), or ``"skipped"`` (write
    failed).
    """
    target = home / "memory" / "core" / INIT_BLOCK_NAME
    if target.exists():
        return "present"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(INIT_BLOCK_TEXT, encoding="utf-8")
        log.info("seeded onboarding bootstrap (fresh home): %s", target)
    except OSError as exc:
        log.warning("seed_init_block: failed to write %s: %s", target, exc)
        return "skipped"
    return "created"


DEFAULT_IDENTITY_MD = core_template_text("00-identity.md")
DEFAULT_NON_GOALS = core_template_text("05-non-goals.md")
DEFAULT_ACTION_BOUNDARIES = core_template_text("06-action-boundaries.md")
DEFAULT_VSM_TERMS = core_template_text("20-vsm-terms.md")
DEFAULT_REFLECTION_POLICY = core_template_text("30-reflection-policy.md")
DEFAULT_LEARNED_BEHAVIORS = core_template_text("40-learned-behaviors.md")
DEFAULT_HEARTBEAT_PATTERNS = core_template_text("50-heartbeat-patterns.md")
DEFAULT_FILING_RULES = core_template_text("60-filing-rules.md")
