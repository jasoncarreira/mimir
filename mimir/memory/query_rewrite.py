"""LLM-driven contextual query rewriting.

In multi-turn conversations the user's latest message often contains
referential terms ("what about him?", "the other one") that retrieve
nothing on their own — the antecedent lives in prior turns. Saga's
``contextual_rewrite`` (P-feature, off in the LongMemEval bench
because LongMemEval is single-turn) calls an LLM to rewrite the query
into a self-contained form before retrieval.

This module ports the same pattern to mimir.memory. Opt-in by call
site: ``MemoryClient.query(context=[...])`` plumbs the conversation
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


logger = logging.getLogger("mimir.memory.query_rewrite")


REWRITE_PROMPT = """\
You are rewriting a user's question so it stands alone — a future \
component will use it to search a memory store that has no access to \
the conversation history.

Rules:
- Resolve pronouns ("he", "she", "they", "it") to the named entities \
they refer to in the context.
- Resolve referential phrases ("the other one", "that thing we \
discussed", "earlier") to their concrete referents.
- Preserve all proper nouns, dates, numbers, and direct quotes \
verbatim.
- If the question already stands alone, return it unchanged.
- Do NOT add explanation or commentary. Output ONLY the rewritten \
question on a single line.
- If the context is empty or doesn't disambiguate the question, \
return the original question unchanged.

Conversation context (oldest first):
{context}

User's question to rewrite:
{question}

Rewritten question:"""


def _format_context(context: list[dict[str, str]]) -> str:
    """Render the conversation context for the rewrite prompt. Each
    entry is ``{"role": "user"|"assistant", "content": "..."}``."""
    lines: list[str] = []
    for turn in context:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _clean_rewrite(raw: str, original: str) -> str:
    """Strip the LLM's preface noise — trailing whitespace, leading
    "Rewritten question:" echoes, surrounding quotes. Returns the
    original query if the parsed rewrite is empty or starts with one
    of the well-known refusal phrases."""
    text = (raw or "").strip()
    if not text:
        return original
    # Drop a "Rewritten question:" prefix if the LLM echoed it.
    text = re.sub(r"^[Rr]ewritten\s*[Qq]uestion\s*:\s*", "", text)
    # Take only the first line — paragraph drift confuses retrieval.
    text = text.splitlines()[0].strip()
    text = text.strip("\"'`")
    # Refusal heuristics — if the LLM declined or said "no change
    # needed", fall back to the original.
    lowered = text.lower()
    if not text or lowered.startswith(("no ", "cannot", "i can't", "i cannot")):
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

    from saga._llm import call_llm
    from saga.config import resolve_llm_config

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
