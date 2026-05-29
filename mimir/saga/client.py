"""SagaClient-compatible facade over the new memory subsystem.

Adapts ``mimir.saga.*`` operations to the ``SagaClient`` Protocol
(see ``mimir/saga_client.py``) so mimir's call sites — ``agent.py``,
``sagatools.py``, ``server.py`` — can flip from saga → memory atomically
with one wiring change (``make_saga_client(..)`` returns a
``SagaStore`` instead of an ``_InProcessSaga``).

Provider/index plumbing:

- Embedding provider: ``saga.embeddings.get_provider()`` — reused
  as-is. Configurable via saga.toml (voyage / openai / onnx).
- FAISS index: ``mimir.saga.vector_index.VectorIndex`` — owns its
  own index keyed on ``mimir.saga.db``'s embeddings table. Built
  lazily on first ``query()``; incrementally updated after each
  ``store()`` via ``on_atom_stored``.
- FTS5: ``mimir.saga.fts.fts_search`` — BM25 over the ``atoms_fts``
  virtual table. Triggers in schema.sql keep atoms_fts in sync with
  atoms; the client just calls the search.
- LLM synth for consolidate: ``mimir.saga.synthesize.
  make_async_rich_synth_fn`` — wraps saga's ``call_llm`` so
  consolidate() can actually emit observations rather than no-op'ing.

v2 is operationally complete: real FAISS over mimir.saga.db, real
FTS5, real LLM-backed consolidation. Embeddings still flow through
saga's provider — that stays until the final mimir/memory →
mimir/saga rename, at which point we move the provider too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from . import migrations as _migrations
from .mark_access import AccessEvent, mark_access
from .recall import recall as _recall
from .store import store as _store
from .reflect import (
    reflect as _reflect,
    recent_session_boundaries as _recent_boundaries,
)
from .forget import forget as _forget
from .fts import fts_search
from .vector_index import VectorIndex


# ─── Provider/index adapters ─────────────────────────────────────────


def _embed_text_sync(text: str) -> tuple[bytes, str, str, int]:
    """Adapt saga.embeddings.get_provider() to the store.EmbedFn shape.

    Returns (vec_bytes, provider_name, model, dim). Sync because the
    provider call is itself sync (network I/O is hidden inside).
    """
    from .embeddings import get_provider
    from ._config_io import get_config

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
    """Adapt for recall.QueryEmbedFn — returns float list (not bytes).

    Returns ``[]`` (which downstream callers treat as "no semantic
    pathway") when the embedding provider can't be loaded — e.g. tests
    without a configured provider, or operators without local ONNX
    model files. Matches saga.core.hybrid_retrieve's behavior of
    skipping the semantic pathway rather than crashing the turn.
    """
    try:
        from .embeddings import get_provider
        from ._config_io import get_config

        cfg = get_config()
        provider = get_provider()
        return provider.embed(text[:cfg("embedding", "max_input_chars", 2000)],
                              input_type="query")
    except Exception:
        return []


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


def _make_triple_search_fn(
    conn: sqlite3.Connection,
    *,
    dim: int | None,
    reference_date=None,
):
    """Closure over the connection matching recall.TripleSearchFn shape.
    Returns None when triples are disabled (the dim arg is None, meaning
    the FAISS index isn't built — same condition under which the
    semantic pathway would also be empty).

    ``reference_date`` is captured in the closure so expired triples
    (valid_until ≤ reference_date) are excluded from retrieval —
    consistent with the valid_until filter in top_triples_with_payload.
    """
    from .triples import triple_augment_search

    def _fn(query_emb: list[float], top_k: int) -> list[tuple[str, float]]:
        return triple_augment_search(
            conn, query_emb, top_k=top_k, dim=dim,
            reference_date=reference_date,
        )
    return _fn


# ─── The facade ──────────────────────────────────────────────────────


class SagaStore:
    """SagaClient-compatible facade. Holds a sqlite3 connection to
    mimir.saga.db and translates each saga-vocabulary method to the
    equivalent ``mimir.saga.*`` operation.

    Connection lifecycle: the client opens one connection per process
    on first use, applies the schema if the file is fresh, applies any
    pending migrations, and reuses that connection. Caller can also
    pass an open connection via ``conn=...`` for tests.

    All public methods are async to match SagaClient. CPU-bound work
    runs via ``asyncio.to_thread`` so mimir's event loop stays
    responsive during synthesis / consolidation passes.

    **Threading contract**: the shared sqlite3 connection is opened
    with ``check_same_thread=False`` to support ``asyncio.to_thread``
    dispatch from a single event loop. SQLite under WAL allows
    concurrent reads but serializes writes at the file level —
    Python's ``sqlite3`` module is not thread-safe by default, so
    write call sites that may race (consolidate cron firing while a
    turn is mid-store) must hold ``_write_lock``. Reads don't need
    the lock — WAL handles snapshot isolation. Production callers
    going through a single agent event loop already serialize through
    the asyncio scheduler; the lock is the belt-and-suspenders for
    cross-task / cross-coroutine writes.
    """

    # chainlink #242: schema migration registry + applier live in
    # ``mimir/saga/migrations.py``.  Class-level aliases are preserved
    # so ``monkeypatch.setattr(SagaStore, "CURRENT_SCHEMA_VERSION", N)``
    # / ``setattr(SagaStore, "MIGRATIONS", {...})`` in tests still
    # works — the wrapper methods below read these at call time so
    # patched values are honored.
    CURRENT_SCHEMA_VERSION: int = _migrations.CURRENT_SCHEMA_VERSION

    # Registry of post-greenfield schema changes. Keys are version
    # numbers (must be > 1, must be contiguous, must equal
    # ``CURRENT_SCHEMA_VERSION`` at the latest entry); values are raw
    # SQL scripts executed via ``conn.executescript``.
    MIGRATIONS: dict[int, str] = _migrations.MIGRATIONS

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
        # Dedicated index for sessions (search_sessions). Separate from the
        # atoms index because sessions store their own embeddings on the
        # sessions table rather than in the embeddings table.
        self._sessions_index: VectorIndex | None = None
        self._sessions_index_built: bool = False
        # LLM synth callable for consolidate(). Late-bound (lazy import
        # of synthesize.py) so SagaStore doesn't transitively pull in
        # the LLM transport at construction time. Despite the name, this
        # holds the *rich* synth callable (returns content + triples +
        # contradictions); ``observation`` is historical from when the
        # earlier tier-2 path was the only consumer. TODO(rename-pass):
        # ``_rich_synth_fn`` is the more accurate name.
        self._rich_synth_fn = None
        # Write-serialization across threads. We open the connection
        # with ``check_same_thread=False`` so each public method can
        # run under ``asyncio.to_thread``; SQLite under WAL serializes
        # writes at the file level, but the Python ``sqlite3`` module
        # isn't thread-safe by default. Wrap writes in this lock so
        # concurrent stores / consolidate passes / mark_access calls
        # can't interleave a transaction. Readers don't need the lock
        # — WAL handles snapshot isolation for them.
        import threading as _threading
        self._write_lock = _threading.Lock()

    async def _write_locked(self, fn):
        """Run a write-path callable in a worker thread, serialized via
        the connection write lock. Use for any method that mutates the
        DB. Reads should call ``asyncio.to_thread(fn)`` directly — they
        rely on WAL snapshot isolation and don't need serialization."""
        def _locked():
            with self._write_lock:
                return fn()
        return await asyncio.to_thread(_locked)

    def connection(self) -> sqlite3.Connection:
        """Public accessor for the underlying sqlite3 connection.

        Used by the upper layer (chainlink #266: skill-memory load
        injection) to run the skill-learning scoped recall, which lives
        in ``mimir.skill_memory`` and can't be reached from this lower
        layer (saga does not import up into ``mimir.*``). Reads under WAL
        are safe across the ``asyncio.to_thread`` worker the caller uses.
        """
        return self._ensure_conn()

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self._db_path is None:
            raise RuntimeError(
                "SagaStore: no db_path and no conn provided. "
                "Construct with SagaStore(db_path=Path(...)) or pass conn=..."
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self._db_path.exists()
        # Assign to a LOCAL variable first; only promote to ``self._conn``
        # after schema setup + pending migrations succeed. If we assign
        # ``self._conn`` first and the migration then raises, the next
        # ``_ensure_conn`` call returns the cached half-initialized
        # connection without retrying the migration — the failure is
        # silent and the migration never lands. (Hit on a v5 migration
        # bug: missing ``DELETE FROM atom_topics`` caused FK failures
        # that mimir kept working through with a stale schema.)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # chainlink #227: apply operator-configured busy_timeout so two
        # concurrent writers (agent's store() + a cron-driven consolidate or
        # forget in another thread/process) wait instead of raising
        # OperationalError immediately. WAL serializes at the OS level but
        # without busy_timeout the loser sees "database is locked" on the
        # first millisecond of contention.
        from ._config_io import get_config
        _cfg = get_config()
        _busy_timeout_ms = int(_cfg("storage", "db_busy_timeout_ms", 5000))
        conn.execute(f"PRAGMA busy_timeout = {_busy_timeout_ms}")
        try:
            if fresh:
                schema_path = Path(__file__).parent / "schema.sql"
                conn.executescript(schema_path.read_text())
                conn.commit()
            # Migration story: record the schema version we know how to
            # serve so future SagaStore builds can detect when an
            # existing DB needs migration. ``CURRENT_SCHEMA_VERSION``
            # bumps with every shipped schema change; ``MIGRATIONS`` is
            # the pending-migrations registry (empty until the first
            # post-greenfield change). Idempotent — re-runs only insert
            # if the row is missing.
            self._apply_pending_migrations(conn, fresh=fresh)
        except Exception:
            # Migration / schema apply failed: close the connection so
            # the next ``_ensure_conn`` reopens fresh and retries. We
            # close even on caller-side bugs (the next attempt re-raises
            # with the same error) — silent half-init was the worse
            # failure mode.
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._conn = conn
        return self._conn

    def _detect_schema_version(self, conn: sqlite3.Connection) -> int:
        """Delegates to :func:`mimir.saga.migrations.detect_schema_version`.
        Kept as a thin method so tests can still call
        ``store._detect_schema_version(conn)`` and monkeypatch it.
        """
        return _migrations.detect_schema_version(conn)

    def _apply_pending_migrations(
        self, conn: sqlite3.Connection, *, fresh: bool,
    ) -> None:
        """Delegates to :func:`mimir.saga.migrations.apply_pending_migrations`.

        Passes ``self.CURRENT_SCHEMA_VERSION`` / ``self.MIGRATIONS`` so
        test-side monkeypatching of those class attributes (see
        ``tests/test_saga_correctness_regressions.py``) continues to
        work.  Also calls through ``self._detect_schema_version`` so a
        test patching that method on the instance still hooks in — the
        ``apply_pending_migrations`` module function uses the
        ``self``-bound detector when supplied.
        """
        _migrations.apply_pending_migrations(
            conn,
            fresh=fresh,
            target_version=self.CURRENT_SCHEMA_VERSION,
            migrations=self.MIGRATIONS,
            detector=self._detect_schema_version,
        )

    def _ensure_index(self, conn: sqlite3.Connection) -> VectorIndex | None:
        """Lazily build the FAISS index on first retrieval. After build,
        store() incremental-adds keep it current; periodic rebuilds
        handle tombstoning accumulation.

        Dimension resolution order — mirrors ``_ensure_sessions_index``:

        1. Pre-set ``self._embedding_dim`` (constructor arg or cached
           from a prior call).
        2. First row in the ``embeddings`` table — authoritative once
           ANY embedding has been stored.
        3. The configured provider's reported ``dimensions()`` — the
           right value for an empty DB. Prevents the "fresh DB +
           non-Voyage provider" failure mode where the previous
           hardcoded ``1024`` default silently rejected every
           384-dim (fastembed) or 1536-dim (OpenAI
           text-embedding-3-small) vector that ever got stored.
        4. No provider available → return ``None`` so the search
           caller falls back to FTS-only. Better than building an
           index at a guessed dim that turns every future ``store()``
           write into a silent drop.
        """
        if self._index_built:
            return self._index
        dim = self._embedding_dim
        if dim is None:
            row = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()
            if row:
                dim = row[0]
            else:
                try:
                    from .embeddings import get_provider
                    dim = get_provider().dimensions()
                except Exception:
                    # Provider unavailable and DB is genuinely empty.
                    # Cache the miss and return None — search callers
                    # (``_make_faiss_search_fn``) already handle None by
                    # returning empty results, so this gracefully
                    # degrades to FTS-only retrieval rather than
                    # building an index at a guessed dim.
                    self._index_built = True
                    self._index = None
                    return None
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

    def _ensure_sessions_index(self, conn: sqlite3.Connection) -> VectorIndex | None:
        """Lazily build the sessions FAISS index from sessions.embedding.

        Separate from _ensure_index (atoms index) — sessions have their own
        embedding column; no join to the embeddings table needed.
        Invalidated by end_session() writes so the next search picks up new
        sessions.
        """
        if self._sessions_index_built:
            return self._sessions_index
        dim = self._embedding_dim
        if dim is None:
            row = conn.execute(
                "SELECT embedding_dim FROM sessions "
                "WHERE embedding_dim IS NOT NULL LIMIT 1"
            ).fetchone()
            if row:
                dim = row[0]
            else:
                # Fall back to atoms embedding dim; then ask the provider.
                # Never guess a magic constant — a wrong dim silently builds
                # an index that filters out every real embedding on arrival.
                row2 = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()
                if row2:
                    dim = row2[0]
                else:
                    try:
                        from .embeddings import get_provider
                        dim = get_provider().dimensions()
                    except Exception:
                        # Provider unavailable and DB is genuinely empty.
                        # Return None so search_sessions falls back to recency-only
                        # rather than building an index at the wrong dimension.
                        self._sessions_index_built = True  # cache the miss
                        self._sessions_index = None
                        return None
            self._embedding_dim = dim
        idx = VectorIndex(dimension=dim)
        idx.build_from_sessions(conn)
        self._sessions_index = idx
        self._sessions_index_built = True
        return idx

    # ── SagaClient surface ──────────────────────────────────────────

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
        reference_date=None,
        enable_contextual_rewrite: bool | None = None,
        pre_rewritten_query: str | None = None,
    ) -> dict[str, Any]:
        # Three paths into the rewrite:
        # 1. Caller pre-resolved the rewrite via ``contextual_rewrite()``
        #    and passes ``pre_rewritten_query`` — we skip the inline
        #    LLM call. This is the parallelization seam: the agent
        #    runs ``contextual_rewrite`` once, then fans out
        #    ``query(pre_rewritten_query=...)`` and the file_search
        #    autopass against the same expanded query via
        #    ``asyncio.gather`` (chainlink-spec #142, PR 166 followup).
        # 2. Inline: caller passes ``enable_contextual_rewrite=True``
        #    + ``context``, we call the LLM here. Preserves the
        #    single-call ergonomics for callers that don't need
        #    parallelism.
        # 3. Default (kwarg is ``None``): consult saga.toml's
        #    ``[retrieval] enable_contextual_rewrite`` flag. Lets the
        #    agent pass ``context=`` without having to thread the
        #    toml flag through every call site. Bench / test code can
        #    force-off by passing ``enable_contextual_rewrite=False``.
        if enable_contextual_rewrite is None:
            from ._config_io import get_config
            enable_contextual_rewrite = bool(
                get_config()("retrieval", "enable_contextual_rewrite", False)
            )
        # Surface the precedence ambiguity so a future call site that
        # sets both kwargs gets a log line — the pre-resolved path
        # silently wins, which could otherwise hide a misconfiguration.
        if pre_rewritten_query is not None and enable_contextual_rewrite:
            log.warning(
                "SagaStore.query: both pre_rewritten_query and "
                "enable_contextual_rewrite=True supplied; "
                "pre_rewritten_query wins (inline rewrite skipped). "
                "Pick one — pre-resolved for parallelism, inline for "
                "single-call ergonomics."
            )
        rewritten_query: str | None = pre_rewritten_query
        if rewritten_query is None and enable_contextual_rewrite and context:
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
            # Compute the query embedding ONCE per query() call —
            # both _recall and top_triples_with_payload need it, and
            # the underlying provider call (~50-300ms on voyage) is
            # the heaviest non-LLM step. Cache it locally and feed
            # both consumers from the same value.
            query_emb = _query_embed_sync(effective_query)
            result = _recall(
                conn, effective_query,
                query_embed_fn=lambda _q: query_emb,
                faiss_search_fn=_make_faiss_search_fn(index),
                fts_search_fn=_make_fts_search_fn(
                    conn, agent_id=self._agent_id,
                    synonyms=self._synonyms,
                ),
                triple_search_fn=_make_triple_search_fn(
                    conn, dim=triple_dim, reference_date=reference_date,
                ),
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
                    conn, query_emb,
                    top_n=self._triples_top_n, dim=triple_dim,
                    reference_date=reference_date,
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

    async def contextual_rewrite(
        self,
        query: str,
        context: list[dict[str, str]] | None,
    ) -> str | None:
        """Pre-resolve saga's contextual rewrite as a standalone step.

        Returns the rewritten query string when the rewrite ran and
        actually changed the input. Returns ``None`` when:
          - context is empty / None
          - the rewrite LLM call failed / returned empty
          - the LLM returned the input unchanged (no-op)

        Exposes the rewrite as a separate API so callers that have
        multiple retrieval surfaces (saga atoms + file_search) can
        run them in parallel against the same expanded query via
        ``asyncio.gather`` — see chainlink-spec #142 (PR 166 followup).
        Pass the returned string back into ``query(pre_rewritten_query=...)``
        to skip the inline rewrite there.

        No-op cost when context is None — does not hit the LLM."""
        if not context:
            return None
        try:
            from .query_rewrite import rewrite_query
            rewritten = await rewrite_query(query, context)
        except Exception:
            return None
        if not rewritten or rewritten == query:
            return None
        return rewritten

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
        return await self._write_locked(_do)

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
        return await self._write_locked(_do)

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
            return await self._write_locked(_do)
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
                # No observation synth at session-end — that lives in
                # consolidate() (cron-driven, cross-session). reflect's
                # within-session synth hook was removed 2026-05-13.
            )
            # Invalidate sessions index so the next search_sessions() call
            # picks up the newly-written session and its embedding.
            self._sessions_index_built = False
            return {
                "session_id": session_id,
                "channel": channel_id,
                "session_summary_written": result.session_summary_written,
            }
        return await self._write_locked(_do)

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
        lookback_days: int = 30,
        min_cluster_size: int = 3,
        dedup_first: bool = True,
        dedup_threshold: float | None = None,
        dedup_max_clusters: int | None = None,
        reference_date=None,
    ) -> dict[str, Any]:
        """Two-pass cross-session consolidation.

        Pass 1 (dedup): if ``dedup_first`` (default True), runs a
        tight-threshold near-duplicate collapse — picks one canonical
        per cluster by ACT-R activation, folds the rest's access history
        and relations into it, and tombstones with reason='merged'. No
        LLM cost.

        ``dedup_threshold`` defaults to **0.92 floor for all providers**
        (``max(_PROVIDER_AUTO_THRESHOLDS[provider], 0.92)``) — the
        per-corpus calibration on mimir's saga.db showed the OpenAI
        and Voyage pair-similarity distributions both place 0.92 at
        the ~99.98th percentile, where mean-cosine merges are
        template-level similar. The floor protects providers whose
        thematic threshold sits at 0.80 (openai / nim) from over-merging
        substantively-distinct atoms during the dedup pass. Caller
        override always wins.

        ``dedup_max_clusters`` caps the number of clusters processed,
        NOT the number of atoms tombstoned — a cluster of 5
        near-duplicates counts as one against the cap but tombstones
        four. Set this to bound LLM-free runtime on cold-start runs;
        leave None for unbounded.

        Pass 2 (thematic): runs the LLM-backed observation synthesizer
        over the (now-deduped) recent raw atoms. Same contract as
        before — emits one observation per cluster surviving
        supersession/equal-evidence checks.

        ``dry_run=True`` walks the candidate set for both passes and
        reports counts without paying any LLM cost or doing any writes.
        Useful for the bench harness's pre-flight check.
        """
        from .cluster import make_default_cluster_fn

        # Build/look up the cached LLM synth_fn. The rich variant
        # returns observation + triples + contradictions in one call;
        # the per-cluster restructure pass then routes each output
        # into the right table. Cached on the client because
        # make_async_rich_synth_fn closes over llm_config (resolved
        # once per process is fine).
        if self._rich_synth_fn is None:
            from .synthesize import make_async_rich_synth_fn
            self._rich_synth_fn = make_async_rich_synth_fn()

        # Synth is async, but consolidate() runs synchronously under
        # to_thread (so transactions stay in one thread). We adapt: a
        # sync wrapper that re-enters the running loop is unsafe
        # (asyncio.run inside an executor thread would deadlock against
        # the parent loop). Instead, we resolve clusters here on the
        # caller's loop, run the LLM calls concurrently, and pass a
        # pre-computed lookup into a sync consolidate variant.
        conn = self._ensure_conn()

        # 0. Pass 1 (dedup): tighter clusterer collapses near-duplicate
        # raws into one canonical each, tombstoning the rest. Reads ACT-R
        # activation to pick the canonical so we keep the retrieval-
        # validated copy. Empty result if dedup_first is False.
        dedup_payload: dict[str, Any] = {
            "candidates_scanned": 0,
            "clusters_formed": 0,
            "canonicals_kept": [],
            "duplicates_tombstoned": [],
            "threshold": None,
        }
        if dedup_first:
            from .dedup import dedup_pass, DEFAULT_DEDUP_THRESHOLD
            from .embeddings import resolve_auto_threshold
            from ._config_io import get_config
            cfg = get_config()
            provider_name = cfg("embedding", "provider", "unknown")
            # 0.92 floor for all providers. The per-provider thematic
            # threshold (0.80 openai / 0.80 nim / 0.92 voyage / 0.92 onnx)
            # is too loose for the dedup pass on openai/nim — calibration
            # against mimir's saga.db (693 atoms, voyage + openai-3-large)
            # showed 0.92 sits at the ~99.98th percentile of pair similarity
            # for both providers. The max() acts as a floor that overrides
            # any provider whose thematic threshold is below 0.92. Caller
            # override always wins.
            effective_dedup_threshold = (
                dedup_threshold
                if dedup_threshold is not None
                else max(
                    resolve_auto_threshold(provider_name),
                    DEFAULT_DEDUP_THRESHOLD,
                )
            )
            dedup_cluster_fn = make_default_cluster_fn(
                conn, threshold=effective_dedup_threshold,
            )
            def _do_dedup():
                return dedup_pass(
                    conn,
                    cluster_fn=dedup_cluster_fn,
                    agent_id=self._agent_id,
                    lookback_days=lookback_days,
                    min_cluster_size=2,
                    dry_run=dry_run,
                    max_clusters=dedup_max_clusters,
                    reference_date=reference_date,
                )
            dedup_result = await self._write_locked(_do_dedup)
            dedup_payload = {
                "candidates_scanned": dedup_result.candidates_scanned,
                "clusters_formed": dedup_result.clusters_formed,
                "canonicals_kept": dedup_result.canonicals_kept,
                "duplicates_tombstoned": dedup_result.duplicates_tombstoned,
                "threshold": effective_dedup_threshold,
            }

        # 1. Candidate selection + clustering (sync; reads only).
        # Re-fetches raws so the tombstoned duplicates from pass 1
        # don't appear as candidates for thematic clustering.
        from .consolidate import (
            _candidate_raws, MIN_CLUSTER_SIZE_FOR_OBSERVATION,
            MAX_OBSERVATIONS_PER_RUN,
        )
        raws = await asyncio.to_thread(
            _candidate_raws,
            conn,
            lookback_days=lookback_days,
            agent_id=self._agent_id,
            reference_date=reference_date,
        )
        if len(raws) < min_cluster_size:
            return {
                "clusters_formed": 0,
                "observations_emitted": [],
                "dedup": dedup_payload,
            }

        cluster_fn = make_default_cluster_fn(conn)
        clusters = await asyncio.to_thread(cluster_fn, raws)

        if dry_run:
            return {
                "dry_run": True,
                "candidates_scanned": len(raws),
                "clusters_found": len(clusters),
                "total_atoms_in_clusters": sum(len(c) for c in clusters),
                "dedup": dedup_payload,
            }

        # 2. LLM synthesis fan-out — concurrent calls, bounded by a
        # semaphore so we don't blow the provider's rate limits.
        # Reuses saga's call_llm transport (anthropic/openai_compat
        # plumbing already lives there).
        max_obs = max_clusters or MAX_OBSERVATIONS_PER_RUN
        eligible_unbounded = [c for c in clusters if len(c) >= min_cluster_size]
        eligible = eligible_unbounded[:max_obs]
        if len(eligible_unbounded) > max_obs:
            log.info(
                "consolidate: max_clusters cap (%d) bound — %d cluster(s) "
                "skipped this run; rerun with a higher max_clusters to "
                "catch the remainder.",
                max_obs, len(eligible_unbounded) - max_obs,
            )
        sem = asyncio.Semaphore(4)

        # P47 / P48: build vocab_block once per run, prior_block per
        # cluster. Both inject into the rich prompt. Empty when DB is
        # cold or there are no priors — bench-neutral.
        from .synthesize import build_vocab_block, build_prior_block
        vocab_block = await asyncio.to_thread(
            build_vocab_block, conn,
            extra_subjects=list(extra_canonical_subjects or []),
        )
        prior_blocks: list[str] = []
        for cluster in eligible:
            evidence_ids = [a["id"] for a in cluster]
            pb = await asyncio.to_thread(build_prior_block, conn, evidence_ids)
            prior_blocks.append(pb)

        async def _synth(cluster, prior_block):
            async with sem:
                try:
                    return await self._rich_synth_fn(
                        cluster,
                        prior_block=prior_block,
                        vocab_block=vocab_block,
                    )
                except Exception:
                    return {"content": "", "topics": [],
                            "triples": [], "contradictions": []}

        results = await asyncio.gather(
            *[_synth(c, pb) for c, pb in zip(eligible, prior_blocks)]
        )

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
                    # Don't fire an access_event: consolidation is
                    # system-internal, not external access. The
                    # ``consolidated_into`` / ``evidenced_by`` relations
                    # provide the persistent audit trail; activation is
                    # for external-access record only.
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
                    # Dedupe hit on the observation content — relations
                    # were already in place from the prior cluster pass.
                    # See note above: no consolidation access_event.
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
                    # No mark_access on evidence raws: consolidation is
                    # system-internal. The evidence_boost on retrieval
                    # provides the ranking signal; access_events stays
                    # a pure external-access record.

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

        # _restructure mutates atoms/observations/triples — write lock
        # serializes it against any concurrent agent-loop store / feedback.
        emitted, superseded, n_triples, n_contra, n_supersedes_contra = (
            await self._write_locked(_restructure)
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
            "dedup": dedup_payload,
        }

    async def consolidate_skill_memories(
        self, *, dry_run: bool = False,
        lookback_days: int | None = None,
        min_cluster_size: int = 2,
        dedup_threshold: float | None = None,
        dedup_max_clusters: int | None = None,
    ) -> dict[str, Any]:
        """Per-skill dedup pass for skill-learning atoms (#266).

        The general :meth:`consolidate` EXCLUDES ``skill_learning`` atoms;
        this runs a SEPARATE dedup pass scoped to each skill so a skill's
        near-duplicate learnings collapse against each other only — never
        across skills, never against a general raw. Run this alongside the
        general pass (the scheduler's ``saga-consolidate`` job does both).

        Thematic (LLM-synthesized observation) consolidation is
        intentionally NOT applied to skill learnings: they are already
        curated, kind-tagged notes authored with intent, not raw evidence
        to summarize into a cross-session observation. Collapsing exact/
        near-duplicate notes is the only safe automatic operation; merging
        a ``failure-mode`` and a ``tip`` into one synthesized observation
        would destroy the valence the surfacing layer (#267) depends on.

        ``lookback_days=None`` (default) dedups a skill's learnings across
        all time — skill atoms are sparse and long-lived, so an age window
        would leave old duplicates uncollapsed. ``dedup_threshold`` /
        ``dedup_max_clusters`` mirror :meth:`consolidate`'s dedup pass.

        Returns ``{"skills_scanned", "threshold", "skills": {name: {...}}}``
        with one per-skill dedup summary each.
        """
        from .consolidate import distinct_skill_scopes
        from .cluster import make_default_cluster_fn
        from .dedup import dedup_pass, DEFAULT_DEDUP_THRESHOLD
        from .embeddings import resolve_auto_threshold
        from ._config_io import get_config

        conn = self._ensure_conn()
        skills = await asyncio.to_thread(
            distinct_skill_scopes, conn, agent_id=self._agent_id,
        )
        summary: dict[str, Any] = {
            "skills_scanned": len(skills),
            "threshold": None,
            "skills": {},
        }
        if not skills:
            return summary

        # Same 0.92-floor threshold resolution as consolidate()'s dedup
        # pass — the per-provider thematic threshold is too loose for
        # near-duplicate collapse. Caller override wins.
        cfg = get_config()
        provider_name = cfg("embedding", "provider", "unknown")
        effective_threshold = (
            dedup_threshold
            if dedup_threshold is not None
            else max(
                resolve_auto_threshold(provider_name),
                DEFAULT_DEDUP_THRESHOLD,
            )
        )
        summary["threshold"] = effective_threshold
        # One clusterer for all skills — candidate selection inside
        # dedup_pass (skill_scope) restricts each call to one skill's
        # atoms, so a shared cluster_fn never crosses skills.
        cluster_fn = make_default_cluster_fn(
            conn, threshold=effective_threshold,
        )

        for skill in skills:
            def _do_dedup(skill=skill):
                return dedup_pass(
                    conn,
                    cluster_fn=cluster_fn,
                    agent_id=self._agent_id,
                    lookback_days=lookback_days,
                    min_cluster_size=min_cluster_size,
                    dry_run=dry_run,
                    max_clusters=dedup_max_clusters,
                    skill_scope=skill,
                )
            res = await self._write_locked(_do_dedup)
            summary["skills"][skill] = {
                "candidates_scanned": res.candidates_scanned,
                "clusters_formed": res.clusters_formed,
                "canonicals_kept": res.canonicals_kept,
                "duplicates_tombstoned": res.duplicates_tombstoned,
            }
        return summary

    async def forget(
        self, *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
    ) -> dict[str, Any]:
        # Map saga's criteria-based forget to forget_by_criteria. Also
        # synchronizes the in-memory FAISS index — ``forget_by_criteria``
        # tombstones the SQLite rows but doesn't know about the index,
        # so without explicit removal here the index accumulates
        # orphaned positions: retrieval still works (the SQL-side
        # ``WHERE tombstoned = 0`` filter in ``recall.py`` masks them
        # out), but index fragmentation grows until
        # ``VectorIndex.rebuild_if_needed`` (>10% removed) kicks in.
        # That's a long time on low-churn deployments — meanwhile
        # over-fetches climb and FAISS top_k starts missing the real
        # top results past the removal noise.
        #
        # PR #342 fixed the silent-drop on ``agent_id`` +
        # ``min_retrievals``. ``contribution_threshold`` +
        # ``contradiction_threshold`` are accepted at the in-process
        # surface (so callsites that pass them through to either
        # provider don't break) but the underlying
        # ``forget_by_criteria`` doesn't implement them yet (HTTP
        # path forwards them server-side). Surface that gap as a
        # log warning instead of the original silent drop — same
        # shape as the bug PR #342 was fixing, applied to the two
        # remaining params.
        if contribution_threshold is not None:
            log.warning(
                "saga.SagaStore.forget: contribution_threshold=%r ignored "
                "in the in-process path (not yet implemented in "
                "forget_by_criteria). HTTP path forwards the param "
                "server-side. See mimir-repo follow-up to PR #342.",
                contribution_threshold,
            )
        if contradiction_threshold is not None:
            log.warning(
                "saga.SagaStore.forget: contradiction_threshold=%r ignored "
                "in the in-process path (not yet implemented in "
                "forget_by_criteria). HTTP path forwards the param "
                "server-side. See mimir-repo follow-up to PR #342.",
                contradiction_threshold,
            )
        from .forget import forget_by_criteria
        def _do():
            conn = self._ensure_conn()
            result = forget_by_criteria(
                conn,
                agent_id=self._agent_id,
                min_age_days=grace_days,
                min_retrievals=min_retrievals,
                activation_below=confidence_floor,
                dry_run=dry_run,
            )
            # Remove tombstoned atoms from the FAISS index (not a dry-run,
            # and the index has been built — otherwise there's nothing
            # to remove). Failures are logged but non-fatal: the SQL
            # filter still masks tombstones at retrieval time, so a
            # missed index-side removal degrades gracefully.
            if (
                not result.dry_run
                and result.tombstoned_ids
                and self._index is not None
                and self._index.built
            ):
                for atom_id in result.tombstoned_ids:
                    try:
                        self._index.remove(atom_id)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "FAISS index remove failed for atom_id=%r",
                            atom_id, exc_info=True,
                        )
            return {
                "tombstoned_count": result.tombstoned_count,
                "preview_ids": result.tombstoned_ids if dry_run else [],
                "dry_run": dry_run,
            }
        return await self._write_locked(_do)

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        def _do():
            conn = self._ensure_conn()
            return _recent_boundaries(
                conn, channel_id=channel_id, count=count,
            )
        return await asyncio.to_thread(_do)

    async def search_sessions(
        self,
        query: str,
        *,
        channel_id: str | None = None,
        alpha: float = 0.7,
        limit: int = 10,
    ) -> list[dict]:
        """Return sessions relevant to *query*, ranked by semantic + recency blend.

        Score = alpha * cosine_similarity + (1 - alpha) * recency_score

        Recency uses a 30-day exponential half-life:
            recency_score = exp(-ln(2) / 30 * age_days)

        Queries ``sessions`` directly — no atoms join, no source_type filter.
        Sessions without an embedding receive similarity_score=0.0 and are
        ranked by recency only (still returned when alpha < 1.0).

        Two semantic paths:
        1. Sessions FAISS index (``_ensure_sessions_index``), built lazily.
        2. Python-side cosine over ``sessions.embedding`` when FAISS is
           unavailable or the index is empty.

        Args:
            query: Natural-language search query.
            channel_id: Restrict results to a single channel.
            alpha: Semantic weight. 0.0 = recency-only, 1.0 = semantic-only.
            limit: Maximum sessions to return.

        Returns:
            List of dicts with keys:
                session_id, channel_id, started_at, ended_at, summary,
                similarity_score, recency_score, blended_score
            Sorted descending by blended_score.
        """
        import math

        # Skip the embed round-trip when alpha=0 (pure recency — cosine score
        # is never consulted).  The downstream _do() handles query_emb==[] via
        # the existing ``if query_emb:`` guard, so the recency path still works.
        if alpha > 0.0:
            query_emb: list[float] = await asyncio.to_thread(_query_embed_sync, query)
        else:
            query_emb = []

        def _do() -> list[dict]:
            conn = self._ensure_conn()

            # ── Step 1: build similarity map from sessions FAISS index ──
            sim_map: dict[str, float] = {}  # session_id → cosine similarity

            if query_emb:
                index = self._ensure_sessions_index(conn)
                if index is not None:
                    for sess_id, score in index.search(
                        query_emb, top_k=min(limit * 4, 200)
                    ):
                        sim_map[sess_id] = float(score)

                if not sim_map:
                    # Python cosine fallback (FAISS unavailable or empty).
                    import struct as _struct
                    q_norm = math.sqrt(sum(x * x for x in query_emb)) + 1e-9
                    dim = len(query_emb)
                    for (sess_id, emb_blob) in conn.execute(
                        "SELECT id, embedding FROM sessions WHERE embedding IS NOT NULL"
                    ).fetchall():
                        if not emb_blob:
                            continue
                        try:
                            e_arr = _struct.unpack(f"{dim}f", emb_blob[: dim * 4])
                            dot = sum(a * b for a, b in zip(query_emb, e_arr))
                            e_norm = math.sqrt(sum(x * x for x in e_arr)) + 1e-9
                            sim_map[sess_id] = dot / (q_norm * e_norm)
                        except Exception:
                            continue

            # ── Step 2: fetch sessions rows ──
            channel_clause = "WHERE channel_id = ?" if channel_id else ""
            params: list = [channel_id] if channel_id else []

            rows = conn.execute(
                f"""
                SELECT id, channel_id, started_at, ended_at, summary, reflected_at
                FROM sessions
                {channel_clause}
                ORDER BY COALESCE(ended_at, reflected_at) DESC
                LIMIT 500
                """,
                params,
            ).fetchall()

            # ── Step 3: score each session ──
            now_ts = datetime.now(tz=timezone.utc).timestamp()
            results: list[dict] = []
            for (sess_id, ch_id, started_at, ended_at, summary, reflected_at) in rows:
                sim = sim_map.get(sess_id, 0.0)

                # Recency reference: ended_at, falling back to reflected_at —
                # mirrors the SQL ``ORDER BY COALESCE(ended_at, reflected_at)``.
                # chainlink #253: previously this used ``ended_at`` only and,
                # on a NULL/empty value, ``fromisoformat("")`` raised → except
                # set ref_ts = now → recency 1.0 (scored NEWEST). That inverted
                # the ranking for any session row with a NULL ended_at (e.g.
                # the migration-2 backfill path) — a never-properly-ended
                # session out-ranked genuinely recent ones. Now: consult
                # reflected_at too, and when there's NO usable timestamp at
                # all, score recency 0.0 (rank LAST), matching SQLite's
                # NULLS-LAST ordering for the same COALESCE.
                ref_str = ended_at or reflected_at or ""
                ref_ts: float | None
                try:
                    if ref_str.endswith("Z"):
                        ref_str = ref_str[:-1] + "+00:00"
                    ref_ts = datetime.fromisoformat(ref_str).timestamp()
                except (ValueError, AttributeError):
                    ref_ts = None
                if ref_ts is None:
                    recency = 0.0
                else:
                    age_days = max(0.0, (now_ts - ref_ts) / 86400.0)
                    recency = math.exp(-math.log(2) / 30.0 * age_days)

                blended = alpha * sim + (1.0 - alpha) * recency
                results.append({
                    "session_id": sess_id,
                    "channel_id": ch_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "summary": summary or "",
                    "similarity_score": round(sim, 6),
                    "recency_score": round(recency, 6),
                    "blended_score": round(blended, 6),
                })

            results.sort(key=lambda r: r["blended_score"], reverse=True)
            return results[:limit]

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
        ``mimir.saga.contributions.mark_contributions`` for the
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
        result = await self._write_locked(_do)
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
        except Exception as exc:
            log.warning("SagaStore.health check failed: %s", exc)
            return False

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                # Leaked file descriptor is worth knowing about even
                # though we don't propagate the error (close() is
                # called from shutdown / cleanup paths that shouldn't
                # block on a misbehaving connection).
                log.warning("SagaStore.close failed: %s", exc)
            self._conn = None

    async def __aenter__(self) -> "SagaStore":
        """Async context manager entry — opens the SQLite connection
        eagerly so ``async with SagaStore(...) as store:`` blocks fail
        fast on bad db paths instead of waiting for the first method
        call. Test ergonomics fix: tests that need a SagaStore for a
        single fixture lifetime can write::

            async with SagaStore(db_path=tmp_path / "t.db") as store:
                ...

        instead of remembering to call ``await store.close()`` in
        their teardown.
        """
        self._ensure_conn()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Async context manager exit — closes the connection. Mirrors
        ``close()`` and absorbs the same exceptions. Does NOT suppress
        caller exceptions; returns ``None`` so any raise in the ``with``
        body propagates."""
        await self.close()


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
