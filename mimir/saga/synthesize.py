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
canonical-subject vocab blocks — all P-features that mimir.saga
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


logger = logging.getLogger("mimir.saga.synthesize")


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


# ─── Rich prompt: OBSERVATION + TRIPLES + CONTRADICTIONS (P42 + P4) ──
#
# Produced in one LLM call per cluster so we pay one round-trip for
# all three signals. The parser handles each section independently —
# any can be NONE without affecting the others.
RICH_PROMPT = """\
You are consolidating {n} related memory atoms. Produce THREE outputs in a \
single response.

Output format (exactly these section headers, in this order):

OBSERVATION:
<one or two sentences capturing what the atoms collectively convey>

TRIPLES:
(subject, predicate, object)
(subject, predicate, object, valid_from=YYYY-MM-DD)
(subject, predicate, object, valid_from=YYYY-MM-DD, valid_until=YYYY-MM-DD)
...
[OR write: NONE if no clean triples]

CONTRADICTIONS:
<atom_index_a> vs <atom_index_b>: <one-sentence summary of what they disagree on>
...
[OR write: NONE if no contradictions]

Rules for the OBSERVATION:
- Preserve specific dates, times, numbers, names, and direct quotes VERBATIM \
when they appear in the atoms.
- If atoms disagree on a fact, keep both versions ("user first mentioned X on \
date A, then updated to Y on date B").
- If an atom is dated "[YYYY-MM-DD role] ...", include the date in the \
observation when the date matters to the content.
- Do not invent details not present in the atoms.

Rules for TRIPLES:
- Subject must be a NAMED ENTITY (person, system, tool, place), max 30 chars
- Object must be a SHORT SPECIFIC VALUE, max 30 chars
- Predicate must be lowercase_snake_case
- PREFER reusing canonical intent predicates over inventing domain-specific \
compounds. Detail goes in the OBJECT, not the predicate. Instead of \
(User, prefers_podcast_length, 20-30_minutes), emit \
(User, prefers, podcast_length=20-30_minutes). You MAY introduce a new \
predicate when no canonical fits — typically for domain relations between \
two non-User entities, e.g. (CompanyX, manufactures, ProductY).
- Implicit subject "User" for user-preference statements
- Lists become multiple triples (one per item)
- Skip emotional/philosophical/meta-commentary content (write NONE)

{vocab_block}Rules for TEMPORAL TAGS (optional valid_from/valid_until):
- Use ONLY when the atoms show a fact CHANGED over time. Take the YYYY-MM-DD \
from the dated atom prefix(es).
- valid_from only: fact starts on a date and is still current (most \
user-preference statements). Example: user moves to a new city — emit \
(User, lives_in, NewCity, valid_from=YYYY-MM-DD).
- Both bounds: closed interval. Example: user held a job from A to B — emit \
(User, employed_at, OldJob, valid_from=A, valid_until=B).
- DO NOT add bounds to facts that don't change (genres, languages, ratings, \
etc.). DO NOT use the consolidation date — use the source atom's own date.

Rules for CONTRADICTIONS:
- Only flag *direct* disagreements where two atoms make incompatible claims \
about the same fact (different objects for the same subject+predicate; \
opposing preferences on the same topic; incompatible dates).
- Use 1-based atom indices from the list below.
- Don't flag temporal evolution ("used to like X, now likes Y") — that's a \
TRIPLES temporal-tag case, not a contradiction.
- Don't flag stylistic / phrasing differences. Substance only.

{prior_block}Atoms:
{indexed_atoms}
"""


# Seed values for the vocab block — applied even on a cold DB so the
# LLM sees a non-empty canonical set on the very first consolidate
# pass. Mirrors saga's _CANONICAL_PREDICATE_SEED / _SUBJECT_SEED.
_CANONICAL_PREDICATE_SEED = (
    "prefers", "lives_in", "works_at", "employed_at", "born_in",
    "studied_at", "graduated_with_degree_in", "owns", "uses",
    "manages", "manufactures", "located_in", "married_to",
    "parent_of", "child_of", "sibling_of", "friend_of",
    "interested_in", "dislikes", "fluent_in",
)
_CANONICAL_SUBJECT_SEED = ("User", "Assistant")


def build_vocab_block(
    conn,
    *,
    top_n_predicates: int = 25,
    top_n_subjects: int = 15,
    extra_subjects: list[str] | None = None,
) -> str:
    """Build the P48 canonical-vocabulary block injected between TRIPLES
    rules and TEMPORAL TAGS in the rich prompt. Reads top-N predicates
    and subjects from the live ``triples`` table by frequency, unions
    them with the static seed, and (for subjects only) appends
    operator-supplied canonical names like identities.yaml entries.

    Returns an empty string when there's nothing to surface (cold DB
    with no seed and no extras — shouldn't happen since the seed is
    always non-empty, but defensive).
    """
    pred_lines: list[tuple[str, int | None]] = []
    subj_lines: list[tuple[str, int | None]] = []
    seen_preds: set[str] = set()
    seen_subjs: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT predicate, COUNT(*) c FROM triples "
            "WHERE tombstoned = 0 "
            "GROUP BY predicate ORDER BY c DESC LIMIT ?",
            (top_n_predicates,),
        ).fetchall()
        for pred, cnt in rows:
            if pred and pred not in seen_preds:
                pred_lines.append((pred, int(cnt or 0)))
                seen_preds.add(pred)
        rows = conn.execute(
            "SELECT subject, COUNT(*) c FROM triples "
            "WHERE tombstoned = 0 "
            "GROUP BY subject ORDER BY c DESC LIMIT ?",
            (top_n_subjects,),
        ).fetchall()
        for subj, cnt in rows:
            if subj and subj not in seen_subjs:
                subj_lines.append((subj, int(cnt or 0)))
                seen_subjs.add(subj)
    except Exception as exc:
        # DB read failed — fall back to seed-only. Logged so it surfaces
        # in operator logs rather than producing a silent quality drop
        # on the consolidation pass.
        logger.warning("vocab_block DB read failed; using seed-only: %s", exc)
    # Union with the static seed.
    for p in _CANONICAL_PREDICATE_SEED:
        if p not in seen_preds:
            pred_lines.append((p, None))
            seen_preds.add(p)
    for s in _CANONICAL_SUBJECT_SEED:
        if s not in seen_subjs:
            subj_lines.append((s, None))
            seen_subjs.add(s)
    # Operator-supplied canonical subjects (identities.yaml).
    if extra_subjects:
        for s in extra_subjects:
            if isinstance(s, str) and s.strip() and s not in seen_subjs:
                subj_lines.append((s.strip(), None))
                seen_subjs.add(s.strip())
    if not pred_lines and not subj_lines:
        return ""

    def _render(lines):
        return ", ".join(
            f"{name} ({cnt})" if cnt is not None else name
            for name, cnt in lines
        )

    parts = [
        "Existing canonical vocabulary (PREFER reusing these — counts in "
        "parens for DB-derived entries; bare names are seed values):",
    ]
    if pred_lines:
        parts.append("Predicates: " + _render(pred_lines))
    if subj_lines:
        parts.append("Subjects: " + _render(subj_lines))
    return "\n".join(parts) + "\n\n"


def build_prior_block(conn, evidence_ids: list[str]) -> str:
    """Build the P47 prior-beliefs block for a single cluster. Finds
    observations whose evidence is a strict subset of ``evidence_ids``
    and renders the triples those observations carry as
    ``(subject, predicate, object)`` lines.

    Empty string when there are no subset observations or they carry no
    triples — the prompt's ``{prior_block}`` placeholder gracefully
    handles that.

    The LLM then either restates each prior in its own TRIPLES section
    (if still supported), revises (if the new atoms contradict), or
    omits (if the prior is no longer true). Matches saga P47.
    """
    if len(evidence_ids) < 2:
        return ""
    placeholders = ",".join("?" * len(evidence_ids))
    # Find observations whose evidence is a strict subset of the
    # incoming cluster. Push the narrowing into SQL so we only scan
    # observations whose evidence OVERLAPS with the target set
    # (which is the universe of candidates anyway — if no overlap,
    # no chance of subset). For a year-old DB with thousands of
    # observations this avoids an O(N_obs) pull-to-Python.
    rows = conn.execute(
        f"SELECT obs.id, GROUP_CONCAT(ar.target_id, '|') AS evi "
        f"FROM atoms obs "
        f"JOIN atom_relations ar ON ar.source_id = obs.id "
        f" AND ar.relation_type = 'evidenced_by' "
        f"WHERE obs.memory_type = 'observation' AND obs.tombstoned = 0 "
        f"  AND obs.id IN ("
        f"    SELECT source_id FROM atom_relations "
        f"    WHERE target_id IN ({placeholders}) "
        f"    AND relation_type = 'evidenced_by'"
        f"  ) "
        f"GROUP BY obs.id",
        list(evidence_ids),
    ).fetchall()
    target_set = set(evidence_ids)
    subset_obs: list[str] = []
    for obs_id, evi in rows:
        old_set = set((evi or "").split("|")) - {""}
        if old_set and old_set < target_set:  # strict subset
            subset_obs.append(obs_id)
    if not subset_obs:
        return ""
    # Fetch triples attached to those observations.
    obs_placeholders = ",".join("?" * len(subset_obs))
    triple_rows = conn.execute(
        f"SELECT subject, predicate, object FROM triples "
        f"WHERE source_atom_id IN ({obs_placeholders}) "
        f"AND tombstoned = 0",
        subset_obs,
    ).fetchall()
    if not triple_rows:
        return ""
    prior_lines = [
        f"({subj}, {pred}, {obj})"
        for subj, pred, obj in triple_rows
    ]
    return (
        "Previous beliefs about these atoms (from earlier consolidations "
        "on a smaller evidence set):\n"
        + "\n".join(prior_lines)
        + "\n\nFor each previous belief: if the new atoms still support "
        "it, restate it in your TRIPLES section; if the new atoms revise "
        "or contradict it, output the updated version (or omit if it's "
        "no longer true).\n\n"
    )


_TRIPLES_HEADER = re.compile(r"^\s*TRIPLES\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
_CONTRADICTIONS_HEADER = re.compile(
    r"^\s*CONTRADICTIONS\s*:?\s*$", re.IGNORECASE | re.MULTILINE,
)


def _parse_contradictions(raw: str) -> list[dict]:
    """Pull the CONTRADICTIONS section out of the rich response.

    Recognized line format::

        3 vs 7: <one-sentence summary>

    Returns ``[{atom_index_a, atom_index_b, summary}, ...]``. Empty
    when the section is missing or the LLM wrote "NONE".
    """
    if not raw:
        return []
    m = _CONTRADICTIONS_HEADER.search(raw)
    if not m:
        return []
    body = raw[m.end():]
    # Stop at the next ALL-CAPS section header if any.
    end_m = re.search(r"^\s*(OBSERVATION|TRIPLES)\s*:?\s*$",
                       body, re.IGNORECASE | re.MULTILINE)
    if end_m:
        body = body[: end_m.start()]
    out: list[dict] = []
    line_re = re.compile(
        r"^\s*(\d+)\s*(?:vs|VS|v\.|×|x)\s*(\d+)\s*:\s*(.+?)\s*$",
        re.MULTILINE,
    )
    for line_m in line_re.finditer(body):
        try:
            a = int(line_m.group(1))
            b = int(line_m.group(2))
        except ValueError:
            continue
        if a == b:
            continue
        summary = line_m.group(3).strip()
        if not summary or summary.upper() == "NONE":
            continue
        out.append({"atom_index_a": a, "atom_index_b": b, "summary": summary})
    return out


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


def make_async_rich_synth_fn(
    *,
    llm_config: dict | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.3,
) -> Callable[..., Any]:
    """Async rich synthesizer — returns OBSERVATION + TRIPLES +
    CONTRADICTIONS in a single LLM call.

    Signature: ``_do(cluster, *, prior_block="", vocab_block="")``.
    Caller (e.g. ``MemoryClient.consolidate``) can inject:

    - ``prior_block`` (P47): a per-cluster string surfacing existing
      observations whose evidence is a subset of this cluster, so the
      LLM revises rather than duplicates. Build via
      ``build_prior_block(conn, evidence_ids)``.
    - ``vocab_block`` (P48): a per-run string surfacing the live DB's
      canonical predicates + subjects + operator-supplied identities,
      so the LLM canonicalizes against existing vocabulary. Build via
      ``build_vocab_block(conn, extra_subjects=...)``.

    Both default to empty — the prompt's ``{prior_block}`` and
    ``{vocab_block}`` placeholders gracefully render nothing when off,
    keeping the bench harness's no-priors / no-canonicals path
    behavior-equivalent.

    Returns a coroutine that resolves to a dict::

        {
            "content": "<observation prose>",
            "topics": ["topic", ...],
            "triples": [
                {"subject": ..., "predicate": ..., "object": ...,
                 "valid_from": ...?, "valid_until": ...?},
                ...
            ],
            "contradictions": [
                {"atom_index_a": int, "atom_index_b": int,
                 "summary": "..."},
                ...
            ],
        }
    """
    async def _do(
        cluster: list[dict], *,
        prior_block: str = "",
        vocab_block: str = "",
    ) -> dict:
        from ._llm import call_llm
        from ._config_io import resolve_llm_config

        cfg = llm_config or resolve_llm_config("consolidation")
        indexed = "\n".join(
            f"[{i + 1}] {a['content']}"
            for i, a in enumerate(cluster)
        )
        prompt = RICH_PROMPT.format(
            n=len(cluster),
            indexed_atoms=indexed,
            prior_block=prior_block,
            vocab_block=vocab_block,
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
                "rich_synth_fn LLM call failed (cluster size=%d): %s",
                len(cluster), exc,
            )
            return {
                "content": "", "topics": [],
                "triples": [], "contradictions": [],
            }

        # Lazy import to avoid the cycle when triples.py imports
        # back from synthesize for prompt-shared helpers.
        from .triples import parse_triples

        return {
            "content": _parse_observation(raw),
            "topics": _collect_topics(cluster),
            "triples": parse_triples(raw),
            "contradictions": _parse_contradictions(raw),
        }

    return _do


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
        from ._llm import call_llm
        from ._config_io import resolve_llm_config

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
        from ._llm import call_llm
        from ._config_io import resolve_llm_config

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
