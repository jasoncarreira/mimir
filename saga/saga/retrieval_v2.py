#!/usr/bin/env python3
"""
MSAM Retrieval v2: Nine improvements for 1M-scale retrieval.

Improvements:
  1. Triple-augmented retrieval (entity→triple→atom graph traversal)
  2. Query expansion via entity resolution
  3. Temporal query detection + recency filtering
  4. Atom quality scoring (pre-computed information density)
  5. Negative example tracking (implicit feedback)
  6. Cross-encoder re-ranking (NIM API)
  7. Embedding model upgrade path (hot-swap support)
  8. Query rewriting (pattern-based expansion)
  9. Beam search retrieval (multi-path merge)

All improvements are independently toggleable via saga.toml [retrieval_v2].
"""

import re
import math
import time
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from .config import get_config

_cfg = get_config()


# ═══════════════════════════════════════════════════════════════════
# 1. TRIPLE-AUGMENTED RETRIEVAL
# ═══════════════════════════════════════════════════════════════════

async def triple_augmented_retrieve(query: str, mode: str = "task", top_k: int = 12) -> list[dict]:
    """
    Use triples as a bridge: query → entities → triples → source atoms.
    
    When embedding similarity fails (e.g., "What shows is the user performing in?"
    doesn't match "Hamilton")), triples provide the link:
      query entities: [User, shows, performing]
      triples: (User) --[performs_in]--> (Hamilton)
      source atom: "Show: Hamilton on Broadway"
    """
    from .core import get_db, hybrid_retrieve
    
    conn = get_db()
    
    # Extract entities from query
    entities = extract_query_entities(query)
    
    if not entities:
        return await hybrid_retrieve(query, mode=mode, top_k=top_k)
    
    # Find triples matching any entity
    triple_atom_ids = set()
    matched_triples = []
    
    for entity in entities:
        entity_lower = entity.lower()
        rows = conn.execute("""
            SELECT atom_id, subject, predicate, object, confidence
            FROM triples WHERE state = 'active'
            AND (LOWER(subject) LIKE ? OR LOWER(object) LIKE ?)
        """, (f'%{entity_lower}%', f'%{entity_lower}%')).fetchall()
        
        for row in rows:
            triple_atom_ids.add(row[0])
            matched_triples.append({
                'atom_id': row[0],
                'subject': row[1],
                'predicate': row[2],
                'object': row[3],
                'confidence': row[4],
                'matched_entity': entity,
            })
    
    # Get standard retrieval results
    standard_results = await hybrid_retrieve(query, mode=mode, top_k=top_k)
    standard_ids = {a.get('id', '') for a in standard_results}
    
    # Fetch triple-linked atoms that standard retrieval missed
    augmented = []
    for atom_id in triple_atom_ids:
        if atom_id not in standard_ids:
            row = conn.execute(
                "SELECT * FROM atoms WHERE id = ? AND state IN ('active', 'fading')",
                (atom_id,)
            ).fetchone()
            if row:
                atom = dict(row)
                # Score based on triple confidence and number of matching triples
                matching = [t for t in matched_triples if t['atom_id'] == atom_id]
                triple_boost = sum(t['confidence'] for t in matching)
                atom['_combined_score'] = triple_boost * 3.0  # scale to be competitive
                atom['_triple_augmented'] = True
                atom['_matched_triples'] = len(matching)
                atom.pop('embedding', None)
                augmented.append(atom)
    
    # Merge: standard results + triple-augmented, sort by score, take top_k
    all_results = standard_results + augmented
    all_results.sort(key=lambda x: x.get('_combined_score', 0), reverse=True)
    
    return all_results[:top_k]


_QUERY_STOPWORDS = frozenset({
    'What', 'Where', 'When', 'Which', 'Who', 'Whom', 'How', 'Why',
    'Does', 'Did', 'Can', 'Could', 'Would', 'Should', 'Will',
    'Are', 'Is', 'Was', 'Were', 'Has', 'Have', 'Had',
    'The', 'This', 'That', 'These', 'Those',
})


def extract_query_entities(query: str) -> list[str]:
    """Extract named entities from a query using pattern matching."""
    entities = []
    
    # Capitalized words (likely proper nouns) -- excluding query stopwords
    caps = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b', query)
    entities.extend(c for c in caps if c not in _QUERY_STOPWORDS)
    
    # Known entity patterns
    known_entities = {
        'user': 'User', 'agent': 'Agent', 'msam': 'MSAM',
        
        'openclaw': 'OpenClaw',
    }
    q_lower = query.lower()
    for key, canonical in known_entities.items():
        if key in q_lower and canonical not in entities:
            entities.append(canonical)
    
    # Also extract key nouns (words > 4 chars that aren't stopwords)
    from .core import _STOPWORDS
    words = query.split()
    for w in words:
        w_clean = re.sub(r'[^\w]', '', w)
        if len(w_clean) > 4 and w_clean.lower() not in _STOPWORDS and w_clean not in entities:
            entities.append(w_clean)
    
    return entities


# ═══════════════════════════════════════════════════════════════════
# 3. TEMPORAL QUERY DETECTION
# ═══════════════════════════════════════════════════════════════════

TEMPORAL_SIGNALS = {
    'today': 1,
    'yesterday': 1,
    'recent': 2,
    'recently': 2,
    'latest': 2,
    'last week': 7,
    'this week': 7,
    'last month': 30,
    'this month': 30,
    'just now': 0.1,
    'earlier': 1,
    'ago': 3,
}


def detect_temporal_scope(query: str) -> Optional[int]:
    """
    Detect if query has temporal scope. Returns max age in days, or None.
    
    "What happened today?" → 1
    "Recent events" → 2
    "Last week's conversations" → 7
    """
    q_lower = query.lower()
    
    for signal, days in TEMPORAL_SIGNALS.items():
        if signal in q_lower:
            return days
    
    return None


def apply_temporal_filter(atoms: list[dict], max_age_days: int) -> list[dict]:
    """Filter atoms by creation date and boost recent ones."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    
    filtered = []
    for atom in atoms:
        try:
            created = datetime.fromisoformat(atom['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            
            if created >= cutoff:
                # Recency boost: newer atoms within window score higher
                age_hours = (now - created).total_seconds() / 3600
                recency_boost = 1.0 / (1.0 + age_hours / 24.0)  # decay over days
                atom['_combined_score'] = atom.get('_combined_score', 0) * (1 + recency_boost)
                atom['_temporal_boosted'] = True
                filtered.append(atom)
        except (KeyError, ValueError):
            # No valid timestamp -- include without boost
            filtered.append(atom)
    
    # Re-sort by boosted score
    filtered.sort(key=lambda x: x.get('_combined_score', 0), reverse=True)
    return filtered


# ═══════════════════════════════════════════════════════════════════
# 5. NEGATIVE EXAMPLE TRACKING
# ═══════════════════════════════════════════════════════════════════

def init_feedback_table():
    """Create implicit feedback tracking table."""
    from .core import get_db
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            atom_id TEXT NOT NULL,
            retrieved_rank INTEGER,
            was_used BOOLEAN,  -- True if sentence extracted, False if skipped
            similarity REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_atom 
        ON retrieval_feedback(atom_id)
    """)
    conn.commit()


def log_retrieval_feedback(query: str, atom_id: str, rank: int, 
                           was_used: bool, similarity: float = 0):
    """Log whether a retrieved atom was actually useful."""
    from .core import get_db
    conn = get_db()
    init_feedback_table()
    conn.execute("""
        INSERT INTO retrieval_feedback (query, atom_id, retrieved_rank, was_used, similarity)
        VALUES (?, ?, ?, ?, ?)
    """, (query, atom_id, rank, was_used, similarity))
    conn.commit()


def get_atom_usefulness(atom_id: str) -> float:
    """
    Get atom's historical usefulness ratio.
    Returns: ratio of times used / times retrieved. 1.0 = always useful.
    """
    from .core import get_db
    conn = get_db()
    init_feedback_table()
    
    row = conn.execute("""
        SELECT COUNT(*) as total, SUM(CASE WHEN was_used THEN 1 ELSE 0 END) as used
        FROM retrieval_feedback WHERE atom_id = ?
    """, (atom_id,)).fetchone()
    
    total = row[0] or 0
    used = row[1] or 0
    
    if total < 3:
        return 0.5  # Not enough data, neutral
    
    return used / total


# ═══════════════════════════════════════════════════════════════════
# 6. CROSS-ENCODER RE-RANKING (NIM API)
# ═══════════════════════════════════════════════════════════════════

async def rerank_with_llm(query: str, atoms: list[dict], top_k: int = 5) -> list[dict]:
    """
    Re-rank retrieved atoms using LLM-as-judge.
    The LLM understands semantic roles (who is described vs who is describing)
    which embedding models cannot distinguish.

    Falls back to original ranking if API fails.
    """
    import requests
    from .config import resolve_llm_config

    llm = resolve_llm_config('retrieval_v2')

    if not llm['api_key'] or not atoms or len(atoms) <= 1:
        return atoms[:top_k]

    # Only rerank top candidates (limit to 8 to keep prompt short)
    candidates = atoms[:min(8, len(atoms))]

    # Build ranking prompt
    passages_text = '\n'.join(
        f'{i}: {a.get("content", "")[:150]}'
        for i, a in enumerate(candidates)
    )

    prompt = f"""Rank these passages by relevance to the query. Return ONLY the indices in order, most relevant first. No explanation.

Query: {query}

Passages:
{passages_text}

Ranking:"""

    try:
        from ._llm import call_llm
        ranking_text = await call_llm(
            llm, prompt=prompt, temperature=0, max_tokens=30,
        )
        if ranking_text:
            # Parse indices from response (e.g., "2, 0, 1, 3" or "2 0 1 3")
            indices = []
            for token in re.findall(r'\d+', ranking_text):
                idx = int(token)
                if idx < len(candidates) and idx not in indices:
                    indices.append(idx)
            
            if indices:
                reranked = []
                for rank, idx in enumerate(indices[:top_k]):
                    atom = candidates[idx].copy()
                    atom['_rerank_score'] = len(indices) - rank  # higher = better
                    atom['_original_rank'] = idx
                    atom['_reranked'] = True
                    reranked.append(atom)
                
                # Append any remaining atoms not in reranked list
                reranked_ids = {a.get('id') for a in reranked}
                for a in atoms:
                    if a.get('id') not in reranked_ids and len(reranked) < top_k:
                        reranked.append(a)
                
                return reranked[:top_k]
    except Exception:
        pass
    
    return atoms[:top_k]


# ═══════════════════════════════════════════════════════════════════
# 7. EMBEDDING MODEL HOT-SWAP
# ═══════════════════════════════════════════════════════════════════

def check_embedding_upgrade() -> dict:
    """
    Check if a better embedding model is available and report migration cost.
    
    Returns upgrade recommendation with estimated re-embedding cost.
    """
    from .core import get_db
    conn = get_db()
    
    total_atoms = conn.execute("SELECT COUNT(*) FROM atoms WHERE state = 'active'").fetchone()[0]
    total_triples = conn.execute("SELECT COUNT(*) FROM triples WHERE state = 'active'").fetchone()[0]
    total_sentences = conn.execute("SELECT COUNT(*) FROM sentence_embeddings").fetchone()[0]
    
    total_embeddings = total_atoms + total_triples + total_sentences
    
    # NIM API: ~0.5s per embedding call, 10 batch size typical
    est_time_minutes = (total_embeddings / 10) * 0.5 / 60
    
    return {
        "current_model": _cfg('embedding', 'model', 'nvidia/nv-embedqa-e5-v5'),
        "total_embeddings_to_migrate": total_embeddings,
        "atoms": total_atoms,
        "triples": total_triples,
        "sentences": total_sentences,
        "estimated_time_minutes": round(est_time_minutes, 1),
        "recommended_upgrades": [
            {
                "model": "nvidia/nv-embedqa-e5-v5",
                "status": "current",
                "dim": 1024,
            },
            {
                "model": "nvidia/llama-3.2-nv-embedqa-1b-v2", 
                "status": "available",
                "dim": 2048,
                "note": "2x dimensions, better semantic coverage, ~2x slower",
            },
        ],
    }


def migrate_embeddings(new_model: str, batch_size: int = 10):
    """
    Re-embed all content with a new model.
    Preserves old embeddings in a backup column.
    """
    from .core import get_db
    from .embeddings import embed_text
    
    conn = get_db()
    
    # Backup current embeddings
    try:
        conn.execute("ALTER TABLE atoms ADD COLUMN embedding_backup BLOB")
        conn.execute("UPDATE atoms SET embedding_backup = embedding")
        conn.commit()
        print("Backed up existing embeddings")
    except sqlite3.OperationalError:
        pass
    
    # Re-embed atoms
    atoms = conn.execute("SELECT id, content FROM atoms WHERE state = 'active'").fetchall()
    for i, (atom_id, content) in enumerate(atoms):
        emb = embed_text(content)
        if emb:
            import struct
            blob = struct.pack(f'{len(emb)}f', *emb)
            conn.execute("UPDATE atoms SET embedding = ? WHERE id = ?", (blob, atom_id))
        if (i + 1) % batch_size == 0:
            conn.commit()
            print(f"  Atoms: {i+1}/{len(atoms)}")
    conn.commit()
    
    # Re-embed sentences
    from .subatom import cache_all_sentences
    conn.execute("DELETE FROM sentence_embeddings")  # Clear cache
    conn.commit()
    cache_all_sentences(batch_size=batch_size)
    
    print(f"Migration complete: {len(atoms)} atoms + sentences re-embedded")


# ═══════════════════════════════════════════════════════════════════
# 9. BEAM SEARCH RETRIEVAL
# ═══════════════════════════════════════════════════════════════════

async def beam_search_retrieve(
    query: str,
    mode: str = "task",
    top_k: int = 12,
    beam_width: int = 3,
) -> list[dict]:
    """Single-beam fallback after the cleanup batch removed regex
    rewrite (beam 2) and triple-graph expand (beam 3). P43 reintroduces
    a multi-beam shape with subatom as beam 2.
    """
    from .core import hybrid_retrieve
    return await hybrid_retrieve(query, mode=mode, top_k=top_k)


# ═══════════════════════════════════════════════════════════════════
# UNIFIED RETRIEVAL PIPELINE
# ═══════════════════════════════════════════════════════════════════

async def retrieve_v2(
    query: str,
    mode: str = "task",
    top_k: int = 12,
) -> list[dict]:
    """
    Full v2 retrieval pipeline. All 9 improvements in sequence.
    Each step is independently toggleable.
    
    Pipeline:
      query → rewrite (8) → expand (2) → beam search (9) 
      → triple augment (1) → temporal filter (3)
      → quality filter (4) → re-rank (6) → feedback log (5)
    
    Embedding upgrade (7) is a migration tool, not per-query.
    """
    t0 = time.time()
    
    original_query = query
    
    # Step 3: Beam search or standard retrieval (9)
    # Three modes: True (always), False (never), "auto" (dynamic gate on atom count)
    beam_setting = _cfg('retrieval_v2', 'enable_beam_search', 'auto')
    if beam_setting == 'auto':
        # Dynamic gate: only activate beam search when DB is large enough to benefit
        from .core import get_db
        _conn = get_db()
        atom_count = _conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        _conn.close()
        beam_threshold = _cfg('retrieval_v2', 'beam_search_atom_threshold', 10000)
        use_beam = atom_count >= beam_threshold
    else:
        use_beam = bool(beam_setting)

    if use_beam:
        beam_width = _cfg('retrieval_v2', 'beam_width', 3)
        atoms = await beam_search_retrieve(query, mode=mode, top_k=top_k * 2, beam_width=beam_width)
    else:
        from .core import hybrid_retrieve
        atoms = await hybrid_retrieve(query, mode=mode, top_k=top_k * 2)
    
    # Step 4: Triple augmentation (1)
    if _cfg('retrieval_v2', 'enable_triple_augment', True):
        entities = extract_query_entities(original_query)
        if entities:
            from .core import get_db
            conn = get_db()
            existing_ids = {a.get('id', '') for a in atoms}

            try:
                for entity in entities:
                    entity_lower = entity.lower()
                    rows = conn.execute("""
                        SELECT DISTINCT atom_id FROM triples
                        WHERE state = 'active'
                        AND (LOWER(subject) LIKE ? OR LOWER(object) LIKE ?)
                    """, (f'%{entity_lower}%', f'%{entity_lower}%')).fetchall()

                    for (atom_id,) in rows:
                        if atom_id not in existing_ids:
                            row = conn.execute(
                                "SELECT * FROM atoms WHERE id = ? AND state IN ('active', 'fading')",
                                (atom_id,)
                            ).fetchone()
                            if row:
                                atom = dict(row)
                                atom['_combined_score'] = 2.0  # baseline for triple-linked
                                atom['_triple_augmented'] = True
                                atom.pop('embedding', None)
                                atoms.append(atom)
                                existing_ids.add(atom_id)
            finally:
                conn.close()
    
    # Re-sort after filters
    atoms.sort(key=lambda x: x.get('_combined_score', 0), reverse=True)
    atoms = atoms[:top_k]
    
    # Step 7: LLM re-ranking (6)
    if _cfg('retrieval_v2', 'enable_rerank', False):  # Off by default (latency)
        atoms = await rerank_with_llm(original_query, atoms, top_k=top_k)
    
    # Step 8: Log feedback for future learning (5)
    if _cfg('retrieval_v2', 'enable_feedback', True):
        try:
            init_feedback_table()
        except Exception:
            pass
    
    latency_ms = (time.time() - t0) * 1000
    
    # Tag results with pipeline metadata
    for atom in atoms:
        atom['_retrieval_version'] = 'v2'
        atom['_latency_ms'] = round(latency_ms, 1)
    
    return atoms


# ═══════════════════════════════════════════════════════════════════
# BENCHMARK
# ═══════════════════════════════════════════════════════════════════

async def benchmark_v2():
    """Compare v1 (hybrid_retrieve) vs v2 (retrieve_v2) on quality."""
    from .core import hybrid_retrieve

    queries = [
        ("Who is the user?", "companion"),
        ("What is MSAM?", "task"),
        ("What projects is the user working on?", "companion"),
        ("What is the user's profession?", "companion"),
        ("Security rules for the system", "task"),
        ("How does model routing work?", "task"),
        ("What happened today?", "task"),
        ("Emotional state and boundaries", "companion"),
        ("What is the agent's personality?", "task"),
        ("Recent conversations and events", "task"),
    ]

    print("=" * 80)
    print("RETRIEVAL V2 BENCHMARK: v1 vs v2")
    print("=" * 80)

    for query, mode in queries:
        v1 = await hybrid_retrieve(query, mode=mode, top_k=5)
        v2 = await retrieve_v2(query, mode=mode, top_k=5)
        
        v1_ids = [a.get('id', '')[:8] for a in v1]
        v2_ids = [a.get('id', '')[:8] for a in v2]
        new_in_v2 = [aid for aid in v2_ids if aid not in v1_ids]
        
        print(f"\nQuery: \"{query}\"")
        print(f"  v1 top: {v1[0]['content'][:60] if v1 else 'NONE'}")
        print(f"  v2 top: {v2[0]['content'][:60] if v2 else 'NONE'}")
        if new_in_v2:
            # Show what v2 found that v1 didn't
            for atom in v2:
                if atom.get('id', '')[:8] in new_in_v2:
                    aug = ' [TRIPLE]' if atom.get('_triple_augmented') else ''
                    beam = f' [BEAM:{atom.get("_beam", "")}]' if atom.get('_beam') else ''
                    print(f"  NEW:    {atom['content'][:60]}{aug}{beam}")
        else:
            print(f"  (same results)")
    
    print(f"\n{'=' * 80}")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python retrieval_v2.py benchmark        # Compare v1 vs v2")
        print("  python retrieval_v2.py query <text>      # Test v2 retrieval")
        print("  python retrieval_v2.py quality           # Pre-compute atom quality")
        print("  python retrieval_v2.py embedding-check   # Check embedding upgrade")
        print("  python retrieval_v2.py feedback-stats    # Show feedback statistics")
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    import asyncio
    if cmd == "benchmark":
        asyncio.run(benchmark_v2())
    elif cmd == "query":
        query = " ".join(sys.argv[2:])
        results = asyncio.run(retrieve_v2(query, top_k=5))
        print(f"Query: {query}")
        for a in results:
            score = a.get('_combined_score', 0)
            aug = ' [T]' if a.get('_triple_augmented') else ''
            beam = f' [{a.get("_beam", "")}]' if a.get('_beam') else ''
            rew = ' [R]' if a.get('_reranked') else ''
            print(f"  [{score:.2f}]{aug}{beam}{rew} {a['content'][:70]}")
    elif cmd == "embedding-check":
        info = check_embedding_upgrade()
        print(json.dumps(info, indent=2))
    elif cmd == "feedback-stats":
        from .core import get_db
        conn = get_db()
        init_feedback_table()
        total = conn.execute("SELECT COUNT(*) FROM retrieval_feedback").fetchone()[0]
        used = conn.execute("SELECT COUNT(*) FROM retrieval_feedback WHERE was_used = 1").fetchone()[0]
        print(f"Total feedback entries: {total}")
        print(f"Used: {used} ({used/max(total,1)*100:.0f}%)")
    else:
        print(f"Unknown command: {cmd}")
