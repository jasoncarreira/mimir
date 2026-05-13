"""LLM-backed synthesis for observations and session boundaries.

Two callable factories — one for ``consolidate()``'s
``observation_synth_fn``, one for ``reflect()``'s ``boundary_synth_fn``.
Both wrap saga's existing ``call_llm`` transport so we inherit
provider selection (anthropic / openai_compat / claude_code), rate-
limit accounting, and async-pool plumbing without reimplementing
any of it. Once the final cutover (mimir/memory → mimir/saga) lands,
this becomes the canonical LLM-synth surface and the saga.consolidation
prompt code path can be deleted.

The prompts here are pared-down versions of saga.consolidation's. The
production prompt has triple extraction, contradiction detection, and
canonical-subject vocab blocks — all P-features that mimir.memory
defers to a Tier 3 follow-up (triples.py, contradictions.py). Keeping
the v2 prompt small means: less LLM cost per cluster, simpler to
parse, fewer ways to fail. We can grow it back once the bench numbers
say we need it.

Output shapes:

- ``observation_synth_fn(cluster) -> (content, topics)``
  ``cluster`` is the list of atom dicts as built by consolidate.py's
  ``_candidate_raws``. ``content`` is the observation text. ``topics``
  is a small list of free-text labels extracted from the cluster's
  topics column (best-effort; may be empty).

- ``boundary_synth_fn(atoms, ctx) -> dict``
  Returns the structured boundary fields reflect.py persists:
  ``summary``, ``topics_discussed``, ``decisions_made``, ``unfinished``,
  ``emotional_state``. The agent's existing synthesis-turn already
  produces these — the LLM-synth fallback exists for cases where
  reflect() is called without pre-rendered fields (cron-driven cross-
  session pass, importer regenerating missing boundaries).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable


logger = logging.getLogger("mimir.memory.synthesize")


# ─── Observation synthesis (consolidate) ─────────────────────────────


OBSERVATION_PROMPT = """\
You are consolidating {n} related memory atoms into a single observation.

Produce a single observation that captures what the atoms collectively convey.

Output format (exactly this header, then the observation):

OBSERVATION:
<one or two sentences>

Rules for the OBSERVATION:
- Preserve specific dates, times, numbers, names, and direct quotes VERBATIM \
when they appear in the atoms.
- If atoms disagree on a fact, keep both versions ("user first mentioned X on \
date A, then updated to Y on date B").
- If an atom is dated "[YYYY-MM-DD role] ...", include the date in the \
observation when the date matters to the content.
- Do not invent details not present in the atoms.

Atoms:
- {atoms}
"""


_OBSERVATION_HEADER = re.compile(r"^OBSERVATION:\s*", re.IGNORECASE | re.MULTILINE)


def _parse_observation(raw: str) -> str:
    """Pull the observation prose out of the LLM response. Strips the
    OBSERVATION: header and any trailing whitespace; returns the first
    non-empty paragraph below the header (LLM sometimes appends notes
    after a blank line, which we drop)."""
    if not raw:
        return ""
    # Drop the header marker if present.
    m = _OBSERVATION_HEADER.search(raw)
    if m:
        body = raw[m.end():]
    else:
        body = raw
    # First paragraph only — split on double-newline, take the head.
    para = body.strip().split("\n\n", 1)[0].strip()
    return para


def make_observation_synth_fn(
    *,
    llm_config: dict | None = None,
    max_tokens: int = 600,
    temperature: float = 0.3,
) -> Callable[[list[dict]], tuple[str, list[str]]]:
    """Return a sync callable suitable for ``consolidate.observation_synth_fn``.

    The callable internally drives saga's async ``call_llm`` via
    ``asyncio.run`` (consolidate() is itself called from a sync
    transaction loop). If the caller is already inside an event loop —
    likely in the live agent path — they should use
    ``make_async_observation_synth_fn`` instead.

    ``llm_config`` defaults to saga's ``[consolidation]`` section
    fallback chain. Pass a dict to override per-call.
    """
    async_fn = make_async_observation_synth_fn(
        llm_config=llm_config,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    def _sync(cluster: list[dict]) -> tuple[str, list[str]]:
        return asyncio.run(async_fn(cluster))

    return _sync


def make_async_observation_synth_fn(
    *,
    llm_config: dict | None = None,
    max_tokens: int = 600,
    temperature: float = 0.3,
) -> Callable[[list[dict]], Any]:
    """Async variant. Returns a coroutine-producing callable so it can
    be awaited from inside an existing event loop (the bench harness,
    mimir's dispatcher worker)."""
    async def _do(cluster: list[dict]) -> tuple[str, list[str]]:
        from saga._llm import call_llm
        from saga.config import resolve_llm_config

        cfg = llm_config or resolve_llm_config("consolidation")
        atoms_block = "\n- ".join(a["content"] for a in cluster)
        prompt = OBSERVATION_PROMPT.format(
            n=len(cluster), atoms=atoms_block,
        )
        try:
            raw = await call_llm(
                cfg,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                system=None,
            )
        except Exception as exc:
            logger.warning(
                "observation_synth_fn LLM call failed (cluster size=%d): %s",
                len(cluster), exc,
            )
            return ("", [])

        content = _parse_observation(raw)
        topics = _collect_topics(cluster)
        return (content, topics)

    return _do


def _collect_topics(cluster: list[dict]) -> list[str]:
    """Union the topics across cluster atoms, drop duplicates, cap at 5.
    Simple aggregation — we don't ask the LLM to infer fresh topics;
    the source atoms already carry them from annotate."""
    seen: list[str] = []
    for a in cluster:
        raw = a.get("topics")
        topics: list[str]
        if isinstance(raw, str):
            try:
                topics = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                topics = []
        elif isinstance(raw, list):
            topics = raw
        else:
            topics = []
        for t in topics:
            if isinstance(t, str) and t and t not in seen:
                seen.append(t)
                if len(seen) >= 5:
                    return seen
    return seen


# ─── Boundary synthesis (reflect, used when caller didn't pre-render) ─


BOUNDARY_PROMPT = """\
You are synthesizing a session boundary — a structured summary of one \
conversation session.

Output a JSON object with these fields (no extra text, no markdown fences):
{{
  "summary": "<one-paragraph summary of what happened this session>",
  "topics_discussed": ["topic1", "topic2", ...],
  "decisions_made": ["decision1", "decision2", ...],
  "unfinished": ["open item 1", "open item 2", ...],
  "emotional_state": "<one phrase or null>"
}}

Rules:
- The summary should be readable as a continuity hook for a future session.
- Topics, decisions, unfinished items: short phrases, max 10 each.
- Emotional state is optional; use null if nothing notable.

Atoms from this session:
- {atoms}
"""


def make_boundary_synth_fn(
    *,
    llm_config: dict | None = None,
    max_tokens: int = 800,
    temperature: float = 0.2,
) -> Callable[[list[dict], dict | None], dict]:
    """Sync boundary synthesizer. Mirrors observation_synth_fn shape."""
    async_fn = make_async_boundary_synth_fn(
        llm_config=llm_config,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    def _sync(atoms: list[dict], ctx: dict | None = None) -> dict:
        return asyncio.run(async_fn(atoms, ctx))

    return _sync


def make_async_boundary_synth_fn(
    *,
    llm_config: dict | None = None,
    max_tokens: int = 800,
    temperature: float = 0.2,
) -> Callable[[list[dict], dict | None], Any]:
    async def _do(atoms: list[dict], ctx: dict | None = None) -> dict:
        from saga._llm import call_llm
        from saga.config import resolve_llm_config

        cfg = llm_config or resolve_llm_config("reflection")
        if not atoms:
            return _empty_boundary()
        atoms_block = "\n- ".join(a.get("content", "") for a in atoms)
        prompt = BOUNDARY_PROMPT.format(atoms=atoms_block)
        try:
            raw = await call_llm(
                cfg,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                system=None,
            )
        except Exception as exc:
            logger.warning("boundary_synth_fn LLM call failed: %s", exc)
            return _empty_boundary()

        return _parse_boundary(raw)

    return _do


def _empty_boundary() -> dict:
    return {
        "summary": "",
        "topics_discussed": [],
        "decisions_made": [],
        "unfinished": [],
        "emotional_state": None,
    }


def _parse_boundary(raw: str) -> dict:
    """Best-effort JSON parse with fallback to empty fields. LLMs
    sometimes wrap JSON in markdown fences; strip those first."""
    if not raw:
        return _empty_boundary()
    text = raw.strip()
    # Strip ``` fences if present.
    if text.startswith("```"):
        # Drop the first line (``` or ```json) and the trailing fence.
        lines = text.split("\n")
        if len(lines) >= 2:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _empty_boundary()
    if not isinstance(data, dict):
        return _empty_boundary()
    return {
        "summary": _coerce_str(data.get("summary")),
        "topics_discussed": _coerce_list(data.get("topics_discussed")),
        "decisions_made": _coerce_list(data.get("decisions_made")),
        "unfinished": _coerce_list(data.get("unfinished")),
        "emotional_state": _coerce_optional_str(data.get("emotional_state")),
    }


def _coerce_str(v) -> str:
    return v if isinstance(v, str) else ""


def _coerce_optional_str(v) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) and v else None


def _coerce_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str) and x]
