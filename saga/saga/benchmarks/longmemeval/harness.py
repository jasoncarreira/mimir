"""Reader stage: given retrieved atoms + the question, produce a hypothesis."""
from __future__ import annotations

import os
import re
import time
from typing import Any

from .config import READER_BASE_URL, READER_MODEL, READER_API_KEY_ENV, READER_MAX_TOKENS, READER_TIMEOUT_S

_CONSOLIDATION_PREFIX = re.compile(r"^\[Consolidated from \d+ atoms?\]\s*")

_SYSTEM = """You answer questions about a user based on excerpts from their own chat history with an AI assistant.

Each excerpt is tagged `[YYYY-MM-DD role]` where role is "user" (something the user said) or "assistant" (something the AI said back). Excerpts are listed in chronological order, oldest first. Today's date will be given.

Rules:
1. Use ONLY information that is stated in the excerpts. Do not invent facts.
2. If the excerpts do not contain the information needed to answer, say "I don't know" or "The provided chat history does not contain this information." Do not guess.
3. When the user's information has changed over time (e.g. they moved, changed jobs, updated a preference), the MOST RECENT excerpt before today's date is authoritative. Older, contradicted information is stale.
4. For questions about time elapsed ("how many days ago", "last spring", "when"), compute from today's date and the excerpt date. Show the arithmetic briefly.
5. For questions about what the user said vs what the assistant said, honor the role tag — only quote the role that the question refers to.
6. Give the final answer on the last line, after any reasoning. Keep it short — a phrase or single sentence is usually enough."""


def _parse_date_from_content(content: str) -> str:
    if content.startswith("[") and len(content) >= 11:
        return content[1:11]
    return "9999-99-99"


def _format_atoms(atoms: list[dict]) -> str:
    ordered = sorted(atoms, key=lambda a: _parse_date_from_content(a.get("content", "")))
    lines = []
    for i, a in enumerate(ordered, 1):
        content = (a.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{i}] {content}")
    return "\n".join(lines)


def _format_observations(obs: list[dict]) -> str:
    lines = []
    for i, o in enumerate(obs, 1):
        content = (o.get("content") or "").strip()
        content = _CONSOLIDATION_PREFIX.sub("", content)
        if not content:
            continue
        ec = o.get("evidence_count") or 0
        lines.append(f"[O{i} evidence={ec}] {content}")
    return "\n".join(lines)


def build_prompt(question: str, question_date: str, retrieved) -> list[dict]:
    """
    Build the reader prompt. Accepts either a list of atoms (single-tier,
    existing callers) or a dict with ``observations`` + ``raws`` keys
    (two-tier, P9). In the two-tier path both blocks are labeled and the
    reader is told to prefer Evidence for specifics.
    """
    if isinstance(retrieved, dict):
        obs_block = _format_observations(retrieved.get("observations") or [])
        raw_block = _format_atoms(retrieved.get("raws") or [])
        sections = [f"Today's date: {question_date}"]
        if obs_block:
            sections.append(
                "Observations (distilled beliefs synthesized from multiple turns):\n"
                + obs_block
            )
        sections.append(
            "Evidence (raw chat turns, chronological):\n"
            + (raw_block or "(no relevant memories retrieved)")
        )
        sections.append(f"Question: {question}")
        sections.append(
            "Observations summarize patterns and preferences across many turns — "
            "treat them as secondary. Evidence is verbatim user/assistant text — "
            "prefer it for specific dates, names, numbers, and direct quotes. "
            "When Observation and Evidence conflict, Evidence wins.\n\n"
            "Think step by step: which items contain the answer? "
            "If multiple conflict, which is most recent? "
            "If no item answers, say so. Then give the final answer on its own line."
        )
        user = "\n\n".join(sections)
    else:
        context_block = _format_atoms(retrieved) or "(no relevant memories retrieved)"
        user = (
            f"Today's date: {question_date}\n\n"
            f"Relevant excerpts from the user's chat history (chronological):\n{context_block}\n\n"
            f"Question: {question}\n\n"
            "Think step by step: which excerpts (if any) contain the answer, and what do they say? "
            "If multiple excerpts conflict, which is most recent? "
            "If no excerpt answers the question, say so. "
            "Then give the final answer on its own line."
        )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def call_reader(messages: list[dict], *, max_tokens: int = READER_MAX_TOKENS) -> dict[str, Any]:
    """OpenAI-compatible call to the reader model configured in config.py."""
    from openai import OpenAI, APIError, RateLimitError, APIConnectionError, APITimeoutError

    api_key = os.environ.get(READER_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{READER_API_KEY_ENV} not set")

    client = OpenAI(api_key=api_key, base_url=READER_BASE_URL, timeout=READER_TIMEOUT_S)

    last_err: Exception | None = None
    for attempt in range(5):
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model=READER_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.0,
            )
            latency_ms = (time.time() - t0) * 1000
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            return {
                "text": text,
                "latency_ms": latency_ms,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "model": READER_MODEL,
            }
        except (RateLimitError, APIError, APIConnectionError, APITimeoutError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Reader call failed after retries: {last_err}")


# Back-compat alias in case anything still imports the old name.
call_minimax = call_reader


def read(question: str, question_date: str, retrieved) -> dict[str, Any]:
    """Accepts a list of atoms (single-tier) or a dict (two-tier)."""
    messages = build_prompt(question, question_date, retrieved)
    result = call_reader(messages)
    return {
        "hypothesis": result["text"],
        "reader_latency_ms": result["latency_ms"],
        "reader_prompt_tokens": result["prompt_tokens"],
        "reader_completion_tokens": result["completion_tokens"],
        "reader_model": result["model"],
    }
