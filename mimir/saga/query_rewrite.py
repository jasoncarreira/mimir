"""LLM-driven contextual query rewriting.

In multi-turn conversations the user's latest message often contains
referential terms ("what about him?", "the other one") that retrieve
nothing on their own — the antecedent lives in prior turns. Saga's
``contextual_rewrite`` (P-feature, off in the LongMemEval bench
because LongMemEval is single-turn) calls an LLM to rewrite the query
into a self-contained form before retrieval.

This module ports the same pattern to mimir.saga. Opt-in by call
site: ``SagaStore.query(context=[...])`` plumbs the conversation
context through; only when a non-empty context is provided AND a
flag is set do we actually call the LLM and use the rewritten form.

The rewrite is best-effort. On LLM failure (transport down, timeout,
empty response, malformed output) the original query is returned
unchanged — the recall path keeps working, just without rewrite.

Bench behavior: LongMemEval has no prior context per question, so
even if the flag is on the rewrite is a no-op. Saga's bench TOML
sets ``enable_contextual_rewrite = false`` explicitly for clarity;
we honor the same default.
"""
from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger("mimir.saga.query_rewrite")


# Bounds matched to saga's _resolve_contextual_query — keep prompt
# token cost roughly stable regardless of how long the conversation
# has gotten. Last-10-msgs window catches the antecedent window for
# nearly every reference resolution case; 400-char per-message cap
# trims long assistant answers (the usual source of bloat).
_MAX_CONTEXT_MESSAGES = 10
_MAX_CONTEXT_CONTENT_CHARS = 400


REWRITE_PROMPT = """\
You rewrite the user's current message into a SELF-CONTAINED \
statement-form retrieval anchor for a memory-retrieval system whose \
atoms are stored as declarative statements (e.g. "user prefers X", \
"<entity> on <topic>", "<concept> = <definition>"). The agent uses \
your output to retrieve atoms by semantic + keyword match; it is \
never shown to the user, so it does NOT need to read like natural \
language.

Rules:
- Resolve all references against the transcript:
    "yes, look for that" + transcript about Sony headphones → \
"Sony headphones"
    "tell me more" + transcript about Italy → "Italy travel details"
    "what about the other one?" + transcript about two laptops → \
"the other laptop"
- Convert questions and commands into noun-phrase or statement form:
    "what's my favorite pizza topping?" → "favorite pizza topping"
    "when did the meeting end?" → "meeting end time"
    "did Sony release a new model?" → "Sony new model release"
    "save the meeting notes" → "meeting notes"
- Preserve proper nouns, dates, numbers, and direct quotes verbatim.
- Do NOT add information not present in the message or transcript.
- Output ONLY the anchor on a single line. No question marks unless \
the original contained a quoted phrase. No preamble, no quotes around \
the output.
- If the message already reads as a self-contained noun phrase or \
statement, return it unchanged.

Conversation transcript (most recent last):
{context}

Current message: {question}

Retrieval anchor:"""


def _format_context(context: list[dict[str, str]]) -> str:
    """Render the conversation context for the rewrite prompt. Each
    entry is ``{"role": "user"|"assistant", "content": "..."}``.

    Caps the window at the last ``_MAX_CONTEXT_MESSAGES`` turns and
    truncates each turn's content to ``_MAX_CONTEXT_CONTENT_CHARS`` —
    keeps the prompt's token cost bounded as conversations grow, and
    pins prompt structure to saga's contextual rewrite (the reference
    implementation).
    """
    recent = context[-_MAX_CONTEXT_MESSAGES:]
    lines: list[str] = []
    for turn in recent:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _MAX_CONTEXT_CONTENT_CHARS:
            content = content[:_MAX_CONTEXT_CONTENT_CHARS] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _clean_rewrite(raw: str, original: str) -> str:
    """Strip the LLM's preface noise — trailing whitespace, leading
    "Rewritten:" or "Rewritten question:" echoes, surrounding quotes.
    Returns the original query if the parsed rewrite is empty or
    starts with one of the well-known refusal phrases."""
    text = (raw or "").strip()
    if not text:
        return original
    # Drop a label-line echo from the prompt: legacy "Rewritten:" /
    # "Rewritten question:" or current "Retrieval anchor:".
    text = re.sub(
        r"^(?:[Rr]ewritten(?:\s*[Qq]uestion)?|[Rr]etrieval\s+anchor)\s*:\s*",
        "",
        text,
    )
    # Take only the first line — paragraph drift confuses retrieval.
    text = text.splitlines()[0].strip()
    text = text.strip("\"'`")
    # Refusal heuristics — if the LLM declined or said "no change
    # needed", fall back to the original. Match on LLM-style refusal
    # phrasing only ("i cannot", "i can't", "cannot rewrite") — a
    # naked "no " prefix would misfire on legitimate queries like
    # "No, I meant Tuesday" or "no Spotify recommendations?".
    lowered = text.lower()
    refusal_prefixes = (
        "i cannot", "i can't", "i can not",
        "cannot rewrite", "can't rewrite",
        "no change", "no rewrite",
        "unable to",
    )
    if not text or lowered.startswith(refusal_prefixes):
        return original
    return text


async def rewrite_query(
    query: str,
    context: list[dict[str, str]] | None,
    *,
    llm_config: dict | None = None,
    max_tokens: int = 200,
    temperature: float = 0.1,
) -> str:
    """Async-rewrite ``query`` using prior ``context``.

    Returns the rewritten query on success, or ``query`` unchanged
    when:
    - ``context`` is empty / None (no antecedents to resolve)
    - the LLM call fails (transport / timeout / empty)
    - the LLM produces a refusal or empty output

    Provider plumbing is saga's ``call_llm`` — same selection chain
    as consolidation. ``llm_config`` overrides per call.
    """
    if not context:
        return query
    rendered = _format_context(context)
    if not rendered:
        return query

    from ._llm import call_llm
    from ._config_io import resolve_llm_config

    cfg = llm_config or resolve_llm_config("retrieval_v2")
    prompt = REWRITE_PROMPT.format(context=rendered, question=query)
    try:
        raw = await call_llm(
            cfg,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system=None,
        )
    except Exception as exc:
        logger.warning("contextual rewrite LLM call failed: %s", exc)
        return query
    return _clean_rewrite(raw, query)


__all__ = ["rewrite_query"]
