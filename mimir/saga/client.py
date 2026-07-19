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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import migrations as _migrations
from .mark_access import AccessEvent, mark_access
from .ownership import RESERVED_SENTINEL_PRINCIPALS
from .recall import recall as _recall
from .store import _hash_content as _store_hash_content
from .store import store as _store
from .reflect import (
    reflect as _reflect,
    recent_session_boundaries as _recent_boundaries,
)
from .fts import fts_search
from .vector_index import VectorIndex

log = logging.getLogger(__name__)


_ALL_SAGA_ATOMS = object()


@dataclass(frozen=True)
class _ServiceMutationScope:
    owner_principal: str
    readable_domains: tuple[str, ...]


def _saga_mutation_scope(
    auth_context: Any, operation: str
) -> object | str | _ServiceMutationScope | None:
    """Return the atom-row scope for a destructive SAGA operation."""
    from ..access_control import get_trusted_service_from_auth_context
    from ..models import AuthContext

    # Mutation authority must come from the frozen server carrier. Read-side
    # AuthorizationScope values and arbitrary duck-typed objects are publicly
    # constructible and therefore cannot prove destructive authority.
    if not isinstance(auth_context, AuthContext):
        return None
    if "admin" in auth_context.roles:
        return _ALL_SAGA_ATOMS
    service = get_trusted_service_from_auth_context(auth_context)
    if service is not None:
        if not service.has_capability(operation):
            return None
        domains = tuple(d for d in service.readable_domains if isinstance(d, str))
        return _ServiceMutationScope(
            owner_principal=f"service:{service.canonical}",
            readable_domains=domains,
        )
    principal = getattr(auth_context, "canonical_principal", None)
    if isinstance(principal, str) and principal:
        if principal not in RESERVED_SENTINEL_PRINCIPALS:
            return principal
    return None


def _authorized_atom_ids(
    conn: sqlite3.Connection,
    atom_ids: list[str],
    scope: object | str | _ServiceMutationScope | None,
) -> list[str] | None:
    """Return all requested IDs only when the entire set is authorized."""
    requested = list(dict.fromkeys(atom_ids))
    if not requested or scope is None:
        return [] if not requested else None
    placeholders = ",".join(["?"] * len(requested))
    sql = f"SELECT id FROM atoms WHERE id IN ({placeholders})"
    params: list[Any] = list(requested)
    if isinstance(scope, str):
        sql += " AND owner_principal = ?"
        params.append(scope)
    elif isinstance(scope, _ServiceMutationScope):
        grants = ["owner_principal = ?"]
        params.append(scope.owner_principal)
        if scope.readable_domains:
            domain_placeholders = ",".join(["?"] * len(scope.readable_domains))
            grants.append(f"origin_domain IN ({domain_placeholders})")
            params.extend(scope.readable_domains)
        sql += f" AND ({' OR '.join(grants)})"
    found = {row[0] for row in conn.execute(sql, params).fetchall()}
    return requested if found == set(requested) else None


# ─── Provider/index adapters ─────────────────────────────────────────


EmbeddingTuple = tuple[bytes, str, str, int]


def _embed_text_sync(text: str) -> EmbeddingTuple:
    """Adapt saga.embeddings.get_provider() to the store.EmbedFn shape.

    Returns (vec_bytes, provider_name, model, dim). Sync because the
    provider call is itself sync (network I/O is hidden inside).
    """
    from .embeddings import get_provider
    from ._config_io import get_config

    cfg = get_config()
    provider = get_provider()
    vec = provider.embed(
        text[: cfg("embedding", "max_input_chars", 2000)], input_type="passage"
    )
    # #493: read provenance off the LIVE provider, not config. The #681 keyless
    # fallback can swap a configured voyage provider for ONNX (BGE 384); config
    # would stamp those rows provider=voyage / model=voyage-4-lite over BGE
    # vectors, misleading any future re-embed-on-provider-change or analytics.
    provider_name = getattr(provider, "provider_name", None) or cfg(
        "embedding", "provider", "unknown"
    )
    model = getattr(provider, "model_id", None) or cfg("embedding", "model", "unknown")
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
        return provider.embed(
            text[: cfg("embedding", "max_input_chars", 2000)], input_type="query"
        )
    except Exception:
        return []


def _make_faiss_search_fn(
    index: VectorIndex | None,
    conn: sqlite3.Connection | None = None,
    *,
    auth_scope=None,
    agent_id: str = "default",
):
    """Return an authorization-scoped VectorIndex search adapter.

    FAISS cannot express the SQL ownership predicate itself, so the adapter
    materializes the caller's authorized live-id set before search and removes
    unauthorized IDs before they reach RRF.  It over-fetches to the index size
    so authorized results are not truncated merely because hidden vectors rank
    above them.
    """

    def _fn(query_emb: list[float], top_k: int) -> list[tuple[str, float]]:
        if index is None:
            return []
        if conn is None or auth_scope is None:
            return index.search(query_emb, top_k=top_k)

        from .ownership import authorization_predicate

        auth_where, auth_params = authorization_predicate(auth_scope, table="a")
        allowed = {
            row[0]
            for row in conn.execute(
                f"SELECT a.id FROM atoms a WHERE a.tombstoned = 0 "
                f"AND a.agent_id IN (?, 'shared') AND {auth_where}",
                [agent_id] + auth_params,
            ).fetchall()
        }
        if not allowed:
            return []
        search_k = max(top_k, index.total_vectors)
        return [
            (atom_id, similarity)
            for atom_id, similarity in index.search(query_emb, top_k=search_k)
            if atom_id in allowed
        ][:top_k]

    return _fn


def _make_fts_search_fn(
    conn: sqlite3.Connection,
    *,
    agent_id: str = "default",
    synonyms: dict[str, list[str]] | None = None,
    auth_scope=None,
):
    """Closure over the connection matching recall.FtsSearchFn shape.
    ``synonyms`` is the P12 query-expansion dict (FTS-only pathway)."""

    def _fn(query: str, top_k: int) -> list[tuple[str, float]]:
        return fts_search(
            conn,
            query,
            top_k=top_k,
            agent_id=agent_id,
            synonyms=synonyms,
            auth_scope=auth_scope,
        )

    return _fn


def _make_triple_search_fn(
    conn: sqlite3.Connection,
    *,
    dim: int | None,
    reference_date=None,
    auth_context: Any = None,
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
            conn,
            query_emb,
            top_k=top_k,
            dim=dim,
            reference_date=reference_date,
            auth_context=auth_context,
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
    # SQL scripts executed inside per-migration transactions.
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
        # Threading contract (chainlink #365): do NOT run concurrent work
        # against one ``sqlite3.Connection`` object. Python's sqlite3 wrapper
        # plus FTS5 can segfault even for concurrent reads when a single
        # ``check_same_thread=False`` connection is shared across worker
        # threads. SQLite/WAL permits concurrency across *connections*, so
        # read-heavy public methods open a short-lived per-call connection
        # when ``db_path`` is available. The shared connection remains the
        # canonical write/migration connection and is protected by locks.
        import threading as _threading

        self._write_lock = _threading.Lock()
        self._db_lock = _threading.RLock()
        self._index_lock = _threading.RLock()
        self._sessions_index_lock = _threading.RLock()

    def _configure_connection(
        self,
        conn: sqlite3.Connection,
        *,
        enable_wal: bool = True,
    ) -> None:
        """Apply runtime pragmas to SagaStore sqlite connections.

        ``journal_mode=WAL`` is a writer-ish pragma; set it on the canonical
        shared connection during initialization, then let per-call read
        connections inherit the file mode instead of all racing to restate it.
        """
        if enable_wal:
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

    def _connect_db_path(self, *, enable_wal: bool = True) -> sqlite3.Connection:
        if self._db_path is None:
            raise RuntimeError("SagaStore: cannot open path connection without db_path")
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._configure_connection(conn, enable_wal=enable_wal)
        return conn

    def _operation_conn(self) -> tuple[sqlite3.Connection, bool]:
        """Return a connection for one read-heavy operation.

        When SagaStore was constructed with ``db_path`` (production shape),
        callers get a short-lived independent connection so concurrent query()
        calls do not share sqlite3 connection state. When tests inject only a
        raw ``conn=`` with no path, there is no safe independent connection to
        open, so we fall back to the shared connection and the caller must use
        ``_db_locked``.
        """
        if self._db_path is None:
            return self._ensure_conn(), False
        # Ensure schema/migrations have run exactly once before a per-call
        # worker connection observes the file. This lock is narrow and only
        # protects shared-connection initialization, not the read operation.
        with self._db_lock:
            self._ensure_conn()
        return self._connect_db_path(enable_wal=False), True

    def _mark_retrieval_access_events(
        self,
        atom_ids: list[str],
        *,
        session_id: str | None,
        reference_date=None,
    ) -> None:
        """Record query() retrieval access events under the write lock.

        query() does its read pass on independent per-call connections so reads
        can overlap. The Pass-4 access-event write is deliberately split out and
        serialized here so concurrent retrievals do not race ``BEGIN IMMEDIATE``
        on either the shared connection or the sqlite file.
        """
        if not atom_ids:
            return
        events = [
            AccessEvent(atom_id=atom_id, source="retrieval", session_id=session_id)
            for atom_id in atom_ids
        ]
        with self._db_lock:
            with self._write_lock:
                conn = self._ensure_conn()
                # Best-effort + ownership-guarded. Access stats are
                # non-essential reinforcement — a failure here must NOT fail the
                # user-facing query (the read pass already ran on its own
                # connection). And only roll back the transaction THIS block
                # began: if BEGIN IMMEDIATE fails because a transaction is
                # already open on the shared connection (e.g. an unlocked
                # ``connection()`` caller mid-transaction), a blind rollback
                # would abort THEIR uncommitted work.
                began = False
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    began = True
                    mark_access(conn, events, now=reference_date)
                    conn.commit()
                except Exception as exc:  # noqa: BLE001 — non-essential write
                    if began:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    log.warning(
                        "retrieval access-event write skipped (%s)",
                        exc,
                    )

    async def _db_locked(self, fn):
        """Run a callable against the shared sqlite3 connection under lock.

        This is the fallback for conn-injected tests and for operations that
        intentionally mutate shared connection state. Do not route normal
        production query() reads through this helper; use per-call connections
        so SAGA read concurrency stays >1.
        """

        def _locked():
            with self._db_lock:
                return fn()

        return await asyncio.to_thread(_locked)

    async def _write_locked(self, fn):
        """Run a write-path callable in a worker thread, serialized via
        both the shared connection lock and the write lock. ``_db_lock``
        protects the shared sqlite3 connection object; ``_write_lock``
        preserves the stronger transaction-level write serialization contract.
        """

        def _locked():
            with self._db_lock:
                with self._write_lock:
                    return fn()

        return await asyncio.to_thread(_locked)

    def connection(self) -> sqlite3.Connection:
        """Public accessor for the underlying sqlite3 connection.

        Used by the upper layer (chainlink #266: skill-memory load
        injection) to run the skill-learning scoped recall, which lives
        in ``mimir.skill_memory`` and can't be reached from this lower
        layer (saga does not import up into ``mimir.*``). Callers that use
        this connection from worker threads must provide their own
        serialization; SagaStore's public methods do so via ``_db_lock``.
        Worker-thread callers should prefer :meth:`run_locked_read`,
        which provides that serialization for them (chainlink #411).
        """
        return self._ensure_conn()

    def run_locked_read(self, fn):
        """Run ``fn(conn)`` against the shared sqlite3 connection under
        ``_db_lock``. Returns ``fn``'s result.

        chainlink #411: the safe seam for upper-layer reads (the #266
        skill-memory load injection) that previously took ``connection()``
        and queried it from a bare worker thread while the consolidate
        cron / turn writers used the same ``check_same_thread=False``
        connection under the store's locks — exactly the cross-thread
        access the threading contract above forbids (#365/#386:
        segfault-class with FTS5). ``_db_lock`` is a ``threading.RLock``,
        so async callers offload via
        ``asyncio.to_thread(store.run_locked_read, fn)`` — the same
        worker-thread-holds-the-lock pattern as ``_db_locked`` (which
        consolidate()'s shared-conn reads use). Saga can't import up into
        ``mimir.*``, so the callable comes from the caller; keep ``fn`` a
        pure read (or a self-contained short transaction) — it holds the
        shared-connection lock for its whole duration.
        """
        with self._db_lock:
            return fn(self._ensure_conn())

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
        conn = self._connect_db_path()
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
        self,
        conn: sqlite3.Connection,
        *,
        fresh: bool,
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

    def _add_atom_to_index_locked(
        self,
        conn: sqlite3.Connection,
        atom_id: str,
    ) -> None:
        """Incrementally add ``atom_id``'s stored vector to the FAISS index,
        holding ``_index_lock`` (#492). No-op when the index isn't built.

        The lock matters: a concurrent ``query()``-driven lazy build/rebuild can
        reassign ``self._index`` between the built-check and the add, so an
        unlocked check-then-act could add into a discarded index (lost add) —
        inconsistent with every other index-mutation site, which all lock.
        ``_index_lock`` is an RLock, so callers already holding it nest safely.
        """
        with self._index_lock:
            if self._index is not None and self._index.built:
                row = conn.execute(
                    "SELECT vec FROM embeddings WHERE atom_id = ?",
                    (atom_id,),
                ).fetchone()
                if row is not None and row[0] is not None:
                    self._index.add(atom_id, row[0])

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

    def _rebuild_index_if_needed(self, conn: sqlite3.Connection) -> bool:
        """Invoke the documented >10%-soft-removed FAISS rebuild backstop.

        chainlink #425: ``VectorIndex.rebuild_if_needed`` existed but had
        zero callers, so the long-standing comments promising it "kicks
        in" were aspirational — soft removals accumulated forever and
        every search over-fetched past them. Called at the natural
        end-of-cycle points (``consolidate()`` / ``forget()``) where the
        index and connection are both in hand and a batch of removals
        may just have landed. Caller must hold ``_db_lock`` (the rebuild
        reads atoms/embeddings on the shared connection); ``_index_lock``
        is taken here, matching the established db-lock→index-lock
        ordering in query()/store(). Cost: a no-op compare below the 10%
        threshold; one bulk ``build_from_db`` above it.
        """
        if self._index is None or not self._index.built:
            return False
        with self._index_lock:
            return self._index.rebuild_if_needed(conn)

    async def rebuild_index_if_needed(self) -> bool:
        """Check the soft-removal threshold and rebuild the atom index if due.

        This public maintenance entry point lets the scheduler run the check
        independently of consolidation and forgetting cycles.
        """
        conn = self._ensure_conn()
        return await self._db_locked(lambda: self._rebuild_index_if_needed(conn))

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
        self,
        query: str,
        *,
        top_k: int = 12,
        mode: str = "task",
        token_budget: int = 500,
        session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
        reference_date=None,
        enable_contextual_rewrite: bool | None = None,
        pre_rewritten_query: str | None = None,
        extra_atom_ranked_pathways: Mapping[str, Iterable[str]] | None = None,
        rrf_pathway_weights: Mapping[str, float] | None = None,
        enable_session_boundary_rrf: bool | None = None,
        session_boundary_limit: int | None = None,
        session_boundary_alpha: float | None = None,
        session_boundary_weight: float | None = None,
        session_boundary_atoms_per_session: int | None = None,
        auth_context: Any = None,
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

        cfg = None
        if enable_session_boundary_rrf is None:
            from ._config_io import get_config

            cfg = get_config()
            enable_session_boundary_rrf = bool(
                cfg("retrieval", "enable_session_boundary_rrf", True)
            )

        extra_pathways_base = (
            dict(extra_atom_ranked_pathways)
            if extra_atom_ranked_pathways is not None
            else None
        )
        pathway_weights_base = (
            dict(rrf_pathway_weights) if rrf_pathway_weights is not None else None
        )
        boundary_limit = boundary_alpha = boundary_weight = None
        boundary_atoms_per_session = None
        should_add_boundary_pathway = bool(
            enable_session_boundary_rrf
            and (
                extra_pathways_base is None
                or "session_boundary" not in extra_pathways_base
            )
        )
        if should_add_boundary_pathway:
            if cfg is None:
                from ._config_io import get_config

                cfg = get_config()
            boundary_limit = int(
                session_boundary_limit
                if session_boundary_limit is not None
                else cfg("retrieval", "session_boundary_limit", 3)
            )
            boundary_alpha = float(
                session_boundary_alpha
                if session_boundary_alpha is not None
                else cfg("retrieval", "session_boundary_alpha", 0.7)
            )
            boundary_weight = float(
                session_boundary_weight
                if session_boundary_weight is not None
                else cfg("retrieval", "session_boundary_weight", 0.5)
            )
            boundary_atoms_per_session = int(
                session_boundary_atoms_per_session
                if session_boundary_atoms_per_session is not None
                else cfg("retrieval", "session_boundary_atoms_per_session", 30)
            )

        def _do_with_conn(conn: sqlite3.Connection) -> dict[str, Any]:
            # Compute the query embedding ONCE per query() call — both recall
            # and session-boundary routing can use it. The underlying provider
            # call (~50-300ms on voyage) is the heaviest non-LLM step.
            query_emb = _query_embed_sync(effective_query)
            extra_pathways = (
                dict(extra_pathways_base) if extra_pathways_base is not None else None
            )
            pathway_weights = (
                dict(pathway_weights_base) if pathway_weights_base is not None else None
            )
            if should_add_boundary_pathway:
                boundary_atom_ids = self._session_boundary_atom_pathway_with_conn(
                    conn,
                    effective_query,
                    limit=boundary_limit or 0,
                    alpha=boundary_alpha or 0.0,
                    atoms_per_session=boundary_atoms_per_session or 0,
                    query_emb=query_emb,
                    auth_context=auth_context,
                )
                if boundary_atom_ids:
                    extra_pathways = extra_pathways or {}
                    extra_pathways["session_boundary"] = boundary_atom_ids
                    pathway_weights = pathway_weights or {}
                    pathway_weights["session_boundary"] = boundary_weight or 0.5

            with self._index_lock:
                index = self._ensure_index(conn)
            from .ownership import get_authorization_scope

            auth_scope = get_authorization_scope(auth_context)
            # Triple-augment pathway uses the SAME embedding dim as the
            # atom-level FAISS index (triples are embedded under the
            # same provider). Pass dim through so triples with a stale
            # dim get filtered.
            triple_dim = self._embedding_dim
            result = _recall(
                conn,
                effective_query,
                query_embed_fn=lambda _q: query_emb,
                faiss_search_fn=_make_faiss_search_fn(
                    index,
                    conn,
                    auth_scope=auth_scope,
                    agent_id=self._agent_id,
                ),
                fts_search_fn=_make_fts_search_fn(
                    conn,
                    agent_id=self._agent_id,
                    synonyms=self._synonyms,
                    auth_scope=auth_scope,
                ),
                triple_search_fn=_make_triple_search_fn(
                    conn,
                    dim=triple_dim,
                    reference_date=reference_date,
                    auth_context=auth_context,
                ),
                extra_atom_ranked_pathways=extra_pathways,
                rrf_pathway_weights=pathway_weights,
                k=top_k,
                session_id=session_id,
                agent_id=self._agent_id,
                reference_date=reference_date,
                min_confidence_tier=min_confidence_tier,
                fire_access_events=False,
                auth_context=auth_context,
                auth_scope=auth_scope,
            )
            returned_atom_ids = [
                c.atom["id"] for c in (result.observations + result.raws)
            ]
            self._mark_retrieval_access_events(
                returned_atom_ids,
                session_id=session_id,
                reference_date=reference_date,
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
                    conn,
                    query_emb,
                    top_n=self._triples_top_n,
                    dim=triple_dim,
                    reference_date=reference_date,
                    auth_context=auth_context,
                )
                # Strip the internal _cosine field from the wire shape;
                # keep it out of the agent-facing dict.
                for t in rich:
                    triples_payload.append(
                        {k: v for k, v in t.items() if not k.startswith("_")}
                    )
            provenance_ids = list(dict.fromkeys(
                returned_atom_ids
                + [
                    str(item["source_atom_id"])
                    for item in triples_payload
                    if item.get("source_atom_id")
                ]
            ))
            ifc_sources: list[dict[str, Any]] = []
            if provenance_ids:
                placeholders = ",".join(["?"] * len(provenance_ids))
                rows = conn.execute(
                    "SELECT id, owner_principal, origin_channel, origin_domain, visibility "
                    f"FROM atoms WHERE id IN ({placeholders})",
                    provenance_ids,
                ).fetchall()
                by_id = {row[0]: row for row in rows}
                for atom_id in provenance_ids:
                    row = by_id.get(atom_id)
                    if row is not None:
                        ifc_sources.append({
                            "resource_id": f"atom:{row[0]}",
                            "owner_principal": row[1],
                            "origin_channel": row[2],
                            "origin_domain": row[3],
                            "visibility": row[4],
                        })
            # Translate the RecallResult into saga's response shape so
            # mimir's call sites don't change.
            return {
                "query": query,
                "mode": mode,
                "two_tier": True,
                "gated": result.gated,
                "gated_reason": result.gated_reason,
                "observations": [_candidate_to_atom(c) for c in result.observations],
                "raws": [_candidate_to_atom(c) for c in result.raws],
                "triples": triples_payload,
                "items_returned": len(result.observations) + len(result.raws),
                "rewritten_query": (rewritten_query or (result.rewritten_query or "")),
                "_ifc_sources": ifc_sources,
            }

        def _do():
            conn, should_close = self._operation_conn()
            try:
                return _do_with_conn(conn)
            finally:
                if should_close:
                    conn.close()

        if self._db_path is None:
            return await self._db_locked(_do)
        return await asyncio.to_thread(_do)

    def _session_boundary_atom_pathway_with_conn(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        limit: int = 3,
        alpha: float = 0.7,
        atoms_per_session: int = 30,
        query_emb: list[float] | None = None,
        auth_context: Any = None,
    ) -> list[str]:
        """Search session boundaries and expand matched sessions to atom ids.

        Session summaries are routing signals only: they are not rendered to
        the reader/prompt. Matched sessions contribute their member atoms as a
        bounded extra RRF pathway. This helper deliberately uses the caller's
        connection so default-on boundary recall does not double per-query
        sqlite connection churn.
        """
        from .ownership import (
            authorization_predicate,
            get_authorization_scope,
        )

        auth_scope = get_authorization_scope(auth_context)

        limit = max(0, int(limit))
        cap = max(0, int(atoms_per_session))
        if limit <= 0 or cap <= 0:
            return []
        if conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is None:
            return []

        try:
            matched_sessions = self._search_sessions_with_conn(
                conn,
                query,
                alpha=alpha,
                limit=limit,
                query_emb=query_emb,
                auth_context=auth_context,
            )
        except Exception:  # noqa: BLE001 — boundary recall is auxiliary
            log.warning("session-boundary RRF search failed", exc_info=True)
            return []
        if not matched_sessions:
            return []

        auth_where, auth_params = authorization_predicate(auth_scope, table="atoms")

        atom_ids: list[str] = []
        seen: set[str] = set()
        for session in matched_sessions:
            sid = session.get("session_id")
            if not sid:
                continue
            rows = conn.execute(
                f"""
                SELECT id
                  FROM atoms
                 WHERE session_id = ?
                   AND tombstoned = 0
                   AND {auth_where}
                 ORDER BY created_at, rowid
                 LIMIT ?
                """,
                (sid, *auth_params, cap),
            ).fetchall()
            for (atom_id,) in rows:
                if atom_id not in seen:
                    atom_ids.append(atom_id)
                    seen.add(atom_id)
        return atom_ids

    async def _session_boundary_atom_pathway(
        self,
        query: str,
        *,
        limit: int = 3,
        alpha: float = 0.7,
        atoms_per_session: int = 30,
    ) -> list[str]:
        def _do():
            conn, should_close = self._operation_conn()
            try:
                return self._session_boundary_atom_pathway_with_conn(
                    conn,
                    query,
                    limit=limit,
                    alpha=alpha,
                    atoms_per_session=atoms_per_session,
                )
            finally:
                if should_close:
                    conn.close()

        if self._db_path is None:
            return await self._db_locked(_do)
        return await asyncio.to_thread(_do)

    async def get_atoms(
        self, ids: list[str], auth_context: Any = None
    ) -> dict[str, Any]:
        """Batch-load atoms by exact id. Pure read — no semantic search, no
        access events, no transaction.

        The by-id counterpart to ``query`` (semantic recall). Use it to
        hydrate atoms whose ids are already known — e.g. ids cited in an
        observation or in a session-boundary "atoms cited" list — so the
        agent can read their content directly instead of stuffing each id
        into a semantic ``query`` and fanning out parallel calls (which also
        raced the shared connection's BEGIN IMMEDIATE; see recall.py).

        Deliberately fires NO access events: a by-id load fetches atoms we
        already know about (often to judge their usefulness), not a retrieval
        that surfaced something — so it must not reinforce activation.

        Like the other read paths (chainlink #365), it runs on a short-lived
        per-call connection (``_operation_conn``) rather than the shared one,
        so concurrent loads don't race sqlite connection state.

        Returns ``{"atoms": [...], "missing": [...]}``: atoms preserve request
        order and de-dupe; tombstoned / out-of-agent-scope / unknown ids are
        excluded from ``atoms`` and listed in ``missing``.
        """
        clean = [i for i in (ids or []) if isinstance(i, str) and i]
        if not clean:
            return {"atoms": [], "missing": []}

        from .ownership import (
            authorization_predicate,
            get_authorization_scope,
        )

        auth_scope = get_authorization_scope(auth_context)

        def _do_with_conn(conn: sqlite3.Connection):
            cols = (
                "id",
                "content",
                "stream",
                "profile",
                "memory_type",
                "source_type",
                "topics",
                "metadata",
                "agent_id",
                "is_pinned",
                "created_at",
                "session_id",
                "encoding_confidence",
                "owner_principal",
                "origin_channel",
                "origin_domain",
                "visibility",
            )
            unique = list(dict.fromkeys(clean))
            placeholders = ",".join(["?"] * len(unique))

            auth_where, auth_params = authorization_predicate(auth_scope)
            sql = (
                f"SELECT {', '.join(cols)} FROM atoms "
                f"WHERE id IN ({placeholders}) AND tombstoned = 0 "
                f"AND {auth_where}"
            )
            rows = conn.execute(sql, unique + auth_params).fetchall()
            found = {row[0]: dict(zip(cols, row)) for row in rows}
            atoms: list[dict[str, Any]] = []
            for aid in unique:
                a = found.get(aid)
                if a is None:
                    continue
                if a["agent_id"] != self._agent_id and a["agent_id"] != "shared":
                    continue
                atoms.append(
                    {
                        "id": a["id"],
                        "content": a["content"],
                        "stream": a.get("stream"),
                        "memory_type": a.get("memory_type"),
                        "source_type": a.get("source_type"),
                        "created_at": a.get("created_at"),
                        "topics": _safe_json_load(a.get("topics")),
                        "metadata": _safe_json_load(a.get("metadata")),
                    }
                )
            returned = {a["id"] for a in atoms}
            missing = [i for i in unique if i not in returned]
            return {
                "atoms": atoms,
                "missing": missing,
                "_ifc_sources": [
                    {
                        "resource_id": f"atom:{a['id']}",
                        "owner_principal": found[a["id"]].get("owner_principal"),
                        "origin_channel": found[a["id"]].get("origin_channel"),
                        "origin_domain": found[a["id"]].get("origin_domain"),
                        "visibility": found[a["id"]].get("visibility"),
                    }
                    for a in atoms
                ],
            }

        def _do():
            conn, should_close = self._operation_conn()
            try:
                return _do_with_conn(conn)
            finally:
                if should_close:
                    conn.close()

        if self._db_path is None:
            return await self._db_locked(_do)
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
        self,
        content: str,
        *,
        stream: str | None = None,
        profile: str | None = None,
        source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
        precomputed_embedding: EmbeddingTuple | None = None,
        session_id: str | None = None,
        session_dedup_threshold: float | None = None,
        owner_principal: str | None = None,
        origin_channel: str | None = None,
        origin_domain: str | None = None,
        visibility: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # session_dedup_threshold is forwarded straight to store() — off
        # by default, opt-in by callers that want session-paraphrase
        # collapse. The bench harness leaves it None.
        content = content.strip()
        if not content:
            raise ValueError("store: content cannot be empty")

        # Compute provider embeddings before taking SagaStore's shared
        # DB/write locks. ``store.py`` already kept embedding outside the
        # sqlite transaction, but this facade wrapped the whole call in
        # ``_write_locked``; slow provider retries therefore serialized every
        # SAGA writer. Preserve the exact-duplicate fast path (no embed needed)
        # by doing the same cheap content-hash check under only ``_db_lock``.
        effective_embedding = precomputed_embedding
        effective_owner = owner_principal or "legacy_admin"
        if effective_embedding is None:
            content_hash = _store_hash_content(content)

            def _exact_duplicate_exists() -> bool:
                conn = self._ensure_conn()
                row = conn.execute(
                    "SELECT 1 FROM atoms WHERE content_hash = ? "
                    "AND agent_id = ? AND owner_principal = ? AND tombstoned = 0",
                    (content_hash, self._agent_id, effective_owner),
                ).fetchone()
                return row is not None

            duplicate_exists = await self._db_locked(_exact_duplicate_exists)
            if not duplicate_exists:
                effective_embedding = await asyncio.to_thread(
                    _embed_text_sync,
                    content,
                )

        def _do():
            conn = self._ensure_conn()
            result = _store(
                conn,
                content,
                embed_fn=_embed_text_sync,
                stream=stream or "semantic",
                profile=profile or "standard",
                source_type=source_type,
                metadata=metadata,
                agent_id=self._agent_id,
                session_id=session_id,
                precomputed_embedding=effective_embedding,
                session_dedup_threshold=session_dedup_threshold,
                owner_principal=owner_principal,
                origin_channel=origin_channel,
                origin_domain=origin_domain,
                visibility=visibility,
                provenance=provenance,
            )
            if result.stored:
                # Incremental-add to the FAISS index if it's already
                # been built. If not, the next query's lazy build
                # will pick the new atom up from disk.
                with self._index_lock:
                    if self._index is not None and self._index.built:
                        row = conn.execute(
                            "SELECT vec FROM embeddings WHERE atom_id = ?",
                            (result.atom_id,),
                        ).fetchone()
                        if row is not None and row[0] is not None:
                            self._index.add(result.atom_id, row[0])
                return {"stored": True, "atom_id": result.atom_id}
            return {
                "stored": False,
                "atom_id": result.atom_id,
                "reason": result.reason or "duplicate",
            }

        return await self._write_locked(_do)

    async def feedback(
        self,
        atom_ids: list[str],
        response_text: str,
        *,
        session_id: str | None = None,
        feedback: str | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]:
        signal = (feedback or "positive").lower()
        scope = _saga_mutation_scope(auth_context, "saga_feedback")

        def _do():
            from . import feedback as _feedback

            conn = self._ensure_conn()
            authorized_ids = _authorized_atom_ids(conn, atom_ids, scope)
            if authorized_ids is None:
                return {"marked": 0, "total": len(atom_ids), "authorized": 0}
            if not authorized_ids:
                return {"marked": 0, "total": len(atom_ids), "authorized": 0}
            n = _feedback(conn, authorized_ids, signal=signal, session_id=session_id)
            return {
                "marked": n,
                "total": len(atom_ids),
                "authorized": len(authorized_ids),
            }

        return await self._write_locked(_do)

    async def outcome(
        self,
        atom_ids: list[str],
        feedback: str,
        *,
        session_id: str | None = None,
        query: str | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]:
        """Outcome is saga's "after the response was delivered, was it
        well-received?" signal.

        - ``feedback="positive"`` → write a ``feedback_positive``
          access event on each atom (weight 2.0). Same as the credit
          pass.
        - ``feedback="negative"`` → write a ``feedback_negative`` event
          (weight **-1.0** per ``SOURCE_WEIGHTS`` — subtracts one
          access-equivalent, cancelling a prior retrieval/credit event so
          the atom's activation actually drops, not just stays flat). The
          event also serves as a flag ``forget_by_criteria`` can join on
          to surface atoms for review.
        - other values → no-op.

        Returns ``{"marked": n, "total": len(atom_ids), "signal": ...}``.
        """
        signal = (feedback or "").lower()
        if signal == "positive":
            return await self.feedback(
                atom_ids,
                "",
                session_id=session_id,
                feedback="positive",
                auth_context=auth_context,
            )
        if signal == "negative":
            scope = _saga_mutation_scope(auth_context, "saga_feedback")

            def _do():
                conn = self._ensure_conn()
                authorized_ids = _authorized_atom_ids(conn, atom_ids, scope)
                if authorized_ids is None:
                    return {
                        "marked": 0,
                        "total": len(atom_ids),
                        "signal": "negative",
                        "authorized": 0,
                    }
                if not authorized_ids:
                    return {
                        "marked": 0,
                        "total": len(atom_ids),
                        "signal": "negative",
                        "authorized": 0,
                    }
                events = [
                    AccessEvent(
                        atom_id=aid,
                        source="feedback_negative",
                        session_id=session_id,
                        metadata={"reason": "outcome_negative"},
                    )
                    for aid in authorized_ids
                ]
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    n = mark_access(conn, events)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return {
                    "marked": n,
                    "total": len(atom_ids),
                    "signal": "negative",
                    "authorized": len(authorized_ids),
                }

            return await self._write_locked(_do)
        return {"marked": 0, "total": len(atom_ids), "signal": signal or "noop"}

    async def end_session(
        self,
        session_id: str,
        summary: str,
        *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
        channel_id: str | None = None,
        owner_principal: str | None = None,
        origin_channel: str | None = None,
        origin_domain: str | None = None,
        visibility: str | None = None,
        provenance: dict[str, Any] | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]:
        """Close a session. ``channel_id`` is persisted on the
        sessions row so ``recent_session_boundaries(channel_id=...)``
        can scope to a single channel — without it the LEFT JOIN on
        sessions can't filter (every boundary's channel_id would be
        NULL).
        """

        mutation_scope = None
        if auth_context is not None:
            mutation_scope = _saga_mutation_scope(auth_context, "saga_end_session")
            if (
                mutation_scope is None
                or getattr(auth_context, "saga_session_id", None) != session_id
            ):
                raise PermissionError("session write denied")
            if isinstance(mutation_scope, str) and owner_principal != mutation_scope:
                raise PermissionError("session write denied")
            # A trusted service's mutation scope proves execution authority.
            # The durable session row deliberately inherits the source session's
            # user ACL, including its ingress origin_domain (discord/slack/web),
            # so atom-read readable_domains must not be applied here. Session-id
            # binding above plus reflect()'s immutable row-identity check prevent
            # first-writer preemption and later owner/channel/domain rebinding.

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
                conn,
                session_id=session_id,
                channel_id=channel_id,
                embed_fn=_embed_text_sync,
                boundary_synth_fn=_stub_synth,
                owner_principal=owner_principal,
                origin_channel=origin_channel,
                origin_domain=origin_domain,
                visibility=visibility,
                provenance=provenance,
            )
            # Invalidate sessions index so the next search_sessions() call
            # picks up the newly-written session and its embedding.
            with self._sessions_index_lock:
                self._sessions_index_built = False
            return {
                "session_id": session_id,
                "channel": channel_id,
                "session_summary_written": result.session_summary_written,
            }

        return await self._write_locked(_do)

    async def consolidate(
        self,
        *,
        dry_run: bool = False,
        max_clusters: int | None = None,
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
            from .dedup import (
                DEFAULT_DEDUP_THRESHOLD,
                DedupResult,
                dedup_pass,
                distinct_dedup_scopes,
            )
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
                conn,
                threshold=effective_dedup_threshold,
                scope_acl=True,
            )

            scopes = await self._db_locked(
                lambda: distinct_dedup_scopes(conn, agent_id=self._agent_id)
            )
            dedup_result = DedupResult()
            for owner, domain, scope_visibility in scopes:
                remaining = (
                    None
                    if dedup_max_clusters is None
                    else dedup_max_clusters - len(dedup_result.canonicals_kept)
                )
                if remaining is not None and remaining <= 0:
                    break

                def _do_dedup(
                    owner=owner,
                    domain=domain,
                    scope_visibility=scope_visibility,
                    remaining=remaining,
                ):
                    return dedup_pass(
                        conn,
                        cluster_fn=dedup_cluster_fn,
                        agent_id=self._agent_id,
                        owner_principal=owner,
                        origin_domain=domain,
                        visibility=scope_visibility,
                        lookback_days=lookback_days,
                        min_cluster_size=2,
                        dry_run=dry_run,
                        max_clusters=remaining,
                        reference_date=reference_date,
                    )

                scoped_result = await self._write_locked(_do_dedup)
                dedup_result.candidates_scanned += scoped_result.candidates_scanned
                dedup_result.clusters_formed += scoped_result.clusters_formed
                dedup_result.canonicals_kept.extend(scoped_result.canonicals_kept)
                dedup_result.duplicates_tombstoned.extend(
                    scoped_result.duplicates_tombstoned
                )
                dedup_result.merges.update(scoped_result.merges)
            dedup_payload = {
                "candidates_scanned": dedup_result.candidates_scanned,
                "clusters_formed": dedup_result.clusters_formed,
                "canonicals_kept": dedup_result.canonicals_kept,
                "duplicates_tombstoned": dedup_result.duplicates_tombstoned,
                "threshold": effective_dedup_threshold,
            }
            # chainlink #390: remove the dedup-tombstoned raws from the FAISS
            # index too (mirror forget()). WHERE tombstoned=0 masks them from
            # final SQL results, but their vectors still consume FAISS top_k
            # slots — so over-fetch climbs and genuinely-relevant atoms can get
            # pushed out of the candidate set until a cold rebuild. Best-effort:
            # a missed removal degrades gracefully (SQL still masks the row).
            if (
                not dry_run
                and dedup_result.duplicates_tombstoned
                and self._index is not None
                and self._index.built
            ):
                with self._index_lock:
                    for atom_id in dedup_result.duplicates_tombstoned:
                        try:
                            self._index.remove(atom_id)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "FAISS index remove failed for dedup-tombstoned "
                                "atom_id=%r",
                                atom_id,
                                exc_info=True,
                            )

        # 1. Candidate selection + clustering (sync; reads only).
        # Re-fetches raws so the tombstoned duplicates from pass 1
        # don't appear as candidates for thematic clustering.
        from .consolidate import (
            _candidate_raws,
            MAX_OBSERVATIONS_PER_RUN,
        )

        # chainlink #386: shared-connection reads run under _db_lock so a
        # concurrent turn's write (also _db_lock-guarded) never touches the same
        # sqlite3 connection object at the same time — Python's sqlite3 + FTS5
        # can segfault on concurrent access to one shared connection (class
        # docstring / chainlink #365). The dedup pass above already clusters
        # under the write lock; this brings the thematic read phase in line.
        raws = await self._db_locked(
            lambda: _candidate_raws(
                conn,
                lookback_days=lookback_days,
                agent_id=self._agent_id,
                reference_date=reference_date,
            )
        )
        # chainlink #331: world_state structural integrity — collapse any
        # dual-current rows from a transient cross-caller write race — is
        # independent of whether there are enough raws to consolidate. Run it for
        # EVERY non-dry-run consolidate, BEFORE the candidate-count early return,
        # so migrated/old corruption sitting in a quiet DB still gets repaired
        # (the case that's never reached if it lives only in the cluster path).
        # Within one serialized consolidate ``_update_world_state`` can't create a
        # dual-current row, so running it here rather than after synthesis loses
        # nothing; the race is a cross-caller phenomenon this pass cleans up.
        world_state_repairs: list = []
        if not dry_run:
            from .triples import repair_world_state_dual_current

            world_state_repairs = await self._write_locked(
                lambda: repair_world_state_dual_current(conn)
            )

        if len(raws) < min_cluster_size:
            # chainlink #425: end-of-cycle FAISS backstop applies on this
            # early exit too — the dedup pass above may have soft-removed
            # vectors even when too few candidates remain for synthesis.
            if not dry_run:
                await self._db_locked(lambda: self._rebuild_index_if_needed(conn))
            return {
                "clusters_formed": 0,
                "observations_emitted": [],
                "world_state_dual_current_repaired": len(world_state_repairs),
                "dedup": dedup_payload,
            }

        cluster_fn = make_default_cluster_fn(conn)
        clusters = await self._db_locked(lambda: cluster_fn(raws))  # chainlink #386

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
                max_obs,
                len(eligible_unbounded) - max_obs,
            )
        sem = asyncio.Semaphore(4)

        # P47 / P48: build both prompt additions per cluster. Vocabulary is
        # owner-scoped so one cluster cannot receive another owner's names.
        from .synthesize import build_vocab_block, build_prior_block
        from .consolidate import _compute_intersected_acl

        prior_blocks: list[str] = []
        vocab_blocks: list[str] = []
        for cluster in eligible:
            evidence_ids = [a["id"] for a in cluster]
            pb, vb = await self._db_locked(  # chainlink #386
                lambda eids=evidence_ids: (
                    build_prior_block(conn, eids),
                    build_vocab_block(
                        conn,
                        owner_principal=_compute_intersected_acl(
                            conn, eids
                        ).owner_principal,
                        extra_subjects=list(extra_canonical_subjects or []),
                    ),
                )
            )
            prior_blocks.append(pb)
            vocab_blocks.append(vb)

        async def _synth(cluster, prior_block, vocab_block):
            async with sem:
                try:
                    return await self._rich_synth_fn(
                        cluster,
                        prior_block=prior_block,
                        vocab_block=vocab_block,
                    )
                except Exception:
                    return {
                        "content": "",
                        "topics": [],
                        "triples": [],
                        "contradictions": [],
                    }

        results = await asyncio.gather(
            *[
                _synth(c, pb, vb)
                for c, pb, vb in zip(eligible, prior_blocks, vocab_blocks)
            ]
        )

        # 2b. chainlink #417: precompute ALL embeddings (each observation's
        # text + each extracted triple's embed input) BEFORE _restructure
        # takes the write lock and opens BEGIN IMMEDIATE. store.py's
        # invariant — embedding is network I/O and must run before the
        # transaction — applied to the consolidate path: pre-fix,
        # ``store_triples(.., embed_fn=_embed_text_sync)`` embedded
        # per-triple INSIDE the relations transaction (and the
        # observation's own embed ran while the global write lock was
        # held), so one hung provider call stalled every memory write in
        # the process. After this block, _restructure does SQLite work
        # only. Observation-embed failures propagate (same crash-the-pass
        # semantics as the pre-fix in-transaction failure); triple-embed
        # failures are tolerated per-triple, mirroring store_triples'
        # own warn-and-store-unembedded fallback.
        from .triples import _triple_text

        def _precompute_embeddings():
            obs_embeds: list[tuple[bytes, str, str, int] | None] = []
            triple_vecs: dict[str, tuple[bytes, str, str, int]] = {}
            for result in results:
                content = (result.get("content") or "").strip()
                if not content:
                    obs_embeds.append(None)
                    continue
                obs_embeds.append(_embed_text_sync(content))
                for t in result.get("triples", []):
                    try:
                        text = _triple_text(
                            t["subject"],
                            t["predicate"],
                            t["object"],
                        )
                        if text in triple_vecs:
                            continue
                        triple_vecs[text] = _embed_text_sync(text)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("triple embed precompute failed: %s", exc)
            return obs_embeds, triple_vecs

        obs_embeds, triple_vecs = await asyncio.to_thread(_precompute_embeddings)

        def _lookup_triple_embed(text: str) -> tuple[bytes, str, str, int]:
            """Pure dict lookup over the precomputed vectors — never does
            I/O, so it's safe to hand to store_triples inside the
            transaction. Raising on a miss (embed failed above) routes
            store_triples to its existing store-unembedded fallback."""
            vec = triple_vecs.get(text)
            if vec is None:
                raise RuntimeError(f"no precomputed embedding for triple text {text!r}")
            return vec

        # 3. Per-cluster restructure: store observation, link evidence,
        # emit access events. Each cluster runs its own short transaction
        # so an LLM failure on one doesn't block the others. Done in a
        # thread so SQLite stays on one writer thread.
        def _restructure():
            from .consolidate import (
                _compute_intersected_acl,
                find_equal_evidence_obs,
                find_superseded_observations,
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
            for cluster, result, obs_emb in zip(eligible, results, obs_embeds):
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

                intersected_acl = _compute_intersected_acl(conn, evidence_ids)

                store_result = _store_atom(
                    conn,
                    content,
                    # chainlink #417: embedding was precomputed above,
                    # before the write lock / transactions; embed_fn is
                    # the unused fallback (store never calls it when
                    # precomputed_embedding is supplied).
                    embed_fn=_embed_text_sync,
                    precomputed_embedding=obs_emb,
                    memory_type="observation",
                    stream="semantic",
                    topics=topics,
                    agent_id=self._agent_id,
                    session_id=None,
                    owner_principal=intersected_acl.owner_principal,
                    origin_channel=intersected_acl.origin_channel,
                    origin_domain=intersected_acl.origin_domain,
                    visibility=intersected_acl.visibility,
                    provenance=intersected_acl.provenance,
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
                        conn,
                        observation_id,
                        set(evidence_ids),
                    )
                    for old_id in old_obs:
                        conn.execute(
                            "INSERT OR IGNORE INTO atom_relations "
                            "(source_id, target_id, relation_type, confidence, "
                            "created_at, metadata) "
                            "VALUES (?, ?, 'supersedes', 1.0, ?, ?)",
                            (
                                observation_id,
                                old_id,
                                now,
                                json.dumps({"trigger": "consolidate"}),
                            ),
                        )
                    conn.execute(
                        "INSERT INTO observations_metadata "
                        "(atom_id, evidence_count, trend, last_evidence_at, "
                        "consolidated_at) VALUES (?, ?, ?, ?, ?)",
                        (observation_id, len(evidence_ids), "strengthening", now, now),
                    )
                    # P42: store any triples the LLM extracted. Source
                    # them to the new observation atom (not the raws)
                    # so triple retrieval surfaces the observation —
                    # the two-tier pathway then lifts the raws via the
                    # existing evidenced_by boost in recall.py.
                    if triples:
                        added = store_triples(
                            conn,
                            triples,
                            source_atom_id=observation_id,
                            # chainlink #417: precomputed-vector lookup —
                            # no network I/O inside this transaction.
                            embed_fn=_lookup_triple_embed,
                            # chainlink #884: pass evidence atoms so triples
                            # inherit their intersected ACL.
                            evidence_ids=evidence_ids,
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
                                (
                                    aid_a,
                                    aid_b,
                                    now,
                                    json.dumps(
                                        {
                                            "summary": c.get("summary", ""),
                                            "trigger": "consolidate",
                                        }
                                    ),
                                ),
                            )
                            if cursor.rowcount > 0:
                                contradicts_stored += 1
                    conn.commit()
                except Exception:
                    conn.rollback()
                    # chainlink #391: _store_atom above committed the observation
                    # in its OWN transaction before this relations transaction.
                    # On rollback it would remain an orphan — no evidenced_by /
                    # observations_metadata — that recall surfaces unbacked, and
                    # find_equal_evidence_obs can't match on retry (it has no
                    # evidence edges) so a re-run duplicates it. Tombstone it
                    # (matches forget's removal model; avoids FK/FTS/embedding
                    # orphan issues a DELETE would risk) before re-raising. It
                    # was never added to the FAISS index (that happens only after
                    # the commit below), so no index cleanup is needed.
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            "UPDATE atoms SET tombstoned=1 WHERE id=?",
                            (observation_id,),
                        )
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        log.warning(
                            "failed to tombstone orphaned observation %s after "
                            "restructure rollback",
                            observation_id,
                            exc_info=True,
                        )
                    raise

                # Trend recompute in its own short txn (refresh_trend
                # manages its own BEGIN/COMMIT).
                refresh_trend(conn, observation_id)

                # Incrementally add the new observation to the FAISS index
                # so the next query can surface it (#492: under _index_lock).
                self._add_atom_to_index_locked(conn, observation_id)

                emitted.append(observation_id)
                for old_id in old_obs:
                    superseded.append((observation_id, old_id))
            # After all contradicts edges land, resolve them into
            # supersedes edges (newer-atom-wins strategy). One pass
            # at end of consolidate so the run sees a consistent view
            # of all contradictions discovered together.
            from .triples import resolve_contradictions_to_supersedes

            new_supersedes_from_contra = (
                resolve_contradictions_to_supersedes(conn) if contradicts_stored else 0
            )
            return (
                emitted,
                superseded,
                triples_stored,
                contradicts_stored,
                new_supersedes_from_contra,
            )

        # _restructure mutates atoms/observations/triples — write lock
        # serializes it against any concurrent agent-loop store / feedback.
        (
            emitted,
            superseded,
            n_triples,
            n_contra,
            n_supersedes_contra,
        ) = await self._write_locked(_restructure)
        # chainlink #425: end-of-cycle FAISS backstop — when soft removals
        # (dedup tombstones mirrored out of the index above) exceed 10% of
        # positions, run the full rebuild ``rebuild_if_needed`` promises.
        await self._db_locked(lambda: self._rebuild_index_if_needed(conn))
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
            "world_state_dual_current_repaired": len(world_state_repairs),
            "dedup": dedup_payload,
        }

    async def consolidate_skill_memories(
        self,
        *,
        dry_run: bool = False,
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
        from .dedup import (
            DEFAULT_DEDUP_THRESHOLD,
            DedupResult,
            dedup_pass,
            distinct_dedup_scopes,
        )
        from .embeddings import resolve_auto_threshold
        from ._config_io import get_config

        conn = self._ensure_conn()
        # chainlink #386: shared-conn read under _db_lock (see consolidate()).
        skills = await self._db_locked(
            lambda: distinct_skill_scopes(conn, agent_id=self._agent_id)
        )
        summary: dict[str, Any] = {
            "skills_scanned": len(skills),
            "threshold": None,
            "skills": {},
        }
        if not skills:
            if not dry_run:
                await self.rebuild_index_if_needed()
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
            conn,
            threshold=effective_threshold,
            scope_acl=True,
        )

        for skill in skills:
            scopes = await self._db_locked(
                lambda skill=skill: distinct_dedup_scopes(
                    conn, agent_id=self._agent_id, skill_scope=skill,
                )
            )
            res = DedupResult()
            for owner, domain, scope_visibility in scopes:
                remaining = (
                    None
                    if dedup_max_clusters is None
                    else dedup_max_clusters - len(res.canonicals_kept)
                )
                if remaining is not None and remaining <= 0:
                    break

                def _do_dedup(
                    skill=skill,
                    owner=owner,
                    domain=domain,
                    scope_visibility=scope_visibility,
                    remaining=remaining,
                ):
                    return dedup_pass(
                        conn,
                        cluster_fn=cluster_fn,
                        agent_id=self._agent_id,
                        owner_principal=owner,
                        origin_domain=domain,
                        visibility=scope_visibility,
                        lookback_days=lookback_days,
                        min_cluster_size=min_cluster_size,
                        dry_run=dry_run,
                        max_clusters=remaining,
                        skill_scope=skill,
                    )

                scoped_result = await self._write_locked(_do_dedup)
                res.candidates_scanned += scoped_result.candidates_scanned
                res.clusters_formed += scoped_result.clusters_formed
                res.canonicals_kept.extend(scoped_result.canonicals_kept)
                res.duplicates_tombstoned.extend(
                    scoped_result.duplicates_tombstoned
                )
                res.merges.update(scoped_result.merges)

            # chainlink #425: mirror the #390 fix here — the general
            # consolidate() removes dedup-tombstoned raws from the FAISS
            # index, but this per-skill pass didn't, so a skill's merged
            # learnings kept their vectors live and consumed top_k slots
            # (the exact #390 regression). Best-effort: a missed removal
            # degrades gracefully (WHERE tombstoned=0 still masks the row).
            if (
                not dry_run
                and res.duplicates_tombstoned
                and self._index is not None
                and self._index.built
            ):
                with self._index_lock:
                    for atom_id in res.duplicates_tombstoned:
                        try:
                            self._index.remove(atom_id)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "FAISS index remove failed for dedup-tombstoned "
                                "skill-learning atom_id=%r",
                                atom_id,
                                exc_info=True,
                            )
            summary["skills"][skill] = {
                "candidates_scanned": res.candidates_scanned,
                "clusters_formed": res.clusters_formed,
                "canonicals_kept": res.canonicals_kept,
                "duplicates_tombstoned": res.duplicates_tombstoned,
            }
        if not dry_run:
            await self.rebuild_index_if_needed()
        return summary

    async def forget(
        self,
        *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]:
        scope = _saga_mutation_scope(auth_context, "saga_forget")

        # Map saga's criteria-based forget to forget_by_criteria. Also
        # synchronizes the in-memory FAISS index — ``forget_by_criteria``
        # tombstones the SQLite rows but doesn't know about the index,
        # so without explicit removal here the index accumulates
        # orphaned positions: retrieval still works (the SQL-side
        # ``WHERE tombstoned = 0`` filter in ``recall.py`` masks them
        # out), but over-fetches climb past the removal noise. The
        # end-of-pass ``_rebuild_index_if_needed`` call (chainlink #425)
        # collapses the accumulated soft removals into a fresh index
        # once they exceed 10% of positions.
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
            from .forget import forget as forget_atoms

            conn = self._ensure_conn()
            if scope is None:
                return {
                    "tombstoned_count": 0,
                    "preview_ids": [],
                    "dry_run": dry_run,
                }
            preview = forget_by_criteria(
                conn,
                agent_id=self._agent_id,
                min_age_days=grace_days,
                min_retrievals=min_retrievals,
                activation_below=confidence_floor,
                dry_run=True,
                owner_principal=(
                    scope.owner_principal
                    if isinstance(scope, _ServiceMutationScope)
                    else scope if isinstance(scope, str) else None
                ),
                origin_domains=(
                    scope.readable_domains
                    if isinstance(scope, _ServiceMutationScope)
                    else None
                ),
            )
            candidate_ids = preview.tombstoned_ids
            affected_observation_ids: list[str] = []
            if candidate_ids:
                placeholders = ",".join(["?"] * len(candidate_ids))
                affected_observation_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT source_id FROM atom_relations "
                        f"WHERE target_id IN ({placeholders}) "
                        "AND relation_type = 'evidenced_by'",
                        candidate_ids,
                    ).fetchall()
                ]
            closure = list(dict.fromkeys(candidate_ids + affected_observation_ids))
            if _authorized_atom_ids(conn, closure, scope) is None:
                return {
                    "tombstoned_count": 0,
                    "preview_ids": [],
                    "dry_run": dry_run,
                }
            if dry_run:
                return {
                    "tombstoned_count": len(candidate_ids),
                    "preview_ids": candidate_ids,
                    "dry_run": True,
                }
            result = forget_atoms(conn, candidate_ids, reason="bulk_criteria")
            authorized_ids = result.tombstoned_ids

            # Remove tombstoned atoms from the FAISS index (not a dry-run,
            # and the index has been built — otherwise there's nothing
            # to remove). Failures are logged but non-fatal: the SQL
            # filter still masks tombstones at retrieval time, so a
            # missed index-side removal degrades gracefully.
            if (
                not result.dry_run
                and authorized_ids
                and self._index is not None
                and self._index.built
            ):
                with self._index_lock:
                    for atom_id in authorized_ids:
                        try:
                            self._index.remove(atom_id)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "FAISS index remove failed for atom_id=%r",
                                atom_id,
                                exc_info=True,
                            )
            # chainlink #425: end-of-cycle FAISS backstop. _do runs under
            # _write_locked (so _db_lock is held, as the helper requires).
            if not result.dry_run:
                self._rebuild_index_if_needed(conn)
            return {
                "tombstoned_count": len(authorized_ids),
                "preview_ids": authorized_ids if dry_run else [],
                "dry_run": dry_run,
            }

        return await self._write_locked(_do)

    async def recent_session_boundaries(
        self,
        *,
        channel_id: str | None = None,
        count: int = 3,
        auth_context: Any = None,
    ) -> list[dict[str, Any]]:
        def _do():
            conn, should_close = self._operation_conn()
            try:
                return _recent_boundaries(
                    conn,
                    channel_id=channel_id,
                    count=count,
                    auth_context=auth_context,
                )
            finally:
                if should_close:
                    conn.close()

        if self._db_path is None:
            return await self._db_locked(_do)
        return await asyncio.to_thread(_do)

    def _search_sessions_with_conn(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        channel_id: str | None = None,
        alpha: float = 0.7,
        limit: int = 10,
        query_emb: list[float] | None = None,
        auth_context: Any = None,
    ) -> list[dict]:
        """Connection-scoped implementation for :meth:`search_sessions`.

        Keeping this synchronous helper separate lets default-on query() run
        boundary routing on the same short-lived per-query connection as atom
        recall, preserving the one-connection-per-read invariant.
        """
        import math

        from .ownership import (
            authorization_predicate_for_sessions,
            get_authorization_scope,
        )

        auth_scope = get_authorization_scope(auth_context)

        if query_emb is None:
            query_emb = _query_embed_sync(query) if alpha > 0.0 else []

        # ── Step 1: build similarity map from sessions FAISS index ──
        sim_map: dict[str, float] = {}  # session_id → cosine similarity

        if query_emb:
            with self._sessions_index_lock:
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
                for sess_id, emb_blob in conn.execute(
                    "SELECT id, embedding FROM sessions WHERE embedding IS NOT NULL"
                ).fetchall():
                    if not emb_blob:
                        continue
                    # #432: a stored embedding from a different-dim provider
                    # (after a provider switch) must not be unpacked at the
                    # QUERY dim — ``emb_blob[:dim*4]`` would silently truncate
                    # a larger vector into a clean-but-meaningless similarity,
                    # precisely when the FAISS path is empty due to dim
                    # filtering. Require an exact byte-length match (mirrors
                    # the dim guard in store.py's cosine path).
                    if len(emb_blob) != dim * 4:
                        continue
                    try:
                        e_arr = _struct.unpack(f"{dim}f", emb_blob)
                        dot = sum(a * b for a, b in zip(query_emb, e_arr))
                        e_norm = math.sqrt(sum(x * x for x in e_arr)) + 1e-9
                        sim_map[sess_id] = dot / (q_norm * e_norm)
                    except Exception:
                        continue

        # ── Step 2: fetch sessions rows ──
        auth_where, auth_params = authorization_predicate_for_sessions(auth_scope)
        channel_clause = "AND channel_id = ?" if channel_id else ""
        params: list = list(auth_params)
        if channel_id:
            params.append(channel_id)

        rows = conn.execute(
            f"""
            SELECT id, channel_id, started_at, ended_at, summary, reflected_at
            FROM sessions
            WHERE {auth_where} {channel_clause}
            ORDER BY COALESCE(ended_at, reflected_at) DESC
            LIMIT 500
            """,
            params,
        ).fetchall()

        # ── Step 3: score each session ──
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        results: list[dict] = []
        for sess_id, ch_id, started_at, ended_at, summary, reflected_at in rows:
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
            results.append(
                {
                    "session_id": sess_id,
                    "channel_id": ch_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "summary": summary or "",
                    "similarity_score": round(sim, 6),
                    "recency_score": round(recency, 6),
                    "blended_score": round(blended, 6),
                }
            )

        results.sort(key=lambda r: r["blended_score"], reverse=True)
        return results[:limit]

    async def search_sessions(
        self,
        query: str,
        *,
        channel_id: str | None = None,
        alpha: float = 0.7,
        limit: int = 10,
        auth_context: Any = None,
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
        # Skip the embed round-trip when alpha=0 (pure recency — cosine score
        # is never consulted).  The downstream helper handles query_emb==[] via
        # the existing ``if query_emb:`` guard, so the recency path still works.
        if alpha > 0.0:
            query_emb: list[float] = await asyncio.to_thread(_query_embed_sync, query)
        else:
            query_emb = []

        def _do() -> list[dict]:
            conn, should_close = self._operation_conn()
            try:
                return self._search_sessions_with_conn(
                    conn,
                    query,
                    channel_id=channel_id,
                    alpha=alpha,
                    limit=limit,
                    query_emb=query_emb,
                    auth_context=auth_context,
                )
            finally:
                if should_close:
                    conn.close()

        if self._db_path is None:
            return await self._db_locked(_do)
        return await asyncio.to_thread(_do)

    async def most_retrieved_atoms(
        self,
        *,
        days: int = 7,
        count: int = 10,
        channel_id: str | None = None,
        contributed_only: bool = False,
        trend: str | None = None,
        auth_context: Any = None,
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
        - ``auth_context``: applies authorization predicate to filter
          results by visibility/owner/domain scope.
        """
        from datetime import datetime, timedelta, timezone

        from .ownership import (
            authorization_predicate,
            get_authorization_scope,
        )

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        auth_scope = get_authorization_scope(auth_context)

        def _do():
            conn, should_close = self._operation_conn()
            try:
                return _most_retrieved_with_conn(conn)
            finally:
                if should_close:
                    conn.close()

        def _most_retrieved_with_conn(conn: sqlite3.Connection):
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
                joins.append("JOIN sessions s ON s.id = e.session_id")
                where.append("s.channel_id = ?")
                params.append(channel_id)

            if trend is not None:
                joins.append("JOIN observations_metadata om ON om.atom_id = a.id")
                where.append("om.trend = ?")
                params.append(trend)

            auth_where, auth_params = authorization_predicate(auth_scope, table="a")
            where.append(auth_where)
            params.extend(auth_params)

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
                {"id": r[0], "content": r[1], "retrieval_count": r[2]} for r in rows
            ]

        if self._db_path is None:
            return await self._db_locked(_do)
        return await asyncio.to_thread(_do)

    async def mark_contributions(
        self,
        retrieved_atoms: list[dict[str, Any]],
        response_text: str,
        *,
        session_id: str | None = None,
        threshold: float | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]:
        """Credit only when authority covers every referenced atom."""
        from .contributions import (
            mark_contributions as _mc,
            DEFAULT_CONTRIBUTION_THRESHOLD,
        )

        scope = _saga_mutation_scope(auth_context, "saga_mark_contributions")
        thr = threshold if threshold is not None else DEFAULT_CONTRIBUTION_THRESHOLD

        def _do():
            conn = self._ensure_conn()
            atom_ids = [a.get("id") for a in retrieved_atoms if a.get("id")]
            if len(atom_ids) != len(retrieved_atoms) or len(set(atom_ids)) != len(
                atom_ids
            ):
                return None
            if _authorized_atom_ids(conn, atom_ids, scope) is None:
                return None
            return (
                _mc(
                    conn,
                    retrieved_atoms,
                    response_text,
                    session_id=session_id,
                    threshold=thr,
                )
                if atom_ids
                else None
            )

        result = await self._write_locked(_do)
        if result is None:
            return {
                "contributed_count": 0,
                "total": len(retrieved_atoms),
                "contribution_rate": 0.0,
                "contributed": [],
                "threshold": thr,
                "authorized": 0,
            }
        return {
            "contributed_count": len(result.contributed_atom_ids),
            "total": len(retrieved_atoms),
            "contribution_rate": result.contribution_rate,
            "contributed": result.contributed_atom_ids,
            "threshold": result.threshold,
            "authorized": len(retrieved_atoms),
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
