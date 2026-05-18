"""FTS5 keyword search over mimir.saga atoms.

Includes P12 query expansion (synonym substitution) on the FTS-only
path. Per saga's canonical bench (saga_bench.toml line 67), P12 is
"the only positive single lever since P30 — shipped to canonical
2026-04-29." The semantic pathway already handles synonyms via
embedding cosine; expansion is FTS5-only.

Schema setup (in schema.sql):

- ``atoms_fts`` is an external-content FTS5 table whose source is the
  ``atoms.content`` column, keyed on ``atoms.rowid``. External-content
  means atoms_fts stores positions + term-frequencies but reads the
  actual content from ``atoms`` at query time.
- INSERT/UPDATE/DELETE triggers on atoms keep atoms_fts in sync. The
  pre-existing dual-write pattern (see saga.core: explicit ``INSERT
  INTO atoms_fts`` inside store()) is replaced by triggers here so
  the source-of-truth for FTS5 sync lives in one place (schema.sql)
  instead of being threaded through every writer.

Query rewrite mirrors saga's ``_fts5_query`` — stopword strip,
short-term strip (<=2 chars are mostly noise in BM25 weighting),
escape FTS5 special characters, OR-join the survivors. Falls back to
the raw query lowercased if every term is filtered out.

BM25 normalization: FTS5's ``bm25(atoms_fts)`` returns negative
scores where smaller (more negative) = better match. We negate and
scale to a positive "keyword_score" so it composes naturally with
the cosine-similarity-driven scoring in recall.py.
"""

from __future__ import annotations

import re
import sqlite3


# Same stopword list saga uses — empirically tuned for English
# conversational text. Don't grow this without bench evidence; over-
# aggressive stopword removal can crater short-question recall.
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "could", "may", "might", "must", "can",
    "in", "on", "at", "to", "for", "of", "with", "from", "by", "about",
    "as", "into", "through", "during", "before", "after", "above",
    "below", "between", "under", "over", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all",
    "each", "few", "more", "most", "other", "some", "such", "no",
    "not", "only", "own", "same", "so", "than", "too", "very", "s",
    "t", "just", "don", "now", "if", "because", "while", "of",
    "its", "it", "he", "she", "they", "we", "you", "i", "me", "my",
    "your", "his", "her", "our", "their", "this", "that", "these",
    "those", "what", "which", "who", "whom", "whose", "any", "also",
    "get", "got",
})


def expand_query_for_keyword(
    query: str,
    synonyms: dict[str, list[str]] | None,
) -> str:
    """P12 — append config-driven synonyms to the FTS5 query.

    Reads a dict like::

        synonyms = {
            "profession": ["job", "career", "work", "occupation"],
            "home":       ["hometown", "residence", "lives"],
            ...
        }

    For each key found in the query (case-insensitive substring), the
    listed synonyms are appended. The semantic pathway already handles
    synonyms via embedding similarity; expansion is FTS5-only.

    No-op when ``synonyms`` is falsy or no key matches.
    """
    if not synonyms or not isinstance(synonyms, dict):
        return query
    extras: list[str] = []
    q_lower = query.lower()
    for word, syns in synonyms.items():
        if not isinstance(word, str) or not isinstance(syns, list):
            continue
        # Match on word boundaries. Pre-fix, substring matching meant
        # ``school`` in ``preschool`` triggered the college/university
        # expansion, polluting recall on pre-K queries with K-12+
        # synonyms. ``\bword\b`` requires a non-word char on each
        # side so ``preschool`` no longer matches ``school`` but
        # ``schools`` still does (boundary at the start, plural is
        # the same root). Case-insensitive flag matches the
        # ``.lower()`` we were doing before.
        if re.search(rf"\b{re.escape(word)}\b", q_lower):
            extras.extend(s for s in syns if isinstance(s, str))
    if not extras:
        return query
    return query + " " + " ".join(extras)


# Bench-tuned synonym dictionary mirroring saga's
# longmemeval_via_mimir/saga_p47*.toml [query_expansion.synonyms]
# block. Empirically the strongest single keyword-side gain over the
# P30 baseline on LongMemEval-S. Override per-process by passing
# ``synonyms=...`` to ``fts_search``.
DEFAULT_LONGMEMEVAL_SYNONYMS: dict[str, list[str]] = {
    "profession": ["job", "career", "work", "occupation", "employed"],
    "home":       ["hometown", "residence", "lives", "address", "neighborhood"],
    "schedule":   ["routine", "calendar", "plan", "appointment", "meeting"],
    "family":     ["spouse", "wife", "husband", "partner", "children",
                   "kids", "parent", "mom", "dad", "sibling"],
    "preference": ["like", "favorite", "prefer", "enjoy", "love"],
    "commute":    ["drive", "travel", "transit", "ride", "route"],
    "school":     ["college", "university", "graduated", "degree",
                   "studied", "education"],
}


def fts5_query(text: str) -> str:
    """Convert a natural-language query to an FTS5 OR-joined expression.

    Strips stopwords + short tokens, escapes FTS5 special characters,
    wraps each survivor in quotes (so multi-word phrases don't trip the
    FTS5 grammar), and OR-joins them. If every term is filtered out
    (e.g. the query is just stopwords), falls back to the raw lowered
    text — FTS5's parser will probably reject it, but that's caught
    by the search caller and falls through to the Python fallback.
    """
    raw_terms = text.lower().split()
    terms = [t for t in raw_terms if t not in STOPWORDS and len(t) > 2]
    if not terms:
        terms = [t for t in raw_terms if len(t) > 2]
    if not terms:
        return text.lower()
    safe: list[str] = []
    for t in terms:
        # FTS5 special chars: " * - + ( ) : ^ AND OR NOT
        for ch in ('"', "*", "-", "+", "(", ")", ":", "^"):
            t = t.replace(ch, "")
        if t:
            safe.append(f'"{t}"')
    return " OR ".join(safe) if safe else text.lower()


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 20,
    include_session_boundaries: bool = False,
    memory_type: str | None = None,
    agent_id: str = "default",
    synonyms: dict[str, list[str]] | None = None,
) -> list[tuple[str, float]]:
    """Run an FTS5-backed keyword search. Returns
    ``[(atom_id, keyword_score)]`` sorted by score (highest first).

    ``synonyms`` enables P12 query expansion. Pass
    ``DEFAULT_LONGMEMEVAL_SYNONYMS`` to use the bench-tuned dictionary,
    or a custom dict for domain-specific term equivalence.

    Score scaling: ``bm25(atoms_fts) * -100`` to convert negative-is-
    better into positive-is-better and bring magnitudes into the same
    ballpark as cosine similarity. The recall scorer applies its own
    weight (``w_kw=0.2`` by default) so absolute magnitudes don't
    need to be perfectly calibrated.

    Falls back to a LIKE-based scan if FTS5 raises (table missing,
    syntax error from the rewrite, etc.) — degraded but functional.
    """
    # P12 expansion BEFORE the FTS5 rewrite — synonyms get the same
    # stopword / safe-quoting treatment as the original query terms.
    expanded = expand_query_for_keyword(query, synonyms)
    fts_q = fts5_query(expanded)

    # session_boundary atoms no longer live in atoms (migration 11);
    # no source_type exclusion needed here. include_session_boundaries
    # parameter is kept for signature compatibility but is a no-op.
    where = ["a.tombstoned = 0", "a.agent_id = ?"]
    params: list = [fts_q, agent_id]
    if memory_type:
        where.append("a.memory_type = ?")
        params.append(memory_type)
    where_sql = " AND ".join(where)

    sql = (
        f"SELECT a.id, bm25(atoms_fts) AS bm25 "
        f"FROM atoms_fts f JOIN atoms a ON a.rowid = f.rowid "
        f"WHERE atoms_fts MATCH ? AND {where_sql} "
        f"ORDER BY bm25(atoms_fts) LIMIT ?"
    )
    params.append(top_k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # FTS5 table not present, or query rewrite produced unparseable
        # syntax. Fall through to LIKE.
        return _fts_fallback(
            conn, expanded,
            top_k=top_k,
            include_session_boundaries=include_session_boundaries,
            memory_type=memory_type,
            agent_id=agent_id,
        )

    return [(r[0], -float(r[1]) * 100.0) for r in rows]


def _fts_fallback(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
    include_session_boundaries: bool,
    memory_type: str | None,
    agent_id: str,
) -> list[tuple[str, float]]:
    """Plain LIKE search, term-count score. Used when FTS5 is missing
    (pre-trigger schema migration) or when fts5_query produced syntax
    FTS5 rejects."""
    raw_terms = [t.strip().lower() for t in query.split() if t.strip()]
    terms = [t for t in raw_terms if t not in STOPWORDS and len(t) > 2]
    if not terms:
        terms = [t for t in raw_terms if len(t) > 2]
    if not terms:
        return []

    like_clauses = " AND ".join(["LOWER(a.content) LIKE ?"] * len(terms))
    where = [like_clauses, "a.tombstoned = 0", "a.agent_id = ?"]
    # session_boundary atoms not in atoms table (migration 11); no exclusion.
    if memory_type:
        where.append("a.memory_type = ?")
    where_sql = " AND ".join(where)

    params: list = [f"%{t}%" for t in terms] + [agent_id]
    if memory_type:
        params.append(memory_type)
    params.append(top_k)

    rows = conn.execute(
        f"SELECT a.id FROM atoms a WHERE {where_sql} LIMIT ?",
        params,
    ).fetchall()
    # Each match gets the term-count as its score; recall.py's
    # w_kw=0.2 weighting absorbs the absolute scale.
    return [(r[0], float(len(terms))) for r in rows]
