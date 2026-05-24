"""Core memory blocks (SPEC §3.1, §5.1) and description extraction.

``memory/core/*.md`` files render directly into the system prompt every turn.
Lexicographic filename ordering (``00-...``, ``10-...``, ...) becomes prompt
ordering. The first line of each file may be ``<!-- desc: ... -->``; if not,
``first_sentence_fallback`` is used (also drives ``[auto]``-prefixed entries
in the auto-generated INDEX.md files).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ── Health-check thresholds ──────────────────────────────────────────────────
# Fires ``core_prompt_degraded`` (negative algedonic event) when either check
# fails. Calibrated conservatively: current production has 9 core files, so
# MIN_COUNT=5 catches a total wipe without flagging minor deliberate pruning.
# MIN_BYTES=200 catches empty or stub files (the smallest current block is
# well over 1 KB). Both constants are module-level so tests can patch them.
_CORE_BLOCKS_MIN_COUNT: int = 5
_CORE_BLOCKS_MIN_BYTES: int = 200

DESC_LINE_RE = re.compile(r"^\s*<!--\s*desc:\s*(.+?)\s*-->\s*$")
H1_LINE_RE = re.compile(r"^\s*#\s+")
SENTENCE_TERMINATOR_RE = re.compile(r"[.!?]")


def extract_desc_comment(text: str) -> str | None:
    """Return the contents of a leading ``<!-- desc: ... -->`` comment, if any."""
    if not text:
        return None
    first_line = text.splitlines()[0] if text else ""
    m = DESC_LINE_RE.match(first_line)
    return m.group(1).strip() if m else None


def first_sentence_fallback(text: str, max_chars: int = 120) -> str:
    """First ``.``/``?``/``!``-terminated phrase or ``max_chars`` chars,
    skipping H1 lines and the first-line desc comment.

    SPEC §3.1 'B': "first sentence (first .?!-terminated phrase or first 120
    chars, whichever is shorter, ignoring H1 lines)".
    """
    if not text:
        return ""
    candidate_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if DESC_LINE_RE.match(stripped):
            continue
        if H1_LINE_RE.match(stripped):
            continue
        candidate_lines.append(stripped)

    body = " ".join(candidate_lines)
    if not body:
        return ""

    match = SENTENCE_TERMINATOR_RE.search(body)
    if match:
        end = match.end()
        sentence = body[:end].strip()
        return sentence[:max_chars]
    return body[:max_chars]


def describe_file(text: str) -> tuple[str, bool]:
    """Return ``(description, is_auto)``.

    ``is_auto`` is True when the description came from the first-sentence
    fallback; the index renders these with an ``[auto]`` prefix so the agent
    sees its own missed desc comments and can self-correct (SPEC §3.4).
    """
    explicit = extract_desc_comment(text)
    if explicit:
        return explicit, False
    return first_sentence_fallback(text), True


@dataclass
class CoreBlock:
    path: Path
    content: str
    description: str
    is_auto_description: bool


def load_core(home: Path) -> list[CoreBlock]:
    """Load every ``memory/core/*.md`` in lexicographic (= numeric prefix) order."""
    core_dir = home / "memory" / "core"
    if not core_dir.is_dir():
        return []
    blocks: list[CoreBlock] = []
    for path in sorted(core_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        desc, is_auto = describe_file(text)
        blocks.append(
            CoreBlock(
                path=path,
                content=text,
                description=desc,
                is_auto_description=is_auto,
            )
        )
    return blocks


def check_core_blocks_health(
    blocks: list[CoreBlock],
    min_count: int = _CORE_BLOCKS_MIN_COUNT,
    min_bytes: int = _CORE_BLOCKS_MIN_BYTES,
) -> tuple[bool, list[str]]:
    """Return ``(degraded, issues)`` for a loaded set of core blocks.

    ``degraded`` is True when any check fails:
    - Fewer than ``min_count`` blocks were loaded (indicates wipe / dir loss).
    - Any individual block is shorter than ``min_bytes`` (indicates empty /
      stub file, i.e. an accidental overwrite that stripped content).

    ``issues`` is a human-readable list of the problems found; empty when
    ``degraded`` is False.

    Designed to be called inside ``_build_system_prompt()`` *after*
    ``load_core()`` so failures are visible every turn, not just at startup.
    The caller emits ``core_prompt_degraded`` (negative algedonic event) +
    logs WARNING when ``degraded`` is True.
    """
    issues: list[str] = []

    if len(blocks) < min_count:
        issues.append(
            f"only {len(blocks)} core block(s) loaded (minimum {min_count})"
        )

    for block in blocks:
        nbytes = len(block.content.encode("utf-8"))
        if nbytes < min_bytes:
            issues.append(
                f"{block.path.name} is {nbytes} bytes (minimum {min_bytes})"
            )

    return bool(issues), issues


def render_core_section(blocks: list[CoreBlock]) -> str:
    """Render core blocks for inclusion in the system prompt under ``## Core memory``.

    Each block is separated by ``---`` and rendered verbatim. The agent wrote
    them, the agent sees them as-is.
    """
    if not blocks:
        return ""
    parts: list[str] = []
    for i, block in enumerate(blocks):
        body = block.content.rstrip()
        if i > 0:
            parts.append("\n\n---\n\n")
        parts.append(body)
    return "".join(parts)
