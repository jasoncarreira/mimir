"""Core memory blocks (SPEC §3.1, §5.1) and description extraction.

``memory/core/*.md`` files render directly into the system prompt every turn.
Lexicographic filename ordering (``00-...``, ``10-...``, ...) becomes prompt
ordering. The first line of each file may be ``<!-- desc: ... -->``; if not,
``first_sentence_fallback`` is used (also drives ``[auto]``-prefixed entries
in the auto-generated INDEX.md files).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

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


def read_text_lossy(path: Path) -> str:
    """Read a UTF-8 text file, tolerating stray non-UTF-8 bytes.

    Prompt-assembly readers (core blocks, memory index) feed home-authored
    ``.md`` straight into the system prompt every turn. A single non-UTF-8 byte
    — e.g. a ``§`` (0xA7) from a cp1252-saved paste, or a mid-write artifact —
    used to raise ``UnicodeDecodeError``, which is a ``ValueError``, **not** an
    ``OSError``; the callers' ``except OSError`` missed it and the whole turn
    crashed during prompt assembly, before any tool ran (chainlink #470).

    Decode strict; on failure, log which file/byte and fall back to replacement
    decoding so one bad byte mangles a single character instead of dropping the
    file or killing the turn. Genuine ``OSError`` (vanished/unreadable file)
    still propagates to the caller's existing guard.
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        bad_byte = data[exc.start] if exc.start < len(data) else 0
        log.warning(
            "non-UTF-8 bytes in %s (byte 0x%02x at position %d); "
            "decoding with replacement",
            path, bad_byte, exc.start,
        )
        _report_non_utf8(path, bad_byte, exc.start)
        return data.decode("utf-8", errors="replace")


#: Paths already reported this process. ``read_text_lossy`` runs during prompt
#: assembly every turn, so a persistent bad file would otherwise emit the
#: algedonic signal every turn — dedupe to one emit per distinct file (re-armed
#: on restart; a fixed file simply stops emitting).
_NON_UTF8_REPORTED: set[str] = set()


def _report_non_utf8(path: Path, bad_byte: int, position: int) -> None:
    """Emit a one-shot ``non_utf8_home_file`` algedonic signal so the agent
    cleans the offending file (re-save as UTF-8 / drop the stray byte).

    Best-effort: the event logger raises if uninitialized (CLI ``index`` builds,
    unit tests), so signalling must never break the read it's reporting on.
    """
    key = str(path)
    if key in _NON_UTF8_REPORTED:
        return
    _NON_UTF8_REPORTED.add(key)
    try:
        from .event_logger import log_event_sync

        log_event_sync(
            "non_utf8_home_file",
            path=key,
            byte=f"0x{bad_byte:02x}",
            position=position,
        )
    except Exception:  # noqa: BLE001 — signalling is best-effort; never break the read
        pass


def load_core(home: Path) -> list[CoreBlock]:
    """Load every ``memory/core/*.md`` in lexicographic (= numeric prefix) order."""
    core_dir = home / "memory" / "core"
    if not core_dir.is_dir():
        return []
    blocks: list[CoreBlock] = []
    for path in sorted(core_dir.glob("*.md")):
        try:
            text = read_text_lossy(path)
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
#   content, and an algedonic event is emitted so over-filed real channels
#   do not stay silently stale.

_CHANNEL_MEMORY_MAX_BYTES: int = 8_000  # ~2k tokens; covers current files with headroom


# Synthetic channel prefixes whose turns should NOT receive channel-memory
# injection. These channels process automated events (scheduled ticks, poller
# notifications) where no human operator context is needed.
_SYNTHETIC_PREFIXES: tuple[str, ...] = ("scheduler:", "poller:")


#: Real channels already reported as over the injection cap this process.
#: Channel memory loads during every turn on that channel, so one oversized
#: directory would otherwise emit the same algedonic signal every turn.
#: Re-armed on restart; fixing/trimming the channel simply stops emitting.
_CHANNEL_MEMORY_OVER_CAP_REPORTED: set[str] = set()


def _report_channel_memory_over_cap(
    *,
    channel_id: str,
    channel_dir: Path,
    total_bytes: int,
    cap_bytes: int,
    file_count: int,
) -> None:
    """Emit a one-shot ``channel_memory_over_cap`` algedonic signal.

    Best-effort: prompt assembly must keep working even if the event logger is
    not initialized (CLI helpers, unit tests) or the event sink fails.
    """
    key = str(channel_dir)
    if key in _CHANNEL_MEMORY_OVER_CAP_REPORTED:
        return
    _CHANNEL_MEMORY_OVER_CAP_REPORTED.add(key)
    try:
        from .event_logger import log_event_sync

        log_event_sync(
            "channel_memory_over_cap",
            channel_id=channel_id,
            path=key,
            bytes=total_bytes,
            cap_bytes=cap_bytes,
            file_count=file_count,
        )
    except Exception:  # noqa: BLE001 — signalling is best-effort
        pass


def load_channel_memory(home: Path, channel_id: str) -> str | None:
    """Load and concatenate ``memory/channels/<channel_id>/*.md`` files.

    Returns a rendered block string ready for ``## Channel context`` injection,
    or ``None`` when no files exist or the channel is synthetic.

    Files are sorted lexicographically so ordering is deterministic and the
    operator can control injection order via filename prefixes (``00-``, etc.).

    Capped at ``_CHANNEL_MEMORY_MAX_BYTES``; truncated with a note when the
    cap fires so the agent sees the truncation rather than losing content
    silently. For real (non-synthetic) channels, the cap also emits a
    ``channel_memory_over_cap`` algedonic signal once per process so the
    operator/agent sees that channel context has gone stale.
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
            text = read_text_lossy(path).rstrip()
        except OSError:
            continue
        if text:
            parts.append(text)

    if not parts:
        return None

    combined = "\n\n---\n\n".join(parts)
    encoded = combined.encode("utf-8")
    if len(encoded) > _CHANNEL_MEMORY_MAX_BYTES:
        _report_channel_memory_over_cap(
            channel_id=channel_id,
            channel_dir=channel_dir,
            total_bytes=len(encoded),
            cap_bytes=_CHANNEL_MEMORY_MAX_BYTES,
            file_count=len(parts),
        )
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
