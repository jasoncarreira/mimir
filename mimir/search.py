"""SQLite + saga-provider hybrid indexer (SPEC §6).

Adapted from a PostgreSQL+pgvector hybrid-search recipe → SQLite + FTS5
here so the container has no Postgres dependency. Hybrid score weights:

    score = 0.5 * cosine + 0.2 * fts_bm25 + 0.3 * recency

Embeddings route through saga's configured provider (voyage / openai /
fastembed) via ``SagaProviderEmbedder`` so file_search and
saga atoms share one vector space. Tests pass ``HashEmbedder`` to stay
offline; ``FastEmbedder`` is kept as a back-compat / pin-to-bge-small
option but is no longer the default.

Lifecycle:
- ``start()`` — create schema, dim-mismatch check, run an initial mtime
  sweep, kick off the 60s background sweep loop.
- File writes call ``enqueue_path()`` (non-blocking) → indexer thread reads,
  embeds, writes. The ``flush()`` helper waits for the queue to drain in tests.
- ``search()`` returns ranked ``SearchResult`` rows.
- ``stop()`` cancels the sweep loop and lets the worker drain.

The ``Embedder`` interface is split so tests can plug in a deterministic fake
without paying the cold-start cost.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .core_blocks import describe_file
from .index_skip import INDEX_SKIP_PATHS, INDEX_SKIP_PREFIXES, is_index_skipped

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 150
SWEEP_INTERVAL_S = 60.0
RECENCY_HALF_LIFE_DAYS = 30.0
W_COSINE = 0.5
W_BM25 = 0.2
W_RECENCY = 0.3
# Mirrors saga's ``cached_embed_query`` LRU (``mimir/saga/embeddings.py``).
# A single turn issues a handful of ``file_search`` calls and benchmarks
# replay the same queries; 64 is plenty without bloating worker memory.
EMBED_QUERY_CACHE_SIZE = 64


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    dim: int

    def embed(
        self, texts: list[str], input_type: str = "passage",
    ) -> list[list[float]]:
        """Embed a batch of texts.

        ``input_type`` distinguishes document/passage embedding (the
        default — what you call when indexing file content) from query
        embedding (what you call to embed a search string). Providers
        like Voyage produce DIFFERENT vectors for the two — passing the
        right input_type at query time is a material recall win.
        FastEmbedder and HashEmbedder ignore it (no asymmetric query/
        doc model); SagaProviderEmbedder routes it through saga's
        provider chain.
        """


class FastEmbedder:
    """Wraps ``fastembed.TextEmbedding`` directly. Lazy-loads the ONNX
    model on first use.

    Kept as a back-compat / test-fallback for the rare case where
    callers want to pin file_search to bge-small specifically without
    going through saga's provider chain. The Indexer's default
    embedder is now ``SagaProviderEmbedder`` — file_search and saga
    atoms share one embedding provider so the cosine spaces align.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", dim: int = 384) -> None:
        self._model_name = model_name
        self.dim = dim
        self._impl = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._impl is None:
            with self._lock:
                if self._impl is None:
                    from fastembed import TextEmbedding

                    self._impl = TextEmbedding(model_name=self._model_name)

    def embed(
        self, texts: list[str], input_type: str = "passage",
    ) -> list[list[float]]:
        # ``input_type`` ignored — fastembed's bge-small treats query
        # and passage embeddings identically at this entry point.
        del input_type
        if not texts:
            return []
        self._ensure()
        # ``list(v)`` on a numpy array yields np.float32 scalars — coerce to
        # Python floats here so cosines, scores, and json.dumps all stay clean.
        return [[float(x) for x in v] for v in self._impl.embed(texts)]  # type: ignore[union-attr]


class SagaProviderEmbedder:
    """Routes file_search embeddings through saga's configured provider.

    Default Indexer embedder. Same provider as saga atoms — one model
    download, one cosine space, one config knob in saga.toml drives
    both surfaces. Picks up voyage / openai / fastembed
    based on the ``[embedding] provider`` in the active saga.toml.

    Lazy initialization: defer the provider instantiation until first
    ``embed()`` call so Indexer construction stays cheap (matches
    FastEmbedder's lazy-load semantics). ``dim`` resolves at the first
    embed too (saga's provider only knows its dimension after model
    config is read).

    For tests that want offline / no-API behavior, pass ``HashEmbedder``
    explicitly to the Indexer constructor instead.
    """

    def __init__(self) -> None:
        self._provider = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._provider is None:
            with self._lock:
                if self._provider is None:
                    from .saga.embeddings import get_provider

                    provider = get_provider()
                    self._dim = provider.dimensions()
                    # Assign provider LAST so concurrent readers see a
                    # fully-initialized object (Python attribute writes
                    # are atomic under the GIL, but we want both fields
                    # visible together).
                    self._provider = provider

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._ensure()
        return self._dim  # type: ignore[return-value]

    def embed(
        self, texts: list[str], input_type: str = "passage",
    ) -> list[list[float]]:
        if not texts:
            return []
        self._ensure()
        # Forward ``input_type`` to saga's provider. For voyage this
        # becomes the "document" or "query" instruction prefix — a
        # real recall win at query time. For OpenAI the parameter is
        # dropped (unknown to OpenAI's API). For fastembed/onnx the
        # BGE-specific query_embed entry point is used when
        # input_type="query".
        vecs = self._provider.batch_embed(texts, input_type=input_type)  # type: ignore[union-attr]
        return [[float(x) for x in v] for v in vecs]


class HashEmbedder:
    """Deterministic fake for tests — pseudo-embeddings derived from the text's
    SHA-256 digest, normalized to unit length. Same text → same vector;
    different texts produce different vectors with reasonable spread."""

    dim = 16

    def embed(
        self, texts: list[str], input_type: str = "passage",
    ) -> list[list[float]]:
        # ``input_type`` ignored — hash-derived fake doesn't model
        # query/doc asymmetry.
        del input_type
        import hashlib

        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            vec = [(b / 127.5) - 1.0 for b in h[: self.dim]]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Sliding-window character chunks. Empty input → empty list."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    if overlap >= size:
        raise ValueError("overlap must be smaller than chunk size")
    out: list[str] = []
    step = size - overlap
    i = 0
    while i < len(text):
        out.append(text[i : i + size])
        i += step
    return out


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS files (
        path        TEXT PRIMARY KEY,
        scope       TEXT NOT NULL,
        mtime       REAL NOT NULL,
        size        INTEGER NOT NULL,
        chunk_count INTEGER NOT NULL,
        description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        path        TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        content     TEXT NOT NULL,
        embedding   BLOB NOT NULL,
        PRIMARY KEY (path, chunk_index),
        FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        path UNINDEXED,
        chunk_index UNINDEXED,
        content,
        tokenize = 'porter unicode61'
    )
    """,
]


# ---------------------------------------------------------------------------
# Search result
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    path: str
    scope: str
    chunk_index: int
    score: float
    cosine: float
    bm25: float
    recency: float
    snippet: str
    description: str | None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "scope": self.scope,
            "chunk_index": self.chunk_index,
            "score": round(float(self.score), 4),
            "cosine": round(float(self.cosine), 4),
            "bm25": round(float(self.bm25), 4),
            "recency": round(float(self.recency), 4),
            "snippet": self.snippet,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _recency(mtime: float, now: float) -> float:
    age_days = max(0.0, (now - mtime) / 86400.0)
    return math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)


def _bm25_norm(raw: float) -> float:
    """SQLite returns a negative bm25 score; lower is better. Map to (0, 1]."""
    return 1.0 / (1.0 + abs(raw))

def _classify_scope(rel: str, home: Path | None = None) -> str | None:
    if is_index_skipped(rel, home):
        return None
    if rel.startswith("memory/"):
        if rel.startswith("memory/core/") or rel == "memory/INDEX.md":
            return None
        return "memory"
    if rel.startswith("state/"):
        if rel == "state/INDEX.md":
            return None
        return "state"
    return None


@dataclass
class IndexerStats:
    files: int = 0
    chunks: int = 0
    last_full_reindex: float | None = None
    last_sweep: float | None = None


class Indexer:
    """Owns the SQLite db + embedder. All blocking work runs in a worker
    thread via ``asyncio.to_thread`` so the event loop never stalls."""

    def __init__(
        self,
        home: Path,
        embedder: Embedder | None = None,
        db_path: Path | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self._home = home
        # Default routes through saga's configured provider so file_search
        # embeddings live in the same cosine space as saga atoms — one
        # ``[embedding] provider`` setting drives both surfaces. Tests
        # pin ``HashEmbedder`` explicitly to stay offline.
        self._embedder: Embedder = embedder or SagaProviderEmbedder()
        self._db_path = db_path or (home / ".mimir" / "index.db")
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._db_lock = threading.Lock()
        self._sweep_task: asyncio.Task | None = None
        self._closed = False
        # Per-instance LRU for query embeddings — file_search call patterns
        # repeat the same query string several times per turn (mirror of
        # saga's ``cached_embed_query``). Wrapping the bound method here
        # gives each Indexer its own cache without leaking across tests.
        self._embed_query = functools.lru_cache(maxsize=EMBED_QUERY_CACHE_SIZE)(
            self._embed_query_uncached
        )

    # ---- lifecycle ----

    def init_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()

    def _check_dim_mismatch(self) -> None:
        """Detect existing chunks whose embedding dim doesn't match the
        current embedder's dim. After an operator switches providers
        (e.g. fastembed bge-small at 384d → voyage at 1024d), existing
        BLOBs are in the OLD vector space — ``_cosine`` returns 0.0 on
        the length mismatch, so semantic search silently degrades to
        BM25-only with no signal to the operator.

        Log a loud warning here pointing at ``mimir reindex --target
        files --apply`` so the next operator hitting a stale file_search
        index gets immediate diagnostic visibility instead of debugging
        why retrieval looks wrong. Best-effort: errors during the probe
        are swallowed (no DB existing yet on first boot, transient lock,
        etc.).
        """
        try:
            expected = self._embedder.dim * 4
        except Exception:  # noqa: BLE001 — embedder.dim init may fail
            return
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT length(embedding) FROM chunks LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return
        if row is None:
            return  # empty index, nothing to mismatch
        actual = row[0]
        if actual != expected:
            log.warning(
                "file_search index dim mismatch: chunks stored at %d "
                "bytes/embedding (%dd) but current embedder expects %d "
                "bytes (%dd). Semantic search will return cosine=0 for "
                "every chunk until you migrate: "
                "`mimir reindex --target files --apply`",
                actual, actual // 4, expected, self._embedder.dim,
            )

    async def start(self, run_initial_sweep: bool = True, sweep_loop: bool = True) -> None:
        await asyncio.to_thread(self.init_schema)
        await asyncio.to_thread(self._check_dim_mismatch)
        if run_initial_sweep:
            await self.sweep()
        if sweep_loop:
            self._sweep_task = asyncio.create_task(self._sweep_loop(), name="mimir-indexer-sweep")

    async def stop(self) -> None:
        self._closed = True
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

    async def _sweep_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(SWEEP_INTERVAL_S)
                if self._closed:
                    return
                try:
                    await self.sweep()
                except Exception:  # noqa: BLE001
                    log.exception("indexer sweep failed")
        except asyncio.CancelledError:
            return

    # ---- public ops ----

    async def reindex_path(self, rel_path: str) -> bool:
        """Reindex a single file by relative path. Returns True if it indexed,
        False if the file is out of scope or missing."""
        return await asyncio.to_thread(self._reindex_sync, rel_path)

    async def sweep(self) -> dict[str, int]:
        """Walk memory/ + state/, reindex any drift; remove db rows for
        deleted files. Returns a small stats dict."""
        return await asyncio.to_thread(self._sweep_sync)

    async def search(
        self,
        query: str,
        scope: str = "all",
        k: int = 5,
        candidate_pool: int = 50,
        *,
        path_prefix: str | None = None,
        semantic_weight: float | None = None,
        keyword_weight: float | None = None,
        recency_weight: float | None = None,
    ) -> list[SearchResult]:
        if not query.strip():
            return []
        # Resolve + validate weights eagerly — before the embed.
        # Negative-weight typos error out without paying the ONNX cost
        # of an embed call we'll never use.
        w_cos = W_COSINE if semantic_weight is None else float(semantic_weight)
        w_bm25 = W_BM25 if keyword_weight is None else float(keyword_weight)
        w_rec = W_RECENCY if recency_weight is None else float(recency_weight)
        if w_cos < 0 or w_bm25 < 0 or w_rec < 0:
            raise ValueError(
                "file_search weights must be non-negative "
                f"(semantic={w_cos}, keyword={w_bm25}, recency={w_rec})"
            )
        # Cached embedding lookup — repeats within a turn (semantic +
        # keyword + variations) skip the ONNX call.
        query_vec_t = await asyncio.to_thread(self._embed_query, query)
        return await asyncio.to_thread(
            self._search_sync, query, list(query_vec_t),
            scope, k, candidate_pool,
            path_prefix, w_cos, w_bm25, w_rec,
        )

    def _embed_query_uncached(self, text: str) -> tuple[float, ...]:
        """Single-query embed; tuple return is hashable + immutable so the
        LRU value can't be mutated by callers.

        Passes ``input_type="query"`` so providers that distinguish
        query vs document embeddings (voyage, BGE) produce the right
        vector for the retrieval-side cosine.
        """
        return tuple(self._embedder.embed([text], input_type="query")[0])

    async def stats(self) -> IndexerStats:
        return await asyncio.to_thread(self._stats_sync)

    # ---- sync internals ----

    def _connect(self) -> sqlite3.Connection:
        # Re-create the parent dir if it was removed out-of-band — a
        # benchmark cleanup or test-fixture rm doesn't crash the sweep loop;
        # next sweep just starts from an empty schema.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep sqlite's transaction manager enabled.  Passing
        # ``isolation_level=None`` puts the connection in autocommit mode,
        # where ``with conn:`` does not roll back partially-applied DML on
        # exception/crash.  Reindex and sweep updates must be all-or-nothing
        # so ``files``, ``chunks``, and ``chunks_fts`` cannot diverge.
        conn = sqlite3.connect(self._db_path, isolation_level="DEFERRED")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _stats_sync(self) -> IndexerStats:
        with self._connect() as conn:
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return IndexerStats(files=files, chunks=chunks)

    def _abs_path(self, rel: str) -> Path:
        return self._home / rel

    def _resolve_rel(self, abs_path: Path) -> str | None:
        try:
            rel = abs_path.relative_to(self._home).as_posix()
        except ValueError:
            return None
        return rel

    def _walk_indexable(self) -> list[Path]:
        roots = [self._home / "memory", self._home / "state"]
        out: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for p in root.rglob("*.md"):
                if not p.is_file():
                    continue
                rel = self._resolve_rel(p)
                if rel is None:
                    continue
                if _classify_scope(rel, self._home) is None:
                    continue
                out.append(p)
        return sorted(out)

    def _reindex_sync(self, rel_path: str) -> bool:
        scope = _classify_scope(rel_path, self._home)
        if scope is None:
            return False
        abs_path = self._abs_path(rel_path)
        if not abs_path.is_file():
            # File deleted — drop from index.
            # CR2 (memory & retrieval) fix: explicit DELETE on
            # ``chunks`` too. Pre-fix this branch deleted from
            # ``files`` and ``chunks_fts`` only, relying on
            # ``ON DELETE CASCADE`` from ``files(path)`` to clean up
            # ``chunks``. The cascade DOES work today (FK support is
            # on per ``_connect`` PRAGMA), but the *update* branch
            # below explicitly deletes from ``chunks`` + ``chunks_fts``
            # — inconsistent. If FK support ever flips off (the PRAGMA
            # is per-connection and easy to drop in a refactor), the
            # delete branch silently leaks orphan chunks rows. Belt-
            # and-suspenders: be explicit.
            with self._db_lock, self._connect() as conn:
                conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks_fts WHERE path = ?", (rel_path,))
            return False
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError:
            return False
        stat = abs_path.stat()
        chunks = chunk_text(text, self._chunk_size, self._chunk_overlap)
        embeddings: list[list[float]] = []
        if chunks:
            embeddings = self._embedder.embed(chunks)
        desc, _ = describe_file(text)
        with self._db_lock, self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM chunks_fts WHERE path = ?", (rel_path,))
            conn.execute(
                "INSERT OR REPLACE INTO files (path, scope, mtime, size, chunk_count, description)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (rel_path, scope, stat.st_mtime, stat.st_size, len(chunks), desc),
            )
            for i, (chunk, vec) in enumerate(zip(chunks, embeddings)):
                conn.execute(
                    "INSERT INTO chunks (path, chunk_index, content, embedding) VALUES (?, ?, ?, ?)",
                    (rel_path, i, chunk, _pack_vec(vec)),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (path, chunk_index, content) VALUES (?, ?, ?)",
                    (rel_path, i, chunk),
                )
        return True

    def _sweep_sync(self) -> dict[str, int]:
        on_disk: dict[str, float] = {}
        for p in self._walk_indexable():
            rel = self._resolve_rel(p)
            if rel is None:
                continue
            try:
                on_disk[rel] = p.stat().st_mtime
            except OSError:
                continue

        with self._db_lock, self._connect() as conn:
            indexed_rows = conn.execute("SELECT path, mtime FROM files").fetchall()
        indexed = {row[0]: row[1] for row in indexed_rows}

        added = 0
        updated = 0
        removed = 0

        # Add or update
        for rel, mtime in on_disk.items():
            if rel not in indexed:
                if self._reindex_sync(rel):
                    added += 1
            elif mtime > indexed[rel] + 1e-6:
                if self._reindex_sync(rel):
                    updated += 1

        # Remove deletions — same explicit-DELETE pattern as the
        # delete branch in ``_reindex_sync`` (CR2 memory & retrieval
        # fix). Don't rely on FK cascade silently keeping ``chunks``
        # in sync.
        for rel in list(indexed.keys()):
            if rel not in on_disk:
                with self._db_lock, self._connect() as conn:
                    conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM chunks_fts WHERE path = ?", (rel,))
                removed += 1

        return {"added": added, "updated": updated, "removed": removed}

    def _search_sync(
        self,
        query: str,
        query_vec: list[float],
        scope: str,
        k: int,
        candidate_pool: int,
        path_prefix: str | None = None,
        w_cos: float = W_COSINE,
        w_bm25: float = W_BM25,
        w_rec: float = W_RECENCY,
    ) -> list[SearchResult]:
        # Weight resolve + validate happens in ``search()`` — by the
        # time we reach this method the floats are already finalized.
        # Sanitize FTS5 query — strip operators that would otherwise raise.
        fts_query = _to_fts_query(query)
        scope_filter = ""
        params: list = []
        if scope in ("memory", "state"):
            scope_filter = " AND f.scope = ?"
            params.append(scope)
        # Finer-grained filter: anchor results to a subdirectory under
        # the chosen scope (e.g. ``state/journal``). Composes with
        # ``scope`` rather than replacing it — passing both is fine.
        # LIKE-escapes the prefix so wildcard characters in path names
        # don't accidentally match extra files.
        if path_prefix:
            normalized = path_prefix.strip().rstrip("/")
            if normalized:
                escaped = (
                    normalized.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                scope_filter += " AND f.path LIKE ? ESCAPE '\\'"
                params.append(f"{escaped}/%")

        candidates: dict[tuple[str, int], dict] = {}

        with self._db_lock, self._connect() as conn:
            # 1) FTS5 candidates by BM25.
            if fts_query:
                rows = conn.execute(
                    f"""
                    SELECT c.path, c.chunk_index, c.content, c.embedding,
                           bm25(chunks_fts) AS bm25, f.mtime, f.scope, f.description
                      FROM chunks_fts
                      JOIN chunks AS c
                        ON c.path = chunks_fts.path AND c.chunk_index = chunks_fts.chunk_index
                      JOIN files AS f
                        ON f.path = c.path
                     WHERE chunks_fts MATCH ?{scope_filter}
                     ORDER BY bm25 ASC
                     LIMIT ?
                    """,
                    [fts_query, *params, candidate_pool],
                ).fetchall()
                for row in rows:
                    key = (row[0], row[1])
                    candidates[key] = {
                        "path": row[0],
                        "chunk_index": row[1],
                        "content": row[2],
                        "embedding": _unpack_vec(row[3]),
                        "bm25": row[4],
                        "mtime": row[5],
                        "scope": row[6],
                        "description": row[7],
                    }

            # 2) Fill remaining pool with cosine candidates from any chunk —
            #    important when FTS5 misses paraphrased queries.
            if len(candidates) < candidate_pool:
                exclude_clause = ""
                exclude_params: list = []
                if candidates:
                    placeholders = ",".join(["(?, ?)"] * len(candidates))
                    exclude_clause = (
                        f" AND (c.path, c.chunk_index) NOT IN ({placeholders})"
                    )
                    for key in candidates.keys():
                        exclude_params.extend(key)
                more_rows = conn.execute(
                    f"""
                    SELECT c.path, c.chunk_index, c.content, c.embedding,
                           f.mtime, f.scope, f.description
                      FROM chunks AS c
                      JOIN files AS f ON f.path = c.path
                     WHERE 1=1{scope_filter}{exclude_clause}
                     ORDER BY f.mtime DESC, c.path, c.chunk_index
                     LIMIT ?
                    """,
                    [*params, *exclude_params, candidate_pool - len(candidates)],
                ).fetchall()
                for row in more_rows:
                    key = (row[0], row[1])
                    candidates[key] = {
                        "path": row[0],
                        "chunk_index": row[1],
                        "content": row[2],
                        "embedding": _unpack_vec(row[3]),
                        "bm25": 0.0,
                        "mtime": row[4],
                        "scope": row[5],
                        "description": row[6],
                    }

        if not candidates:
            return []

        now = time.time()
        scored: list[SearchResult] = []
        for c in candidates.values():
            cos = _cosine(query_vec, c["embedding"])
            cos_norm = max(0.0, cos)
            bm25_norm = _bm25_norm(c["bm25"]) if c["bm25"] else 0.0
            recency = _recency(c["mtime"], now)
            score = w_cos * cos_norm + w_bm25 * bm25_norm + w_rec * recency
            snippet = _make_snippet(c["content"], query)
            scored.append(
                SearchResult(
                    path=c["path"],
                    scope=c["scope"],
                    chunk_index=c["chunk_index"],
                    score=score,
                    cosine=cos,
                    bm25=c["bm25"],
                    recency=recency,
                    snippet=snippet,
                    description=c["description"],
                )
            )

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_fts_query(text: str) -> str:
    """Sanitize a free-form query for FTS5 MATCH. Operators and parens are
    stripped; tokens are OR-joined. Empty input → empty string.

    Dashes are dropped from tokens (not just trimmed) because FTS5
    parses ``-attention`` as the unary NOT operator on the
    ``attention`` column — and chunks_fts has no such column, so
    the query raises ``sqlite3.OperationalError: no such column:
    attention`` and the whole search blows up. This pre-existed
    the chainlink #141 Slice 2 A/B harness; the harness just
    surfaced it by including probes with hyphenated phrases like
    "operator-attention" and "context-1m-2025-08-07". Underscores
    are still safe (they're treated as part of the term by FTS5
    tokenizers) so identifier-shaped tokens like ``file_search``
    survive intact.
    """
    safe: list[str] = []
    for tok in text.split():
        # Replace `-` with space FIRST so a single hyphenated token
        # like ``operator-attention`` splits into two tokens. Then
        # drop any chars beyond alnum + `_`.
        normalized = tok.replace("-", " ")
        for sub in normalized.split():
            clean = "".join(ch for ch in sub if ch.isalnum() or ch == "_")
            if clean:
                safe.append(clean)
    return " OR ".join(safe)


def _make_snippet(text: str, query: str, window: int = 200) -> str:
    """Cheap snippet: first window chars, or window centered around the first
    keyword hit. Avoids invoking FTS5's snippet() across joins."""
    if not text:
        return ""
    needle = query.split()[0].lower() if query.strip() else ""
    if needle:
        idx = text.lower().find(needle)
        if idx >= 0:
            half = window // 2
            start = max(0, idx - half)
            end = min(len(text), start + window)
            snip = text[start:end]
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return prefix + snip.strip() + suffix
    return text[:window].strip() + ("…" if len(text) > window else "")
