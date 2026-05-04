"""XML directive parser for chat-bridge agent responses.

The agent emits its response as natural prose with embedded ``<actions>``
blocks instead of round-tripping through tool calls. The bridge extracts
the directives, sends the cleaned text, and dispatches each directive in
order:

    Got it. Here's the chart.

    <actions>
      <react emoji="thumbsup" />
      <send-file path="quarterly-report.pdf" caption="Q3 numbers" />
    </actions>

→ ``send`` "Got it. Here's the chart."
→ ``react`` 👍 on the latest message in the current channel
→ ``send-file`` quarterly-report.pdf with caption

This avoids one tool-call round-trip per side effect (vs. the
``send_message + react + send_file`` tool-based pattern). Lifted from
lettabot (~/projects/letta/lettabot/src/core/directives.ts) — Python
port with the same vocabulary.

**Trust boundary:** parse only the agent's own assistant text. Don't
run the parser on quoted user input, system reminders, or recent-message
blocks — those could legitimately contain literal ``<actions>`` strings
the agent is reasoning about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


# ─── Directive types ────────────────────────────────────────────────


@dataclass(frozen=True)
class ReactDirective:
    """React with an emoji on the channel the parent ``send_message``
    targets. ``message_id`` defaults to the message just sent in the
    same call (or, for directives-only sends, the most recent
    assistant message in the channel)."""
    type: Literal["react"] = field(default="react", init=False)
    emoji: str = ""
    message_id: str | None = None


@dataclass(frozen=True)
class SendFileDirective:
    """Attach a file to the parent ``send_message`` channel. ``path``
    resolves under ``MIMIR_HOME/attachments/outbound/`` (escapes via
    ``..`` or symlink are rejected)."""
    type: Literal["send-file"] = field(default="send-file", init=False)
    path: str = ""
    caption: str | None = None
    kind: Literal["image", "file", "audio"] | None = None
    cleanup: bool = False  # If True, delete the file after successful send.


# Cross-channel sends use a separate ``send_message`` tool call with an
# explicit ``channel_id`` argument, not an in-actions directive. There's
# no ``<send-message>`` directive — keeping cross-channel routing on the
# tool surface keeps the behavior obvious and per-call rate-limited.

Directive = ReactDirective | SendFileDirective


@dataclass(frozen=True)
class ParseResult:
    """Output of ``parse_directives``.

    ``clean_text`` is the agent's prose with all ``<actions>`` blocks
    stripped — what gets sent as the message body. ``directives`` are
    the parsed actions in source order.
    """
    clean_text: str
    directives: tuple[Directive, ...]


# ─── Regex tokens ───────────────────────────────────────────────────


_ACTIONS_BLOCK_RE = re.compile(
    r"<actions\b[^>]*>([\s\S]*?)</actions>", re.IGNORECASE,
)

# Matches a self-closing <react ... /> or <send-file ... />. The
# whitespace around `/>` is tolerated to be lenient on LLM output.
_DIRECTIVE_TOKEN_RE = re.compile(
    r"""<(react|send-file)\b([^>]*?)/\s*>""",
    re.VERBOSE | re.IGNORECASE,
)

# attr="value" or attr='value'. Attribute names: ascii alnum + dash.
_ATTR_RE = re.compile(
    r"""([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)


def _parse_attrs(attr_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_str or ""):
        name, dq, sq = m.group(1), m.group(2), m.group(3)
        out[name.lower()] = dq if dq is not None else (sq or "")
    return out


def _parse_block_children(block: str) -> list[Directive]:
    out: list[Directive] = []
    for m in _DIRECTIVE_TOKEN_RE.finditer(block):
        tag = (m.group(1) or "").lower()
        attrs = _parse_attrs(m.group(2) or "")
        if tag == "react":
            emoji = attrs.get("emoji", "").strip()
            if not emoji:
                continue
            out.append(ReactDirective(
                emoji=emoji,
                message_id=attrs.get("message") or attrs.get("message_id"),
            ))
        elif tag == "send-file":
            path = (attrs.get("path") or attrs.get("file") or "").strip()
            if not path:
                continue
            caption = attrs.get("caption") or attrs.get("text")
            kind_raw = (attrs.get("kind") or "").lower().strip()
            kind: Literal["image", "file", "audio"] | None = (
                kind_raw  # type: ignore[assignment]
                if kind_raw in ("image", "file", "audio") else None
            )
            cleanup = (attrs.get("cleanup") or "").lower() in ("true", "1", "yes")
            out.append(SendFileDirective(
                path=path, caption=caption, kind=kind, cleanup=cleanup,
            ))
    return out


# ─── Public API ─────────────────────────────────────────────────────


def parse_directives(text: str) -> ParseResult:
    """Extract every ``<actions>...</actions>`` block from ``text``,
    parse the directives inside each, and return a cleaned text plus
    the directives in source order.

    Strips the entire block (including the wrapping ``<actions>`` tags)
    from the cleaned text. Free-floating ``<react>`` / ``<send-file>``
    tags outside ``<actions>`` are NOT parsed — keeping the wrapper
    requirement makes parsing deterministic and gives the agent a clear
    visual marker that it's emitting actions vs. just describing them.
    """
    if "<actions" not in text.lower():
        return ParseResult(clean_text=text, directives=())

    directives: list[Directive] = []

    def _replace(m: re.Match[str]) -> str:
        directives.extend(_parse_block_children(m.group(1)))
        return ""

    cleaned = _ACTIONS_BLOCK_RE.sub(_replace, text)
    # Collapse trailing whitespace/blank lines left behind by stripped
    # blocks — agent-emitted blocks often sit on their own line(s).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return ParseResult(clean_text=cleaned, directives=tuple(directives))


def has_unclosed_actions_block(text: str) -> bool:
    """Streaming helper. True when the most recent ``<actions>`` opening
    tag has no matching ``</actions>`` close yet — caller can hide the
    in-progress block from the user-visible stream."""
    lower = text.lower()
    last_open = lower.rfind("<actions")
    if last_open < 0:
        return False
    last_close = lower.rfind("</actions>")
    return last_open > last_close


def has_incomplete_actions_tag(text: str) -> bool:
    """Streaming helper. True when the tail of ``text`` is the prefix
    of ``<actions>`` or ``</actions>`` — partial token, hold the next
    chunk before flushing to the user-visible stream."""
    last_lt = text.rfind("<")
    if last_lt < 0:
        return False
    last_gt = text.rfind(">")
    if last_lt <= last_gt:
        return False
    tail = text[last_lt:].lower()
    return "<actions>".startswith(tail) or "</actions>".startswith(tail)


def strip_actions_blocks(text: str) -> str:
    """Cleaned-text-only variant of ``parse_directives`` — returns the
    text with every complete ``<actions>`` block removed. Useful in
    streaming / display paths that don't care about the directives
    themselves (e.g., logging, transcript mirroring)."""
    cleaned = _ACTIONS_BLOCK_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


__all__ = [
    "ReactDirective",
    "SendFileDirective",
    "Directive",
    "ParseResult",
    "parse_directives",
    "has_unclosed_actions_block",
    "has_incomplete_actions_tag",
    "strip_actions_blocks",
]
