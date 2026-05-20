"""Strip and capture ``<think>...</think>`` reasoning blocks emitted
inline by some model families (Minimax M2, DeepSeek-R1, QwQ, …).

The model produces reasoning tokens wrapped in literal ``<think>`` tags
as part of ``message.content``. Three consumers care about the
distinction:

* **turns.jsonl** — reasoning belongs in its own event so introspection
  / wiki-mining queries that filter ``type == "reasoning"`` aren't
  polluted by reply text, and ``output`` is the actual visible reply.
* **messages.jsonl + bridges** — the user-facing chat history must not
  show the model's private chain of thought (UX + leakage concern).
* **saga contextual rewrite** — the rewrite LLM should reason from
  reference antecedents in the conversation, not the model's prior
  scratchpad on a different turn.

This module owns the parse so the regex + edge-case handling lives in
one place (callers should never reach for the regex themselves).

Edge cases handled:

* Multiple closed blocks in a single response (model emits a think →
  text → think → text shape).
* Unclosed trailing ``<think>...EOF`` — happens when the model hits
  ``max_tokens`` mid-reasoning. Everything after the open tag is
  captured as reasoning; visible text is whatever preceded the tag.
* Nested tags — we do NOT support nesting (no provider emits them);
  the non-greedy match takes the first ``</think>`` it finds, which
  is the right call for `<think>foo <think> bar </think>` (the inner
  open is reasoning content).
* Whitespace introduced by tag removal collapses to a single blank
  line between paragraphs and is stripped at the edges.
"""
from __future__ import annotations

import re

# Non-greedy + DOTALL so multi-paragraph think blocks are captured as
# one. Anchored on the literal opening tag — no whitespace tolerance
# inside ``<think>`` itself because the inline reasoning convention
# producers all emit the bare tag.
_CLOSED_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

# Unclosed trailing tag — model got cut off (max_tokens or stream
# truncation). Captures everything from the last unmatched ``<think>``
# to end-of-string. Run AFTER all closed blocks have been removed so
# this regex doesn't accidentally swallow a closed-block prefix.
_OPEN_THINK_RE = re.compile(r"<think>(.*)$", re.DOTALL)


def extract_think_blocks(text: str) -> tuple[str, list[str]]:
    """Return ``(visible_text, [think_block, …])``.

    ``visible_text`` is the input with all think regions removed and
    edge whitespace trimmed. The think list preserves order of
    appearance. An unclosed trailing ``<think>`` is captured as the
    final entry. Empty / no-tag input returns ``(text, [])`` cheaply.
    """
    if not text or "<think>" not in text:
        return text, []
    blocks: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        blocks.append(match.group(1).strip())
        return ""

    cleaned = _CLOSED_THINK_RE.sub(_capture, text)
    trailing = _OPEN_THINK_RE.search(cleaned)
    if trailing is not None:
        blocks.append(trailing.group(1).strip())
        cleaned = cleaned[: trailing.start()]
    return cleaned.strip(), blocks


def strip_think_blocks(text: str) -> str:
    """Convenience wrapper when the caller doesn't need the captured
    reasoning — just wants the user-visible content."""
    visible, _ = extract_think_blocks(text)
    return visible
