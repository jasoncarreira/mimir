"""SagaClient-compatible facade over the new memory subsystem.

Adapts ``mimir.memory.*`` operations to the ``SagaClient`` Protocol
(see ``mimir/saga_client.py``) so mimir's call sites — ``agent.py``,
``sagatools.py``, ``server.py`` — can flip from saga → memory atomically
with one wiring change (``make_saga_client(..)`` returns a
``MemoryClient`` instead of an ``_InProcessSaga``).

Provider/index plumbing:

- Embedding provider: ``saga.embeddings.get_provider()`` — reused
  as-is. Configurable via saga.toml (voyage / openai / onnx).
- FAISS index: ``mimir.memory.vector_index.VectorIndex`` — owns its
  own index keyed on ``mimir.memory.db``'s embeddings table. Built
  lazily on first ``query()``; incrementally updated after each
  ``store()`` via ``on_atom_stored``.
- FTS5: ``mimir.memory.fts.fts_search`` — BM25 over the ``atoms_fts``
  virtual table. Triggers in schema.sql keep atoms_fts in sync with
  atoms; the client just calls the search.
- LLM synth for consolidate: ``mimir.memory.synthesize.
  make_async_observation_synth_fn`` — wraps saga's ``call_llm`` so
  consolidate() can actually emit observations rather than no-op'ing.

v2 is operationally complete: real FAISS over mimir.memory.db, real
FTS5, real LLM-backed consolidation. Embeddings still flow through
saga's provider — that stays until the final mimir/memory →
mimir/saga rename, at which point we move the provider too.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

from .mark_access import AccessEvent, mark_access
from .recall import recall as _recall
from .store import store as _store
from .reflect import (
    reflect as _reflect,
    recent_session_boundaries as _recent_boundaries,
)
from .consolidate import consolidate as _consolidate
from .forget import forget as _forget
from .fts import fts_search
from .vector_index import VectorIndex


# ─── Provider/index adapters ─────────────────────────────────────────


def _embed_text_sync(text: str) -> tuple[bytes, str, str, int]:
    """Adapt saga.embeddings.get_provider() to the store.EmbedFn shape.

    Returns (vec_bytes, provider_name, model, dim). Sync because the
    provider call is itself sync (network I/O is hidden inside).
    """
    from saga.embeddings import get_provider
    from saga.config import get_config

    cfg = get_config()
    provider = get_provider()
    vec = provider.embed(text[:cfg("embedding", "max_input_chars", 2000)],
                          input_type="passage")
    provider_name = cfg("embedding", "provider", "unknown")
    model = cfg("embedding", "model", "unknown")
    dim = provider.dimensions()
    vec_bytes = struct.pack(f"{dim}f", *vec)
    return vec_bytes, provider_name, model, dim


def _query_embed_sync(text: str) -> list[float]:
    """Adapt for recall.QueryEmbedFn — returns float list (not bytes)."""
    from saga.embeddings import get_provider
    from saga.config import get_config

    cfg = get_config()
    provider = get_provider()
    return provider.embed(text[:cfg("embedding", "max_input_chars", 2000)],
                          input_type="query")


def _make_faiss_search_fn(index: VectorIndex | None):
    """Closure over the VectorIndex matching recall.FaissSearchFn shape."""
    def _fn(query_emb: list[float], top_k: int) -> list[tuple[str, float]]:
        if index is None:
            return []
        return index.search(query_emb, top_k=top_k)
    return _fn


def _make_fts_search_fn(
    conn: sqlite3.Connection,
    *,
    agent_id: str = "default",
    synonyms: dict[str, list[str]] | None = None,
):
    """Closure over the connection matching recall.FtsSearchFn shape.
    ``synonyms`` is the P12 query-expansion dict (FTS-only pathway)."""
    def _fn(query: str, top_k: int) -> list[tuple[str, float]]:
        return fts_search(
            conn, query, top_k=top_k,
            agent_id=agent_id, synonyms=synonyms,
        )
    return _fn


def _make_triple_search_fn(conn: sqlite3.Connection, *, dim: int | None):
    """Closure over the connection matching recall.TripleSearchFn shape.
    Returns None when triples are disabled (the dim arg is None, meaning
    the FAISS index isn't built — same condition under which the
    semantic pathway would also be empty)."""
    from .triples import triple_augment_search

    def _fn(query_emb: list[float], top_k: int) -> list[tuple[str, float]]:
        return triple_augment_search(conn, query_emb, top_k=top_k, dim=dim)
    return _fn


# ─── The facade ──────────────────────────────────────────────────────


class MemoryClient:
    """SagaClient-compatible facade. Holds a sqlite3 connection to
    mimir.memory.db and translates each saga-vocabulary method to the
    equivalent ``mimir.memory.*`` operation.

    Connection lifecycle: the client opens one connection per process
    on first use, applies the schema if the file is fresh, and reuses
    that connection. Caller can also pass an open connection via
    ``conn=...`` for tests.

    All public methods are async to match SagaClient. CPU-bound work
    runs via ``asyncio.to_thread`` so mimir's event loop stays
    responsive during synthesis / consolidation passes.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        conn: sqlite3.Connection | None = None,
        agent_id: str = "default",
        embedding_dim: int | None = None,
        synonyms: dict[str, list[str]] | None = None,
        include_triples_in_response: bool = True,
        triples_top_n: int = 10,
    ) -> None:
        self._db_path = db_path
        self._conn = conn  # may be None until first use
        self._agent_id = agent_id
        self._embedding_dim = embedding_dim
        # P12 synonyms for FTS5-only query expansion. Saga's canonical
        # bench passes the bench-tuned dict; production callers can
        # pass None (no expansion) or a domain-specific dict.
        self._synonyms = synonyms
        # P42 half-2: surface a top-N triples block in query responses.
        # ON by default so production prompt rendering picks it up
        # automatically; the bench harness leaves this on too, which
        # means triples land in the payload but ``saga.harness.build_prompt``
        # ignores them (only consumes observations + raws) — bench-neutral.
        # Set to False to opt out (saga's older default).
        self._include_triples_in_response = include_triples_in_response
        self._triples_top_n = triples_top_n
        self._index: VectorIndex | None = None
        self._index_built = False
        # LLM synth callable for consolidate(). Late-bound (lazy import
        # of synthesize.py) so MemoryClient doesn't transitively pull in
        # the saga LLM transport at construction time.
        self._observation_synth_fn = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self._db_path is None:
            raise RuntimeError(
                "MemoryClient: no db_path and no conn provided. "
                "Construct with MemoryClient(db_path=Path(...)) or pass conn=..."
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self._db_path.exists()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if fresh:
            schema_path = Path(__file__).parent / "schema.sql"
            self._conn.executescript(schema_path.read_text())
            self._conn.commit()
        return self._conn

    def _ensure_index(self, conn: sqlite3.Connection) -> VectorIndex | None:
        """Lazily build the FAISS index on first retrieval. After build,
        store() incremental-adds keep it current; periodic rebuilds
        handle tombstoning accumulation."""
        if self._index_built:
            return self._index
        # Determine the embedding dim from the first embedding row if
        # not pre-set; falls back to 1024 (voyage default).
        dim = self._embedding_dim
        if dim is None:
            row = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()
            dim = row[0] if row else 1024
            self._embedding_dim = dim
        self._index = VectorIndex(dimension=dim)
        self._index.build_from_db(conn)
        self._index_built = True
        return self._index

    def rebuild_index(self) -> None:
        """Force a full FAISS rebuild from the current DB state.
        Called by the bench harness between per-question DBs and by
        the migration importer after bulk-loading atoms."""
        conn = self._ensure_conn()
        if self._index is None:
            self._ensure_index(conn)
        else:
            self._index.build_from_db(conn)

    # ── SagaClient surface ──────────────────────────────────────────

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
        reference_date=None,
        enable_contextual_rewrite: bool = False,
    ) -> dict[str, Any]:
        # Opt-in contextual rewrite (saga's P-feature, off in bench).
        # We only call the LLM when both the flag and a non-empty
        # context are present — empty context is a no-op so the cost
        # never pays for nothing. The rewritten form replaces the
        # original for retrieval; the response includes both so the
        # caller (turn viewer / metrics) can see what was done.
        rewritten_query: str | None = None
        if enable_contextual_rewrite and context:
            from .query_rewrite import rewrite_query
            try:
                rewritten_query = await rewrite_query(query, context)
            except Exception:
                rewritten_query = None
        effective_query = rewritten_query or query

        def _do():
            conn = self._ensure_conn()
            index = self._ensure_index(conn)
            # Triple-augment pathway uses the SAME embedding dim as the
            # atom-level FAISS index (triples are embedded under the
            # same provider). Pass dim through so triples with a stale
            # dim get filtered.
            triple_dim = self._embedding_dim
            result = _recall(
                conn, effective_query,
                query_embed_fn=_query_embed_sync,
                faiss_search_fn=_make_faiss_search_fn(index),
                fts_search_fn=_make_fts_search_fn(
                    conn, agent_id=self._agent_id,
                    synonyms=self._synonyms,
                ),
                triple_search_fn=_make_triple_search_fn(conn, dim=triple_dim),
                k=top_k,
                session_id=session_id,
                agent_id=self._agent_id,
                reference_date=reference_date,
                min_confidence_tier=min_confidence_tier,
            )
            # P42 half-2: surface a top-N triples block in the response so
            # production prompt rendering (mimir/sagatools.py:_format_saga_payload)
            # can show structured (s, p, o) facts alongside obs/raws, and
            # the post-message hook's _source_atom_ids_from_triples can
            # credit atoms via mark_contributions. Opt-in via the
            # ``include_triples`` kwarg below — left ON when triples are
            # populated in the DB. Empty list when the triples table is
            # empty (no extra prompt block, no behavior change).
            triples_payload: list[dict[str, Any]] = []
            if self._include_triples_in_response:
                from .triples import top_triples_with_payload
                rich = top_triples_with_payload(
                    conn, _query_embed_sync(effective_query),
                    top_n=self._triples_top_n, dim=triple_dim,
                )
                # Strip the internal _cosine field from the wire shape;
                # keep it out of the agent-facing dict.
                for t in rich:
                    triples_payload.append({
                        k: v for k, v in t.items() if not k.startswith("_")
                    })
            # Translate the RecallResult into saga's response shape so
            # mimir's call sites don't change.
            return {
                "query": query, "mode": mode, "two_tier": True,
                "gated": result.gated,
                "gated_reason": result.gated_reason,
                "observations": [_candidate_to_atom(c) for c in result.observations],
                "raws": [_candidate_to_atom(c) for c in result.raws],
                "triples": triples_payload,
                "items_returned": len(result.observations) + len(result.raws),
                "rewritten_query": (
                    rewritten_query
                    or (result.rewritten_query or "")
                ),
            }
        return await asyncio.to_thread(_do)

    async def store(
        self, content: str, *, stream: str | None = None,
        profile: str | None = None, source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
        precomputed_embedding: tuple[bytes, str, str, int] | None = None,
        session_id: str | None = None,
        session_dedup_threshold: float | None = None,
    ) -> dict[str, Any]:
        # session_dedup_threshold is forwarded straight to store() — off
        # by default, opt-in by callers that want session-paraphrase
        # collapse. The bench harness leaves it None.
        def _do():
            conn = self._ensure_conn()
            result = _store(
                conn, content, embed_fn=_embed_text_sync,
                stream=stream or "semantic",
                profile=profile or "standard",
                source_type=source_type,
                metadata=metadata,
                agent_id=self._agent_id,
                session_id=session_id,
                precomputed_embedding=precomputed_embedding,
                session_dedup_threshold=session_dedup_threshold,
            )
            if result.stored:
                # Incremental-add to the FAISS index if it's already
                # been built. If not, the next query's lazy build
                # will pick the new atom up from disk.
                if self._index is not None and self._index.built:
                    row = conn.execute(
                        "SELECT vec FROM embeddings WHERE atom_id = ?",
                        (result.atom_id,),
                    ).fetchone()
                    if row is not None and row[0] is not None:
                        self._index.add(result.atom_id, row[0])
                return {"stored": True, "atom_id": result.atom_id}
            return {
                "stored": False, "atom_id": result.atom_id,
                "reason": result.reason or "duplicate",
            }
        return await asyncio.to_thread(_do)

    async def feedback(
        self, atom_ids: list[str], response_text: str, *,
        session_id: str | None = None, feedback: str | None = None,
    ) -> dict[str, Any]:
        # Saga's feedback contract is "credit pass after generating a
        # response." Map atom_ids → feedback_positive events
        # (the response_text was generated using these; that's the
        # endorsement). 'feedback' parameter (positive/negative) maps
        # to our signal kwarg.
        signal = (feedback or "positive").lower()
        def _do():
            from . import feedback as _feedback
            conn = self._ensure_conn()
            n = _feedback(conn, atom_ids, signal=signal, session_id=session_id)
            return {"marked": n, "total": len(atom_ids)}
        return await asyncio.to_thread(_do)

    async def outcome(
        self, atom_ids: list[str], feedback: str, *,
        session_id: str | None = None, query: str | None = None,
    ) -> dict[str, Any]:
        """Outcome is saga's "after the response was delivered, was it
        well-received?" signal.

        - ``feedback="positive"`` → write a ``feedback_positive``
          access event on each atom (weight 2.0). Same as the credit
          pass.
        - ``feedback="negative"`` → write a ``feedback_negative`` event
          (weight 0.0 — no activation contribution; the event is the
          flag). ``forget_by_criteria`` can later use this to surface
          atoms for review by joining on access_events.
        - other values → no-op.

        Returns ``{"marked": n, "total": len(atom_ids), "signal": ...}``.
        """
        signal = (feedback or "").lower()
        if signal == "positive":
            return await self.feedback(
                atom_ids, "", session_id=session_id, feedback="positive",
            )
        if signal == "negative":
            def _do():
                conn = self._ensure_conn()
                events = [AccessEvent(
                    atom_id=aid, source="feedback_negative",
                    session_id=session_id,
                    metadata={"reason": "outcome_negative"},
                ) for aid in atom_ids]
                if not events:
                    return {"marked": 0, "total": 0, "signal": "negative"}
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    n = mark_access(conn, events)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return {"marked": n, "total": len(atom_ids), "signal": "negative"}
            return await asyncio.to_thread(_do)
        return {"marked": 0, "total": len(atom_ids), "signal": signal or "noop"}

    async def end_session(
        self, session_id: str, summary: str, *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        """Close a session. ``channel_id`` is persisted on the
        sessions row so ``recent_session_boundaries(channel_id=...)``
        can scope to a single channel — without it the LEFT JOIN on
        sessions can't filter (every boundary's channel_id would be
        NULL).
        """
        # The agent has already done the synthesis — it's calling
        # end_session with the rendered fields. The new reflect()
        # would re-derive them via LLM; here we just persist what was
        # passed in. We supply a stub boundary_synth_fn that returns
        # the agent's pre-computed fields.
        def _stub_synth(_atoms, _ctx):
            return {
                "summary": summary,
                "topics_discussed": topics_discussed or [],
                "decisions_made": decisions_made or [],
                "unfinished": unfinished or [],
                "emotional_state": emotional_state,
            }
        def _do():
            conn = self._ensure_conn()
            result = _reflect(
                conn, session_id=session_id, channel_id=channel_id,
                embed_fn=_embed_text_sync,
                boundary_synth_fn=_stub_synth,
                # observation_synth_fn=None — saga's end_session
                # didn't auto-synthesize observations; the
                # consolidator did. Same here: leave observation
                # synthesis to consolidate().
            )
            # Return BOTH the saga-compatible ``atom_id`` (consumed by
            # mimir/sagatools.py:583/603 for the local boundary mirror +
            # the user-facing success message) AND the
            # ``boundary_atom_id`` alias for clarity. Dropping either
            # breaks an existing call site silently.
            return {
                "atom_id": result.boundary_atom_id,
                "boundary_atom_id": result.boundary_atom_id,
                "session_id": session_id,
                "channel": channel_id,
                "boundary_created": result.boundary_created,
                "session_member_count": result.session_member_count,
            }
        return await asyncio.to_thread(_do)

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
        lookback_days: int = 30,
        min_cluster_size: int = 3,
    ) -> dict[str, Any]:
        """Cross-session consolidation pass. Runs the LLM-backed
        observation synthesizer over recent raw atoms; emits one
        observation per cluster that survives the supersession/equal-
        evidence checks. See ``consolidate.consolidate()`` for the
        per-cluster contract.

        ``dry_run=True`` walks the candidate set and reports cluster
        counts without paying any LLM cost — useful for the bench
        harness's pre-flight check.
        """
        from .cluster import make_default_cluster_fn

        # Build/look up the cached LLM synth_fn. The rich variant
        # returns observation + triples + contradictions in one call;
        # the per-cluster restructure pass then routes each output
        # into the right table. Cached on the client because
        # make_async_rich_synth_fn closes over llm_config (resolved
        # once per process is fine).
        if self._observation_synth_fn is None:
            from .synthesize import make_async_rich_synth_fn
            self._observation_synth_fn = make_async_rich_synth_fn()

        # Synth is async, but consolidate() runs synchronously under
        # to_thread (so transactions stay in one thread). We adapt: a
        # sync wrapper that re-enters the running loop is unsafe
        # (asyncio.run inside an executor thread would deadlock against
        # the parent loop). Instead, we resolve clusters here on the
        # caller's loop, run the LLM calls concurrently, and pass a
        # pre-computed lookup into a sync consolidate variant.
        conn = self._ensure_conn()

        # 1. Candidate selection + clustering (sync; reads only).
        from .consolidate import (
            _candidate_raws, MIN_CLUSTER_SIZE_FOR_OBSERVATION,
            MAX_OBSERVATIONS_PER_RUN,
        )
        raws = await asyncio.to_thread(
            _candidate_raws,
            conn,
            lookback_days=lookback_days,
            agent_id=self._agent_id,
        )
        if len(raws) < min_cluster_size:
            return {"clusters_formed": 0, "observations_emitted": []}

        cluster_fn = make_default_cluster_fn(conn)
        clusters = await asyncio.to_thread(cluster_fn, raws)

        if dry_run:
            return {
                "dry_run": True,
                "candidates_scanned": len(raws),
                "clusters_found": len(clusters),
                "total_atoms_in_clusters": sum(len(c) for c in clusters),
            }

        # 2. LLM synthesis fan-out — concurrent calls, bounded by a
        # semaphore so we don't blow the provider's rate limits.
        # Reuses saga's call_llm transport (anthropic/openai_compat
        # plumbing already lives there).
        max_obs = max_clusters or MAX_OBSERVATIONS_PER_RUN
        eligible = [c for c in clusters if len(c) >= min_cluster_size][:max_obs]
        sem = asyncio.Semaphore(4)

        async def _synth(cluster):
            async with sem:
                try:
                    return await self._observation_synth_fn(cluster)
                except Exception:
                    return {"content": "", "topics": [],
                            "triples": [], "contradictions": []}

        results = await asyncio.gather(*[_synth(c) for c in eligible])

        # 3. Per-cluster restructure: store observation, link evidence,
        # emit access events. Each cluster runs its own short transaction
        # so an LLM failure on one doesn't block the others. Done in a
        # thread so SQLite stays on one writer thread.
        def _restructure():
            from .consolidate import (
                find_equal_evidence_obs, find_superseded_observations,
            )
            from .observations import refresh_trend
            from .store import store as _store_atom
            from .triples import store_triples
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            emitted: list[str] = []
            superseded: list[tuple[str, str]] = []
            triples_stored = 0
            contradicts_stored = 0
            for cluster, result in zip(eligible, results):
                content = result.get("content", "")
                topics = result.get("topics", [])
                triples = result.get("triples", [])
                contradictions = result.get("contradictions", [])
                if not content or not content.strip():
                    continue
                evidence_ids = [a["id"] for a in cluster]
                existing_equal = find_equal_evidence_obs(conn, set(evidence_ids))
                if existing_equal:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        mark_access(conn, [AccessEvent(
                            atom_id=existing_equal, source="consolidation",
                        )])
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    continue

                store_result = _store_atom(
                    conn, content,
                    embed_fn=_embed_text_sync,
                    memory_type="observation",
                    stream="semantic",
                    topics=topics,
                    agent_id=self._agent_id,
                    session_id=None,
                )
                if not store_result.stored:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        mark_access(conn, [AccessEvent(
                            atom_id=store_result.atom_id, source="consolidation",
                        )])
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    continue

                observation_id = store_result.atom_id
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.executemany(
                        "INSERT INTO atom_relations "
                        "(source_id, target_id, relation_type, confidence, created_at) "
                        "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
                        [(observation_id, rid, now) for rid in evidence_ids],
                    )
                    conn.executemany(
                        "INSERT INTO atom_relations "
                        "(source_id, target_id, relation_type, confidence, created_at) "
                        "VALUES (?, ?, 'consolidated_into', 1.0, ?)",
                        [(rid, observation_id, now) for rid in evidence_ids],
                    )
                    mark_access(conn, [
                        AccessEvent(atom_id=rid, source="consolidation")
                        for rid in evidence_ids
                    ])

                    old_obs = find_superseded_observations(
                        conn, observation_id, set(evidence_ids),
                    )
                    for old_id in old_obs:
                        conn.execute(
                            "INSERT OR IGNORE INTO atom_relations "
                            "(source_id, target_id, relation_type, confidence, "
                            "created_at, metadata) "
                            "VALUES (?, ?, 'supersedes', 1.0, ?, ?)",
                            (observation_id, old_id, now,
                             json.dumps({"trigger": "consolidate"})),
                        )
                    conn.execute(
                        "INSERT INTO observations_metadata "
                        "(atom_id, evidence_count, trend, last_evidence_at, "
                        "consolidated_at) VALUES (?, ?, ?, ?, ?)",
                        (observation_id, len(evidence_ids),
                         "strengthening", now, now),
                    )
                    # P42: store any triples the LLM extracted. Source
                    # them to the new observation atom (not the raws)
                    # so triple retrieval surfaces the observation —
                    # the two-tier pathway then lifts the raws via the
                    # existing evidenced_by boost in recall.py.
                    if triples:
                        added = store_triples(
                            conn, triples,
                            source_atom_id=observation_id,
                            embed_fn=_embed_text_sync,
                        )
                        triples_stored += len(added)
                    # Contradiction edges: map LLM-emitted 1-based atom
                    # indices to atom IDs in the cluster and insert a
                    # 'contradicts' relation. The
                    # resolve_contradictions_to_supersedes() pass turns
                    # these into supersedes edges. Skip self-references
                    # and out-of-range indices.
                    if contradictions:
                        for c in contradictions:
                            ia = c.get("atom_index_a")
                            ib = c.get("atom_index_b")
                            if not isinstance(ia, int) or not isinstance(ib, int):
                                continue
                            if ia < 1 or ia > len(cluster):
                                continue
                            if ib < 1 or ib > len(cluster):
                                continue
                            aid_a = cluster[ia - 1]["id"]
                            aid_b = cluster[ib - 1]["id"]
                            if aid_a == aid_b:
                                continue
                            cursor = conn.execute(
                                "INSERT OR IGNORE INTO atom_relations "
                                "(source_id, target_id, relation_type, "
                                " confidence, created_at, metadata) "
                                "VALUES (?, ?, 'contradicts', 1.0, ?, ?)",
                                (aid_a, aid_b, now,
                                 json.dumps({
                                     "summary": c.get("summary", ""),
                                     "trigger": "consolidate",
                                 })),
                            )
                            if cursor.rowcount > 0:
                                contradicts_stored += 1
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

                # Trend recompute in its own short txn (refresh_trend
                # manages its own BEGIN/COMMIT).
                refresh_trend(conn, observation_id)

                # Incrementally add the new observation to the FAISS
                # index so the next query can surface it.
                if self._index is not None and self._index.built:
                    row = conn.execute(
                        "SELECT vec FROM embeddings WHERE atom_id = ?",
                        (observation_id,),
                    ).fetchone()
                    if row is not None and row[0] is not None:
                        self._index.add(observation_id, row[0])

                emitted.append(observation_id)
                for old_id in old_obs:
                    superseded.append((observation_id, old_id))
            # After all contradicts edges land, resolve them into
            # supersedes edges (newer-atom-wins strategy). One pass
            # at end of consolidate so the run sees a consistent view
            # of all contradictions discovered together.
            from .triples import resolve_contradictions_to_supersedes
            new_supersedes_from_contra = (
                resolve_contradictions_to_supersedes(conn) if contradicts_stored
                else 0
            )
            return (emitted, superseded, triples_stored,
                    contradicts_stored, new_supersedes_from_contra)

        emitted, superseded, n_triples, n_contra, n_supersedes_contra = (
            await asyncio.to_thread(_restructure)
        )
        return {
            "candidates_scanned": len(raws),
            "clusters_found": len(clusters),
            "clusters_consolidated": len(emitted),
            "observations_emitted": emitted,
            "observations_superseded": superseded,
            "observations_created": len(emitted),
            "triples_stored": n_triples,
            "contradicts_stored": n_contra,
            "supersedes_from_contradictions": n_supersedes_contra,
        }

    async def decay(self) -> dict[str, Any]:
        # No decay cron in the new design — activation is computed
        # on-demand, no state to transition. Return a no-op shape
        # that matches saga's response so call sites don't break.
        return {"transitions": {"faded": 0, "dormanted": 0}}

    async def forget(
        self, *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
    ) -> dict[str, Any]:
        # Map saga's criteria-based forget to forget_by_criteria.
        from .forget import forget_by_criteria
        def _do():
            conn = self._ensure_conn()
            result = forget_by_criteria(
                conn,
                min_age_days=grace_days,
                activation_below=confidence_floor,
                dry_run=dry_run,
            )
            return {
                "tombstoned_count": result.tombstoned_count,
                "preview_ids": result.tombstoned_ids if dry_run else [],
                "dry_run": dry_run,
            }
        return await asyncio.to_thread(_do)

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        def _do():
            conn = self._ensure_conn()
            return _recent_boundaries(
                conn, channel_id=channel_id, count=count,
            )
        return await asyncio.to_thread(_do)

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
        trend: str | None = None,
    ) -> list[dict[str, Any]]:
        """Count retrieval / feedback events per atom in the last N days.

        Filters:
        - ``channel_id``: join access_events.session_id → sessions.id
          and require ``sessions.channel_id = ?``. Lets per-channel
          callers (reflection / per-channel summaries) scope the
          ranking to one channel.
        - ``contributed_only``: count only ``feedback_positive`` events
          (i.e. the credit-pass endorsements), excluding plain
          retrievals. Matches saga's "contributed atoms" semantics.
        - ``trend``: join observations_metadata.trend = ?. Filters to
          observation-typed atoms with the given trend label
          (strengthening / stable / weakening / stale).
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def _do():
            conn = self._ensure_conn()
            if contributed_only:
                sources = ("feedback_positive",)
            else:
                sources = ("retrieval", "feedback_positive")
            placeholders = ",".join(["?"] * len(sources))

            joins = []
            where = [
                "a.tombstoned = 0",
                "a.agent_id = ?",
                "e.ts >= ?",
                f"e.source IN ({placeholders})",
            ]
            params: list = [self._agent_id, cutoff, *sources]

            if channel_id is not None:
                # access_events.session_id → sessions.id → channel_id.
                # LEFT JOIN so atoms with NULL session_id (consolidation-
                # synthesized observations etc.) don't get dropped by
                # an INNER JOIN when channel_id is unfiltered — but
                # WHERE clause ensures they ARE dropped when channel_id
                # is filtered, which is the right semantics.
                joins.append("JOIN sessions s ON s.id = e.session_id")
                where.append("s.channel_id = ?")
                params.append(channel_id)

            if trend is not None:
                joins.append("JOIN observations_metadata om ON om.atom_id = a.id")
                where.append("om.trend = ?")
                params.append(trend)

            join_sql = " ".join(joins)
            where_sql = " AND ".join(where)
            params.append(count)

            sql = (
                f"SELECT a.id, a.content, COUNT(e.id) AS n "
                f"FROM atoms a "
                f"JOIN access_events e ON e.atom_id = a.id "
                f"{join_sql} "
                f"WHERE {where_sql} "
                f"GROUP BY a.id ORDER BY n DESC LIMIT ?"
            )
            rows = conn.execute(sql, params).fetchall()
            return [
                {"id": r[0], "content": r[1], "retrieval_count": r[2]}
                for r in rows
            ]
        return await asyncio.to_thread(_do)

    async def mark_contributions(
        self,
        retrieved_atoms: list[dict[str, Any]],
        response_text: str,
        *,
        session_id: str | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """Credit-pass: identify which retrieved atoms contributed to a
        response and fire ``feedback_positive`` events on them. See
        ``mimir.memory.contributions.mark_contributions`` for the
        heuristic. Returns a dict the bench harness can log
        (``contribution_rate``, ``contributed_count``, ``total``).

        Opt-in by call site. The bench harness doesn't call this (saga's
        bench has it off too).
        """
        from .contributions import (
            mark_contributions as _mc, DEFAULT_CONTRIBUTION_THRESHOLD,
        )
        thr = threshold if threshold is not None else DEFAULT_CONTRIBUTION_THRESHOLD
        def _do():
            conn = self._ensure_conn()
            return _mc(
                conn, retrieved_atoms, response_text,
                session_id=session_id, threshold=thr,
            )
        result = await asyncio.to_thread(_do)
        return {
            "contributed_count": len(result.contributed_atom_ids),
            "total": len(retrieved_atoms),
            "contribution_rate": result.contribution_rate,
            "contributed": result.contributed_atom_ids,
            "threshold": result.threshold,
        }

    async def health(self) -> bool:
        try:
            conn = self._ensure_conn()
            conn.execute("SELECT 1 FROM atoms LIMIT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _candidate_to_atom(c) -> dict[str, Any]:
    """Map a RecallCandidate to saga's atom-response shape."""
    a = c.atom
    return {
        "id": a["id"],
        "content": a["content"],
        "stream": a.get("stream"),
        "memory_type": a.get("memory_type"),
        "source_type": a.get("source_type"),
        "_activation": c.activation,
        "_similarity": c.similarity,
        "_combined_score": c.total,
        "_trend": c.trend_label,
        # Both keys: saga's prompt renderer at sagatools.py:54 checks
        # ``confidence_tier`` first, then falls back to ``_confidence_tier``
        # (back-compat with an older saga shape). Setting both means
        # whichever consumer reads, it sees the tier.
        "confidence_tier": c.confidence_tier,
        "_confidence_tier": c.confidence_tier,
        "topics": _safe_json_load(a.get("topics")),
        "metadata": _safe_json_load(a.get("metadata")),
    }


def _safe_json_load(s):
    if not s:
        return [] if isinstance(s, str) else s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return s
