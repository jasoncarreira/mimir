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
  skill to drive reflection refine/retire proposed-changes.
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


def recall_skill_learnings(
    conn: sqlite3.Connection,
    skill: str,
    *,
    limit: int = 10,
    kinds: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Return a skill's learning atoms, newest-first, for injection on load.

    Direct SQL (not the FAISS/FTS pipeline): at skill-load time the
    "query" is just "I'm loading skill X", so we want all of that skill's
    learnings ranked by recency, not semantic similarity to the turn.
    Activation-aware ranking can layer on when the load injection wires in.

    *kinds* optionally restricts to a valence subset (e.g.
    ``NEGATIVE_KINDS`` for the #267 surfacing path). ``None`` = all kinds.

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
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT id, content, json_extract(metadata, '$.kind') AS kind, created_at
        FROM atoms
        WHERE source_type = ?
          AND json_extract(metadata, '$.skill') = ?
          AND tombstoned = 0
          {kind_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        {"id": r[0], "content": r[1], "kind": r[2], "created_at": r[3]}
        for r in rows
    ]


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
