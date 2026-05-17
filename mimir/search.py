"""SQLite + saga-provider hybrid indexer (SPEC §6).

Recipe ported from muninnbot's ``scripts/state_search.py`` (PostgreSQL+pgvector
in source) → SQLite + FTS5 here so the benchmark container has no Postgres
dependency. Same hybrid score weights:

    score = 0.5 * cosine + 0.2 * fts_bm25 + 0.3 * recency

Embeddings route through saga's configured provider (voyage / openai /
fastembed / nvidia-nim) via ``SagaProviderEmbedder`` so file_search and
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

Recency fusion (chainlink #141 Slice 2, Option A)
-------------------------------------------------

When the third (ColBERT) channel fires, the fused ranking is the
sum of reciprocal-rank scores across BM25, dense, and ColBERT —
recency drops out of that score by design (it's a tie-breaker in
the legacy weighted-sum branch, not a relevance signal). Option A
adds a single optional post-RRF multiplier so recent files get a
small nudge above stale ones at the same content-relevance tier
**without** changing what each channel ranks. The shape:

    final_score_i = rrf_score_i × (1 + α × exp(-age_days_i / 30))

``α`` is read from ``Config.file_search_recency_fuse_alpha`` and
plumbed through the ``Indexer`` constructor as
``recency_fuse_alpha``. **Default 0.0** — when α=0 the multiplier
collapses to 1.0 and the path is short-circuited entirely, so the
fused ranking is byte-identical to PR #184 as shipped. Operators
opt in via ``MIMIR_FILE_SEARCH_RECENCY_FUSE_ALPHA=0.3`` (the value
we measured in ``state/spec/chainlink-141-slice2-ab-results.md``).
The no-ColBERT weighted-sum branch is untouched — it already folds
recency in via the ``W_RECENCY`` term.
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

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 150
SWEEP_INTERVAL_S = 60.0
RECENCY_HALF_LIFE_DAYS = 30.0
W_COSINE = 0.5
W_BM25 = 0.2
W_RECENCY = 0.3
# RRF (reciprocal rank fusion) constant. The standard k=60 from
# Cormack/Clarke/Buettcher 2009 — empirically robust across IR
# datasets and what every "RRF" reference uses. Only activated
# when the ColBERT channel is in play (chainlink #141 Slice 2);
# the no-ColBERT path still uses the weighted-sum scoring above
# to avoid regressing the existing two-channel behavior.
RRF_K = 60
# Mirrors saga's ``cached_embed_query`` LRU (saga/saga/embeddings.py:340).
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
    both surfaces. Picks up voyage / openai / fastembed / nvidia-nim
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


# Paths under state/ that look like operator/agent shared workspace, not
# knowledge worth retrieving via file_search. Embedding these is waste
# (frequent rewrites trigger reindexes) and pollution (results leak as
# "knowledge" hits). Per-deployment customization can come later via a
# <home>/.mimir/index-skip.txt; not needed for v0.4.
INDEX_SKIP_PATHS: frozenset[str] = frozenset(
    {
        "state/heartbeat-backlog.md",  # operator/agent shared todo
        "state/proposed-changes.md",  # pending HITL items
        "state/identities.yaml",  # operator config; not .md but defensive
    }
)
INDEX_SKIP_PREFIXES: tuple[str, ...] = (
    "state/social/",  # social-cli artifacts (FUTURE_WORK §10.1)
)


def _classify_scope(rel: str) -> str | None:
    if rel in INDEX_SKIP_PATHS:
        return None
    if any(rel.startswith(p) for p in INDEX_SKIP_PREFIXES):
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
        colbert_provider: "ColBERTChannel | None" = None,
        recency_fuse_alpha: float = 0.0,
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
        # chainlink #141 Slice 2: optional ColBERT third channel.
        # Default-lazy: ``_LazyColBERTChannel`` probes for the index
        # on disk on first ``.search()`` call and only imports
        # pylate then. Operators / tests can inject a different
        # provider (mock, prebuilt, disabled) by passing
        # ``colbert_provider`` explicitly.
        self._colbert: ColBERTChannel = colbert_provider \
            if colbert_provider is not None else _LazyColBERTChannel(home)
        # chainlink #141 Slice 2 Option A: optional post-RRF
        # recency multiplier. 0.0 (default) preserves the PR #184
        # fused ranking byte-for-byte; positive values multiply
        # each RRF score by (1 + alpha * exp(-age_days/30)) and
        # re-sort. Only applied on the ColBERT-fused branch — the
        # legacy weighted-sum branch already includes recency via
        # W_RECENCY. Negative alphas are clamped to 0.0 (no
        # behavior change) so a misconfiguration can't invert the
        # recency direction silently.
        self._recency_fuse_alpha = max(0.0, float(recency_fuse_alpha))
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
    ) -> list[SearchResult]:
        if not query.strip():
            return []
        # Cached embedding lookup — repeats within a turn (semantic +
        # keyword + variations) skip the ONNX call.
        query_vec_t = await asyncio.to_thread(self._embed_query, query)
        # chainlink #141 Slice 2: pull ColBERT hits off-loop on a
        # worker thread. The channel may no-op (returns []) when
        # the index isn't built or pylate isn't installed — that
        # path is intentionally indistinguishable from "ColBERT
        # ran but found nothing", so the downstream RRF/weighted
        # branch in ``_search_sync`` just sees an empty list.
        colbert_hits = await asyncio.to_thread(
            self._colbert.search, query, max(k, 10),
        )
        return await asyncio.to_thread(
            self._search_sync, query, list(query_vec_t), scope, k,
            candidate_pool, colbert_hits,
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
        conn = sqlite3.connect(self._db_path, isolation_level=None)
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
                if _classify_scope(rel) is None:
                    continue
                out.append(p)
        return sorted(out)

    def _reindex_sync(self, rel_path: str) -> bool:
        scope = _classify_scope(rel_path)
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
        colbert_hits: list["ColBERTHit"] | None = None,
    ) -> list[SearchResult]:
        # Sanitize FTS5 query — strip operators that would otherwise raise.
        fts_query = _to_fts_query(query)
        scope_filter = ""
        params: list = []
        if scope in ("memory", "state"):
            scope_filter = " AND f.scope = ?"
            params.append(scope)

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

        if not candidates and not colbert_hits:
            return []

        now = time.time()
        # Per-channel raw signals for every candidate. ``c["bm25"]``
        # is the FTS5 score (lower = better); ``cos`` is dot
        # product over normalized vectors when both are unit.
        cand_keys: list[tuple[str, int]] = []
        cand_payload: dict[tuple[str, int], dict] = {}
        for key, c in candidates.items():
            cos = _cosine(query_vec, c["embedding"])
            cand_keys.append(key)
            cand_payload[key] = {
                **c,
                "cos": cos,
                "recency": _recency(c["mtime"], now),
            }

        # When ColBERT didn't fire (no index / no extra installed),
        # preserve the legacy weighted-sum scoring exactly — no
        # behavior change for the default no-extra install.
        if not colbert_hits:
            scored: list[SearchResult] = []
            for key in cand_keys:
                c = cand_payload[key]
                cos_norm = max(0.0, c["cos"])
                bm25_norm = _bm25_norm(c["bm25"]) if c["bm25"] else 0.0
                score = (
                    W_COSINE * cos_norm
                    + W_BM25 * bm25_norm
                    + W_RECENCY * c["recency"]
                )
                scored.append(
                    SearchResult(
                        path=c["path"],
                        scope=c["scope"],
                        chunk_index=c["chunk_index"],
                        score=score,
                        cosine=c["cos"],
                        bm25=c["bm25"],
                        recency=c["recency"],
                        snippet=_make_snippet(c["content"], query),
                        description=c["description"],
                    )
                )
            scored.sort(key=lambda r: r.score, reverse=True)
            return scored[:k]

        # ColBERT is in play — fuse three channels via RRF.
        #
        # Ranks per channel:
        # - BM25 rank by ascending bm25 (lower = better in FTS5)
        # - Dense rank by descending cosine
        # - ColBERT rank by descending MaxSim
        #
        # RRF score = sum_channel 1 / (RRF_K + rank). All channels
        # are equal-weighted; weighting belongs in Slice 3's
        # measurement harness. Recency is folded in **post-fusion**
        # via the Option-A multiplier below (alpha=0.0 default → no
        # change, byte-identical to PR #184 as shipped). The
        # weighted-sum branch above still folds recency in via the
        # W_RECENCY term — different mechanism, same intent.
        bm25_ranked = sorted(
            (k_ for k_ in cand_keys if cand_payload[k_]["bm25"]),
            key=lambda k_: cand_payload[k_]["bm25"],  # ascending
        )
        dense_ranked = sorted(
            cand_keys, key=lambda k_: cand_payload[k_]["cos"], reverse=True,
        )

        # ColBERT hits keyed off (path, chunk_no). The colbert
        # chunker emits a different chunk granularity than mimir's
        # SQLite indexer (heading-aware vs character-stride). To
        # fuse rankings into the SQLite candidate set we collapse
        # ColBERT hits to the path level: take the best score per
        # path and fuse that against (path, chunk_index) by
        # matching on path. The single-vector channel still
        # surfaces the right chunk_index inside the path.
        colbert_by_path: dict[str, float] = {}
        for hit in colbert_hits:
            existing = colbert_by_path.get(hit.path)
            if existing is None or hit.score > existing:
                colbert_by_path[hit.path] = hit.score
        colbert_ranked_paths = sorted(
            colbert_by_path.keys(),
            key=lambda p: colbert_by_path[p],
            reverse=True,
        )
        # Map each candidate (path, chunk_index) → its path's
        # ColBERT rank (1-based). Candidates whose path isn't in
        # colbert_by_path get no ColBERT contribution.
        colbert_path_rank: dict[str, int] = {
            p: i + 1 for i, p in enumerate(colbert_ranked_paths)
        }

        # Pull in any ColBERT-only paths that the BM25+dense
        # candidate set missed entirely — RRF should reflect
        # ColBERT's recall, not just be a re-ranker. We synthesize
        # SQLite candidates from chunks rows on demand for those
        # paths so downstream snippet rendering works.
        missing_paths = [
            p for p in colbert_by_path
            if not any(k_[0] == p for k_ in cand_keys)
        ]
        if missing_paths:
            with self._db_lock, self._connect() as conn:
                placeholders = ",".join(["?"] * len(missing_paths))
                rows = conn.execute(
                    f"""
                    SELECT c.path, c.chunk_index, c.content, c.embedding,
                           f.mtime, f.scope, f.description
                      FROM chunks AS c
                      JOIN files AS f ON f.path = c.path
                     WHERE c.path IN ({placeholders})
                     ORDER BY c.path, c.chunk_index
                    """,
                    missing_paths,
                ).fetchall()
            # Take the first chunk of each missing path — ColBERT
            # ranks at the path level here, so any chunk is fine
            # for the snippet anchor.
            seen_path: set[str] = set()
            for row in rows:
                p = row[0]
                if p in seen_path:
                    continue
                seen_path.add(p)
                key = (row[0], row[1])
                cand_keys.append(key)
                cand_payload[key] = {
                    "path": row[0],
                    "chunk_index": row[1],
                    "content": row[2],
                    "embedding": _unpack_vec(row[3]),
                    "bm25": 0.0,
                    "mtime": row[4],
                    "scope": row[5],
                    "description": row[6],
                    "cos": _cosine(query_vec, _unpack_vec(row[3])),
                    "recency": _recency(row[4], now),
                }

        bm25_rank: dict[tuple[str, int], int] = {
            k_: i + 1 for i, k_ in enumerate(bm25_ranked)
        }
        dense_rank: dict[tuple[str, int], int] = {
            k_: i + 1 for i, k_ in enumerate(dense_ranked)
        }

        rrf_scored: list[SearchResult] = []
        alpha = self._recency_fuse_alpha
        for key in cand_keys:
            c = cand_payload[key]
            s = 0.0
            if key in bm25_rank:
                s += 1.0 / (RRF_K + bm25_rank[key])
            if key in dense_rank:
                s += 1.0 / (RRF_K + dense_rank[key])
            pr = colbert_path_rank.get(c["path"])
            if pr is not None:
                s += 1.0 / (RRF_K + pr)
            # Option A recency fuse: post-RRF multiplier. alpha=0.0
            # short-circuits to a no-op (no float drift, no sort
            # tie-break inversion) so the PR #184 behavior is
            # byte-identical when the flag is unset. ``c["recency"]``
            # is ``exp(-age_days/30)`` already (see ``_recency``)
            # so the formula is just ``score * (1 + alpha *
            # recency)``.
            if alpha > 0.0:
                s *= 1.0 + alpha * c["recency"]
            rrf_scored.append(
                SearchResult(
                    path=c["path"],
                    scope=c["scope"],
                    chunk_index=c["chunk_index"],
                    score=s,
                    cosine=c["cos"],
                    bm25=c["bm25"],
                    recency=c["recency"],
                    snippet=_make_snippet(c["content"], query),
                    description=c["description"],
                )
            )
        rrf_scored.sort(key=lambda r: r.score, reverse=True)
        return rrf_scored[:k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ColBERT third channel (chainlink #141 Slice 2)
# ---------------------------------------------------------------------------


@dataclass
class ColBERTHit:
    """Minimal channel-output shape: a path + chunk identity + score.

    Distinct from ``SearchResult`` because the ColBERT chunk
    granularity (heading-aware) doesn't match the SQLite indexer's
    character-stride chunks; we collapse to path-level for RRF
    fusion in ``_search_sync``.
    """
    path: str
    chunk_no: int
    score: float


class ColBERTChannel(Protocol):
    """Provider interface for the third RRF channel. The real
    implementation lives in ``mimir.colbert``; the search module
    only depends on this protocol so the colbert extra stays
    fully optional. Tests inject mock channels by providing
    something with the same ``.search()`` shape.
    """

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        ...


class _LazyColBERTChannel:
    """Default channel implementation. Probes for a built ColBERT
    index at ``<home>/.colbert-index/`` on first ``.search()`` call;
    silently returns ``[]`` if the index doesn't exist or pylate
    isn't installed. The cost-to-no-op is one stat()-equivalent
    check per query.
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._tried = False
        self._index: object | None = None

    def _ensure(self) -> object | None:
        if self._tried:
            return self._index
        self._tried = True
        try:
            from . import colbert as _colbert
        except ImportError:
            return None
        if not _colbert.index_available(self._home):
            return None
        try:
            self._index = _colbert.ColBERTIndex.open(_colbert.default_index_dir(self._home))
        except Exception:  # noqa: BLE001
            log.exception("colbert: failed to open index; disabling third channel")
            self._index = None
        return self._index

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        idx = self._ensure()
        if idx is None:
            return []
        try:
            raw = idx.search(query, k=k)  # type: ignore[attr-defined]
        except ImportError:
            return []
        except Exception:  # noqa: BLE001
            log.exception("colbert: search failed; falling back to two-channel")
            return []
        return [
            ColBERTHit(path=row[0].path, chunk_no=row[0].chunk_no, score=row[1])
            for row in raw
        ]


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
