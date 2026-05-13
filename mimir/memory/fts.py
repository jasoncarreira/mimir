"""FTS5 keyword search over mimir.memory atoms.

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
) -> list[tuple[str, float]]:
    """Run an FTS5-backed keyword search. Returns
    ``[(atom_id, keyword_score)]`` sorted by score (highest first).

    Score scaling: ``bm25(atoms_fts) * -100`` to convert negative-is-
    better into positive-is-better and bring magnitudes into the same
    ballpark as cosine similarity. The recall scorer applies its own
    weight (``w_kw=0.2`` by default) so absolute magnitudes don't
    need to be perfectly calibrated.

    Falls back to a LIKE-based scan if FTS5 raises (table missing,
    syntax error from the rewrite, etc.) — degraded but functional.
    """
    fts_q = fts5_query(query)

    where = ["a.tombstoned = 0", "a.agent_id = ?"]
    params: list = [fts_q, agent_id]
    if not include_session_boundaries:
        where.append("(a.source_type IS NULL OR a.source_type != 'session_boundary')")
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
            conn, query,
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
    if not include_session_boundaries:
        where.append("(a.source_type IS NULL OR a.source_type != 'session_boundary')")
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
