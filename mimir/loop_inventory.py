"""Scan the codebase for ``# VSM: <layer>`` and ``# loop_id: <id>`` tags
to produce a runtime inventory of mimir's feedback loops.

The two-line tag convention (FUTURE_WORK §12.6a):

    # VSM: <layer> — <one-line description>
    # loop_id: <FEEDBACK-LOOPS.md section number>
    def some_function(...):
        ...

is parseable by grep and by this module. ``mimir loops`` (§12.6b)
uses this inventory to print a per-loop status table; the same
inventory also acts as a lint target — if FEEDBACK-LOOPS.md
mentions a loop ID that no code carries, or vice versa, that's a
documentation drift warning.

Scanning lives at runtime instead of as a build step so the tags
stay in sync with whatever the venv has installed. Cost is one
recursive walk of mimir/ (including mimir/saga/) at CLI invocation time
(~50ms typical).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_VSM_TAG_RE = re.compile(
    r"^\s*#\s*VSM:\s*(?P<layer>[A-Za-z0-9*]+(?:\s*\([^)]+\))?)"
    r"\s*(?:[—-]\s*(?P<desc>.*))?$"
)
_LOOP_ID_RE = re.compile(r"^\s*#\s*loop_id:\s*(?P<id>[A-Za-z0-9._-]+)\s*$")


@dataclass
class LoopTag:
    """One feedback-loop entry-point declaration found in code."""

    file: Path
    line: int
    layer: str               # e.g. "S3", "S3*", "algedonic", "algedonic (in)"
    description: str
    loop_id: str | None      # e.g. "1.1", or None if missing
    target: str | None       # the def / class line that follows the tag


def scan(roots: list[Path]) -> list[LoopTag]:
    """Walk ``roots`` for ``.py`` files and extract VSM tags.

    The ``# VSM:`` line and the optional ``# loop_id:`` line must be
    consecutive (or separated only by other comment lines). The
    target def/class line is the first non-comment, non-blank line
    after them.
    """
    tags: list[LoopTag] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            # Skip the inventory itself + tests + caches.
            parts = path.parts
            if any(seg.startswith(("__pycache__", ".")) for seg in parts):
                continue
            if path.name == "loop_inventory.py":
                continue
            tags.extend(_scan_file(path))
    return tags


def _scan_file(path: Path) -> list[LoopTag]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    out: list[LoopTag] = []
    i = 0
    while i < len(lines):
        m = _VSM_TAG_RE.match(lines[i])
        if not m:
            i += 1
            continue

        layer = (m.group("layer") or "").strip()
        desc_parts = [(m.group("desc") or "").strip()]
        loop_id: str | None = None
        target: str | None = None
        tag_line = i + 1  # 1-based for human display

        # Look ahead for loop_id and continuation comment lines.
        j = i + 1
        while j < len(lines):
            line = lines[j]
            stripped = line.strip()
            if not stripped:
                # Blank line; stop the search-window for the tag block.
                break
            id_match = _LOOP_ID_RE.match(line)
            if id_match:
                loop_id = id_match.group("id")
                j += 1
                continue
            if stripped.startswith("#"):
                # Continuation of the description.
                cont = stripped.lstrip("#").strip()
                if cont:
                    desc_parts.append(cont)
                j += 1
                continue
            # First non-comment, non-blank line is the target.
            target = stripped
            break

        out.append(LoopTag(
            file=path,
            line=tag_line,
            layer=layer,
            description=" ".join(p for p in desc_parts if p),
            loop_id=loop_id,
            target=target,
        ))
        i = max(j, i + 1)
    return out


def default_roots() -> list[Path]:
    """Walk roots for the default ``mimir loops`` invocation. The mimir
    package covers the runtime memory backend (``mimir/saga/``) as well
    as the agent core; the bench-shell package at ``benchmarks/saga/``
    has no feedback-loop entry points and is skipped."""
    here = Path(__file__).resolve().parent.parent
    return [here / "mimir"]
