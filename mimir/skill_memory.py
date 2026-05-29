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
from datetime import datetime, timezone

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
    """Return a skill's learning atoms, ranked by ACT-R activation, for
    injection on load (chainlink #266 slice 6).

    Direct SQL fetch (not the FAISS/FTS pipeline): at skill-load time the
    "query" is just "I'm loading skill X", so semantic similarity is not
    useful.  We fetch all learnings then rank by ACT-R activation so that
    atoms the synthesis turn voted *useful* (weight-2.0 feedback_positive
    events) surface first in the top-K.

    Initial ordering is equivalent to recency because new atoms carry only
    their 'store' access event.  After saga_feedback voting cycles the
    activation of frequently-useful learnings diverges upward, driving the
    "votes determine what survives the top-K" property described in the
    chainlink spec.  We deliberately do NOT fire an access event on
    injection — that would ossify the first-injected 8 regardless of
    quality.

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
    # Over-fetch slightly so the activation sort can pick the best *limit* from
    # a wider pool.  Cap at 3× the requested limit to bound the SQL scan.
    fetch_limit = min(int(limit) * 3, 100)
    params.append(fetch_limit)
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
    if not rows:
        return []

    atoms = [
        {"id": r[0], "content": r[1], "kind": r[2], "created_at": r[3]}
        for r in rows
    ]

    # ── Activation ranking ──────────────────────────────────────────
    # Fetch the denormalized access summary for each candidate and compute
    # Petrov OL activation.  Atoms with no summary (shouldn't happen after
    # store(), but defensive) fall back to 0.0 (sorted last).
    from .saga.activation import compute_activation  # local import avoids circular
    atom_ids_list = [a["id"] for a in atoms]
    placeholders = ",".join(["?"] * len(atom_ids_list))
    summary_rows = conn.execute(
        f"SELECT atom_id, recent_ts_json, recent_weights_json, "
        f"old_count, old_weight_sum, old_oldest_ts "
        f"FROM atom_access_summary WHERE atom_id IN ({placeholders})",
        atom_ids_list,
    ).fetchall()
    summaries = {
        r[0]: {
            "recent_ts": json.loads(r[1] or "[]"),
            "recent_weights": json.loads(r[2] or "[]"),
            "old_count": r[3] or 0,
            "old_weight_sum": r[4] or 0.0,
            "old_oldest_ts": r[5],
        }
        for r in summary_rows
    }
    now = datetime.now(timezone.utc)
    for atom in atoms:
        s = summaries.get(atom["id"])
        if s is None:
            atom["_act"] = 0.0
        else:
            try:
                atom["_act"] = compute_activation(
                    recent_ts=s["recent_ts"],
                    recent_weights=s["recent_weights"],
                    old_count=s["old_count"],
                    old_weight_sum=s["old_weight_sum"],
                    old_oldest_ts=s["old_oldest_ts"],
                    now=now,
                )
            except Exception:  # noqa: BLE001 — activation error must not drop atom
                atom["_act"] = 0.0

    atoms.sort(key=lambda a: a["_act"], reverse=True)
    for atom in atoms:
        del atom["_act"]
    return atoms[:int(limit)]


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
) -> tuple[str, list[str]]:
    """Append a skill's recalled learnings to its SKILL.md *body* for
    load-time injection (chainlink #266 read path).

    Recalls up to *limit* learnings for *skill* (ALL kinds — a tip and a
    gotcha both help the next invocation) and appends them under
    ``_LEARNINGS_HEADING``.

    Returns ``(augmented_body, atom_ids)`` so callers can register the
    injected learning atoms on the turn's votable set (slice 6). The
    synthesis turn then scores them via ``saga_feedback`` — useful learnings
    accrue activation, stale ones decay out, driving activation-based
    recall ranking on future skill loads.

    When the skill has no learnings, returns ``(body, [])`` — body
    unchanged, empty list, no votable atoms. Best-effort: any DB error
    returns ``(body, [])`` rather than failing the skill load.
    """
    try:
        learnings = recall_skill_learnings(conn, skill, limit=limit)
    except Exception:  # noqa: BLE001 — skill load must not fail on a recall error
        return body, []
    rendered = render_skill_learnings(learnings)
    if not rendered:
        return body, []
    atom_ids = [item["id"] for item in learnings]
    augmented = (
        f"{body}\n\n{_LEARNINGS_HEADING}\n{rendered}"
        f"\n\n{_LEARNINGS_NUDGE}"
    )
    return augmented, atom_ids


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
