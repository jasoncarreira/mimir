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


# ── Channel memory injection ────────────────────────────────────────────────
# ``memory/channels/<channel_id>/`` contains per-channel fact files (operator
# name, preferences, channel-specific patterns). When a ``user_message``
# arrives on channel X, these files are injected into the turn prompt so the
# agent has channel context without an explicit tool call each turn.
#
# Design constraints (chainlink #187):
# - Only fires for real channels. Synthetic channels (``scheduler:*``,
#   ``poller:*``) share no human operator context worth injecting.
# - No prompt inflation on channels with no memory files — graceful no-op.
# - Per-channel blast radius is bounded: a write to
#   ``memory/channels/<id>/`` only affects turns on *that* channel, not
#   every turn globally (unlike ``memory/core/``). This is the justification
#   for keeping channel file writes **autonomous** in 06-action-boundaries.md
#   even after auto-injection ships.
# - Cap at ``_CHANNEL_MEMORY_MAX_BYTES`` to defend against runaway file
#   growth. When the cap fires the returned block is truncated with a note
#   so the agent can see the truncation rather than silently losing tail
#   content.

_CHANNEL_MEMORY_MAX_BYTES: int = 8_000  # ~2k tokens; covers current files with headroom


# Synthetic channel prefixes whose turns should NOT receive channel-memory
# injection. These channels process automated events (scheduled ticks, poller
# notifications) where no human operator context is needed.
_SYNTHETIC_PREFIXES: tuple[str, ...] = ("scheduler:", "poller:")


def load_channel_memory(home: Path, channel_id: str) -> str | None:
    """Load and concatenate ``memory/channels/<channel_id>/*.md`` files.

    Returns a rendered block string ready for ``## Channel context`` injection,
    or ``None`` when no files exist or the channel is synthetic.

    Files are sorted lexicographically so ordering is deterministic and the
    operator can control injection order via filename prefixes (``00-``, etc.).

    Capped at ``_CHANNEL_MEMORY_MAX_BYTES``; truncated with a note when the
    cap fires so the agent sees the truncation rather than losing content
    silently.
    """
    if not channel_id:
        return None
    # Skip synthetic channels — no human operator context to inject.
    if any(channel_id.startswith(p) for p in _SYNTHETIC_PREFIXES):
        return None

    channel_dir = home / "memory" / "channels" / channel_id
    if not channel_dir.is_dir():
        return None

    parts: list[str] = []
    for path in sorted(channel_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        if text:
            parts.append(text)

    if not parts:
        return None

    combined = "\n\n---\n\n".join(parts)
    encoded = combined.encode("utf-8")
    if len(encoded) > _CHANNEL_MEMORY_MAX_BYTES:
        # Truncate at byte boundary, add visible note.
        truncated = encoded[:_CHANNEL_MEMORY_MAX_BYTES].decode("utf-8", errors="replace")
        combined = truncated + f"\n\n…[channel memory truncated at {_CHANNEL_MEMORY_MAX_BYTES} bytes]"

    return combined


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
