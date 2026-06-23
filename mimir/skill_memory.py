"""Skill memory — per-skill learnings stored as SAGA atoms (chainlink #266).

Replaces the rejected per-skill ``.memory.md`` design (#263/#264). Skill
learnings (gotchas, input-quirks, perf-caveats, tips) live as SAGA atoms
so they ride the existing recall ranking, ACT-R decay, consolidation/dedup,
and atom-level ``saga_feedback`` — rather than an unbounded append-only
markdown file that would re-open the library-bloat problem MUSE-Autoskill
(arXiv 2605.27366) warns about and that SAGA already solves.

## Atom convention
- ``source_type = "skill_learning"``
- ``metadata = {"skill": "<name>", "kind": "<kind>"}``

``kind`` is the **content valence**, declared at write time by the writer
(the in-turn agent or the session-boundary synthesis), because it knows
its own intent. It is NOT derivable from activation or feedback score —
those measure whether the atom was *useful*, which is orthogonal to what
the note *says*. A frequently-useful cautionary gotcha and a frequently-
useful tip both have high activation; only the declared ``kind`` tells
them apart. The two valences compose: ``kind`` is stable (what it says);
``saga_feedback`` stale-marks a gotcha that stops being true, so it
decays out (self-heal) without changing its ``kind``.

## Recall semantics
- **On skill load** (read path, #266): recall *all* kinds for the skill —
  a tip and a gotcha both help the next invocation. Ranked by recency
  (activation-aware ranking folds in when the load injection lands).
- **General recall**: ``skill_learning`` atoms are EXCLUDED (see
  ``mimir/saga/recall.py``) so a circuit-breaker gotcha never surfaces as
  a "memory" in an unrelated turn — it only appears via the skill-load
  injection.
- **Operator surfacing** (#267): negative-kind learnings are counted per
  skill to drive reflection refine/retire recommendations.
"""

from __future__ import annotations

import json
import sqlite3

# Atom source_type tag for skill-learning atoms. Recall (mimir/saga/recall.py)
# excludes this source_type from general candidate hydration.
SKILL_LEARNING_SOURCE_TYPE = "skill_learning"

# Content-valence kinds. NEGATIVE = cautionary (the skill misled / has a
# sharp edge); POSITIVE = how-to / it-worked. Declared at write time.
NEGATIVE_KINDS: frozenset[str] = frozenset(
    {"failure-mode", "input-quirk", "perf-caveat"}
)
POSITIVE_KINDS: frozenset[str] = frozenset({"tip", "success-pattern"})
ALL_KINDS: frozenset[str] = NEGATIVE_KINDS | POSITIVE_KINDS


def is_negative_kind(kind: str) -> bool:
    """True if *kind* is a cautionary/negative learning kind."""
    return kind in NEGATIVE_KINDS


def build_metadata(skill: str, kind: str) -> dict[str, str]:
    """Build the metadata dict for a skill-learning atom.

    Raises ``ValueError`` on an empty skill name or an unrecognized
    ``kind`` — valence is a closed enum so the surfacing layer (#267) can
    filter on it reliably; a typo'd kind would silently drop a learning
    out of the negative-count.
    """
    if not skill or not skill.strip():
        raise ValueError("skill name is required")
    if kind not in ALL_KINDS:
        raise ValueError(
            f"unknown skill-learning kind {kind!r}; "
            f"expected one of {sorted(ALL_KINDS)}"
        )
    return {"skill": skill.strip(), "kind": kind}


def _activations_for(
    conn: sqlite3.Connection,
    atom_ids: list[str],
    *,
    now=None,
) -> dict[str, float]:
    """Map each atom_id → its ACT-R activation from ``atom_access_summary``.

    Mirrors recall.py's activation pass: one summary query, then
    ``compute_activation`` per atom. Atoms with no summary row (shouldn't
    happen — ``store()`` logs an initial access event) are omitted; the
    caller treats missing as ``-inf`` so they sort last behind a recency
    tiebreak. ``now`` is injectable for deterministic tests; ``None``
    defaults to the current time inside ``compute_activation``.
    """
    if not atom_ids:
        return {}
    from .saga.activation import compute_activation
    placeholders = ",".join("?" * len(atom_ids))
    rows = conn.execute(
        f"SELECT atom_id, recent_ts_json, recent_weights_json, old_count, "
        f"old_weight_sum, old_oldest_ts FROM atom_access_summary "
        f"WHERE atom_id IN ({placeholders})",
        list(atom_ids),
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        out[r[0]] = compute_activation(
            recent_ts=json.loads(r[1] or "[]"),
            recent_weights=json.loads(r[2] or "[]"),
            old_count=r[3] or 0,
            old_weight_sum=r[4] or 0.0,
            old_oldest_ts=r[5],
            now=now,
        )
    return out


def recall_skill_learnings(
    conn: sqlite3.Connection,
    skill: str,
    *,
    limit: int = 10,
    kinds: frozenset[str] | set[str] | None = None,
    now=None,
) -> list[dict]:
    """Return a skill's learning atoms, activation-ranked, for load injection.

    Direct SQL (not the FAISS/FTS pipeline): at skill-load time the
    "query" is just "I'm loading skill X", so we rank that skill's
    learnings by **ACT-R activation** (chainlink #266 slice 6) rather than
    semantic similarity. Activation folds in recency for never-curated
    atoms — so this degrades to newest-first when nothing's been voted on
    — and lifts learnings the agent has marked *useful* at the
    session-boundary synthesis turn (a ``feedback_positive`` access event).
    ``created_at`` desc is the tiebreak. The result is that the genuinely
    useful gotchas survive the top-*limit* cut instead of being evicted by
    whatever was written most recently.

    *kinds* optionally restricts to a valence subset (e.g.
    ``NEGATIVE_KINDS`` for the #267 surfacing path). ``None`` = all kinds.
    *now* is injectable for deterministic tests.

    Each result: ``{"id", "content", "kind", "created_at"}``. Tombstoned
    atoms are excluded.
    """
    if not skill or not skill.strip():
        return []
    params: list = [SKILL_LEARNING_SOURCE_TYPE, skill.strip()]
    kind_clause = ""
    if kinds:
        kind_list = sorted(kinds)
        kind_clause = (
            f" AND json_extract(metadata, '$.kind') IN "
            f"({','.join(['?'] * len(kind_list))})"
        )
        params.extend(kind_list)
    # No SQL LIMIT: skill atoms are sparse, and we rank by activation in
    # Python before taking the top-*limit*. A SQL ``LIMIT`` on created_at
    # would drop a high-activation older learning before it could be
    # ranked. The fetch + sort is O(N) / O(N log N) over a skill's full
    # live learning set; this assumes that set stays small — bounded by
    # per-skill dedup (consolidate_skill_memories) + forget pruning +
    # decay. If a heavily-used skill ever accumulates hundreds of live
    # learnings, revisit with a recency pre-cap before the activation
    # sort (#268).
    rows = conn.execute(
        f"""
        SELECT id, content, json_extract(metadata, '$.kind') AS kind, created_at
        FROM atoms
        WHERE source_type = ?
          AND json_extract(metadata, '$.skill') = ?
          AND tombstoned = 0
          {kind_clause}
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()
    if not rows:
        return []
    activations = _activations_for(conn, [r[0] for r in rows], now=now)
    neg_inf = float("-inf")
    # Sort by (activation desc, created_at desc). created_at is an ISO
    # string, so lexical desc == recency desc — a valid tiebreak when
    # activations match (e.g. all just-stored, never-curated learnings).
    # This relies on the store layer writing created_at in a single
    # uniform ISO-8601 format (UTC, consistent precision); a mixed format
    # (naive local time, varying fractional digits) would lexically
    # mis-sort the tiebreak. store.py writes uniform timestamps, so this
    # holds for atoms this code creates.
    ranked = sorted(
        rows,
        key=lambda r: (activations.get(r[0], neg_inf), r[3]),
        reverse=True,
    )
    return [
        {"id": r[0], "content": r[1], "kind": r[2], "created_at": r[3]}
        for r in ranked[: int(limit)]
    ]


def render_skill_learnings(learnings: list[dict]) -> str:
    """Render recalled skill-learning atoms as a compact prompt block.

    One bullet per learning: ``- [<kind>] <content>``. Newest-first
    (recall order preserved). Returns ``""`` for an empty list so callers
    can cheaply skip the section. Content is single-lined so a multi-line
    learning can't break the surrounding markdown structure.
    """
    if not learnings:
        return ""
    lines: list[str] = []
    for item in learnings:
        kind = item.get("kind") or "?"
        content = " ".join(str(item.get("content") or "").split())
        lines.append(f"- [{kind}] {content}")
    return "\n".join(lines)


# Heading the injected learnings block carries when appended to a skill's
# SKILL.md body at load time. Distinct enough to be greppable.
_LEARNINGS_HEADING = "## Learnings from past runs"

# One-line in-turn write nudge appended under the learnings block (#266
# slice 3). Closes the loop: the model sees past learnings AND is reminded
# it can record a new one the moment this run reveals it — not only at the
# session-end synthesis turn. Only shown when learnings already render, so
# no-memory skill loads stay byte-for-byte their SKILL.md.
_LEARNINGS_NUDGE = (
    "_Hit a new gotcha, quirk, or tip running this skill? Record it now "
    "with `saga_record_skill_learning(skill, kind, content)` so the next "
    "run gets it too._"
)


def augment_skill_body(
    conn: sqlite3.Connection,
    skill: str,
    body: str,
    *,
    limit: int = 8,
    now=None,
) -> tuple[str, list[str]]:
    """Append a skill's recalled learnings to its SKILL.md *body* for
    load-time injection (chainlink #266 read path).

    Recalls up to *limit* learnings for *skill* (ALL kinds — a tip and a
    gotcha both help the next invocation, activation-ranked) and appends
    them under ``_LEARNINGS_HEADING``.

    Returns ``(augmented_body, injected_atom_ids)``. The atom IDs let the
    caller record which learnings were injected this turn so the
    session-boundary synthesis turn can curate feedback on them (slice 6).
    When the skill has no learnings, returns ``(body, [])`` unchanged (a
    skill with no accumulated memory injects exactly its SKILL.md, no empty
    section). Best-effort: any DB error returns ``(body, [])`` rather than
    failing the skill load.
    """
    try:
        learnings = recall_skill_learnings(conn, skill, limit=limit, now=now)
    except Exception:  # noqa: BLE001 — skill load must not fail on a recall error
        return body, []
    rendered = render_skill_learnings(learnings)
    if not rendered:
        return body, []
    ids = [str(item["id"]) for item in learnings if item.get("id")]
    augmented = (
        f"{body}\n\n{_LEARNINGS_HEADING}\n{rendered}"
        f"\n\n{_LEARNINGS_NUDGE}"
    )
    return augmented, ids


def count_negative_learnings(
    conn: sqlite3.Connection,
    skill: str,
    *,
    since_iso: str | None = None,
) -> int:
    """Count a skill's cautionary (negative-kind) learning atoms.

    Drives the #267 reflection threshold ("skill X has N negative learnings
    → refine/retire candidate"). *since_iso* optionally bounds to a recent
    window (atoms older than it are excluded), so a skill that was fixed
    long ago — whose old gotchas have decayed/aged — doesn't keep tripping
    the threshold. Tombstoned atoms are excluded.
    """
    if not skill or not skill.strip():
        return 0
    neg = sorted(NEGATIVE_KINDS)
    params: list = [SKILL_LEARNING_SOURCE_TYPE, skill.strip(), *neg]
    since_clause = ""
    if since_iso:
        since_clause = " AND created_at >= ?"
        params.append(since_iso)
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM atoms
        WHERE source_type = ?
          AND json_extract(metadata, '$.skill') = ?
          AND json_extract(metadata, '$.kind') IN ({','.join(['?'] * len(neg))})
          AND tombstoned = 0
          {since_clause}
        """,
        params,
    ).fetchone()
    return int(row[0]) if row else 0
