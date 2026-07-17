"""FAISS-backed approximate nearest-neighbor search for mimir.saga.

Reads vectors from the ``embeddings`` table (one row per atom) joined
to ``atoms`` filtered on ``tombstoned=0``. No state-machine column;
the only retrieval-time exclusion is the tombstone bit. Compare with
saga's ``vector_index.py`` which filters on ``state IN ('active',
'fading')`` — the new schema has no such states by design.

Lifecycle:

- ``VectorIndex(dimension=N)`` — construct empty.
- ``build_from_db(conn)`` — bulk-load all live embeddings into a FAISS
  index. Picks IndexFlatIP (exact) for <50k vectors, IndexIVFFlat
  (approximate) above that.
- ``add(atom_id, vec_bytes)`` — incremental add after each ``store()``.
  Called from the SagaStore hook, not from store.py itself
  (store stays pure-SQL).
- ``remove(atom_id)`` — soft-mark a position as removed; filtered out
  of search results. Triggers full rebuild when >10% of positions are
  marked removed.
- ``search(query_vec, top_k)`` — returns ``[(atom_id, similarity)]``.

Per-SagaStore singleton (not module-global). Two SagaStores
pointing at different DBs each own their own index. Saga's module-
global singletons assumed one process / one DB; the bench harness
needs cross-question DB switching, so per-client is the right scope.

faiss-cpu is an optional dependency. If unavailable, ``search``
returns ``[]`` and the recall path falls through to FTS5 only.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import numpy as np


logger = logging.getLogger("mimir.saga.vector_index")

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.info(
        "faiss-cpu not installed — mimir.saga.vector_index falls back to empty search. "
        "Install faiss-cpu for ANN candidates; FTS5 still works without it."
    )


# Threshold to switch from exact (IndexFlatIP) to approximate (IndexIVFFlat).
# Matches saga's default. Below 50k vectors, exact is fast enough on CPU.
APPROX_THRESHOLD = 50_000


class VectorIndex:
    """FAISS index keyed on atom_id. Thread-safe via an internal lock —
    incremental adds from store() can race against searches from the
    agent's retrieval path, and the bench harness rebuilds the index
    mid-run when it switches per-question DBs.
    """

    def __init__(self, dimension: int = 1024):
        self.dimension = dimension
        self._index = None
        self._id_to_pos: dict[str, int] = {}
        self._pos_to_id: dict[int, str] = {}
        self._removed: set[int] = set()
        self._next_pos = 0
        self._lock = threading.Lock()
        self._built = False

    @property
    def total_vectors(self) -> int:
        return self._next_pos - len(self._removed)

    @property
    def built(self) -> bool:
        return self._built

    def build_from_db(self, conn: sqlite3.Connection) -> None:
        """Bulk-load live atom embeddings into a fresh FAISS index.

        Live = ``atoms.tombstoned = 0``. Joins ``embeddings`` on
        ``atom_id``; atoms without an embedding row (shouldn't happen
        in steady state — store() inserts both atomically — but defensive)
        are silently skipped.
        """
        if not FAISS_AVAILABLE:
            self._built = True
            return

        rows = conn.execute("""
            SELECT a.id, e.vec, e.dim
            FROM atoms a
            JOIN embeddings e ON e.atom_id = a.id
            WHERE a.tombstoned = 0
        """).fetchall()

        if not rows:
            with self._lock:
                self._index = None
                self._id_to_pos.clear()
                self._pos_to_id.clear()
                self._removed.clear()
                self._next_pos = 0
                self._built = True
            return

        ids: list[str] = []
        vecs: list[np.ndarray] = []
        for atom_id, blob, dim in rows:
            if blob is None or dim is None or dim != self.dimension:
                # Mismatched dim — likely a provider switch mid-stream.
                # Skip; a re-embedding pass would re-add these.
                continue
            if len(blob) < self.dimension * 4:
                continue
            vec = np.frombuffer(blob, dtype=np.float32).copy()
            if vec.shape[0] != self.dimension:
                continue
            ids.append(atom_id)
            vecs.append(vec)

        if not vecs:
            with self._lock:
                self._index = None
                self._built = True
            return

        matrix = np.vstack(vecs).astype(np.float32)
        # Cosine similarity via normalized inner product.
        faiss.normalize_L2(matrix)
        n = len(vecs)

        with self._lock:
            if n < APPROX_THRESHOLD:
                self._index = faiss.IndexFlatIP(self.dimension)
            else:
                nlist = min(int(np.sqrt(n)), 256)
                quantizer = faiss.IndexFlatIP(self.dimension)
                self._index = faiss.IndexIVFFlat(
                    quantizer, self.dimension, nlist,
                    faiss.METRIC_INNER_PRODUCT,
                )
                self._index.train(matrix)
                self._index.nprobe = min(nlist // 4, 16)

            self._index.add(matrix)
            self._id_to_pos = {}
            self._pos_to_id = {}
            for i, atom_id in enumerate(ids):
                self._id_to_pos[atom_id] = i
                self._pos_to_id[i] = atom_id
            self._next_pos = n
            self._removed.clear()
            self._built = True

        logger.info(
            "VectorIndex built: %d vectors, dim=%d, type=%s",
            n, self.dimension,
            "IVFFlat" if n >= APPROX_THRESHOLD else "FlatIP",
        )

    def build_from_sessions(self, conn: sqlite3.Connection) -> None:
        """Bulk-load session embeddings from the sessions table.

        Sessions without an embedding (NULL embedding column) or with a
        mismatched dimension are silently skipped — they remain reachable
        via the recency pathway in search_sessions().
        """
        if not FAISS_AVAILABLE:
            self._built = True
            return

        rows = conn.execute("""
            SELECT id, embedding, embedding_dim
            FROM sessions
            WHERE embedding IS NOT NULL
        """).fetchall()

        ids: list[str] = []
        vecs: list[np.ndarray] = []
        for sess_id, blob, dim in rows:
            if blob is None or dim is None or dim != self.dimension:
                continue
            if len(blob) < self.dimension * 4:
                continue
            vec = np.frombuffer(blob, dtype=np.float32).copy()
            if vec.shape[0] != self.dimension:
                continue
            ids.append(sess_id)
            vecs.append(vec)

        if not vecs:
            with self._lock:
                self._index = None
                self._id_to_pos.clear()
                self._pos_to_id.clear()
                self._removed.clear()
                self._next_pos = 0
                self._built = True
            return

        matrix = np.vstack(vecs).astype(np.float32)
        faiss.normalize_L2(matrix)
        n = len(vecs)

        with self._lock:
            # Sessions are always small — IndexFlatIP is fast enough forever.
            self._index = faiss.IndexFlatIP(self.dimension)
            self._index.add(matrix)
            self._id_to_pos = {}
            self._pos_to_id = {}
            for i, sid in enumerate(ids):
                self._id_to_pos[sid] = i
                self._pos_to_id[i] = sid
            self._next_pos = n
            self._removed.clear()
            self._built = True

        logger.info(
            "Sessions VectorIndex built: %d vectors, dim=%d", n, self.dimension,
        )

    def add(self, atom_id: str, vec_bytes: bytes) -> None:
        """Incremental add after a successful store. No-op if the
        index hasn't been built yet — the next build_from_db will pick
        up the atom from disk."""
        if not FAISS_AVAILABLE or self._index is None:
            return
        if len(vec_bytes) < self.dimension * 4:
            return
        vec = np.frombuffer(vec_bytes, dtype=np.float32).copy().reshape(1, -1)
        if vec.shape[1] != self.dimension:
            return
        faiss.normalize_L2(vec)

        with self._lock:
            # chainlink #235: if this atom_id was added before (re-embed,
            # calibration, future re-add path), mark the OLD position as
            # removed BEFORE overwriting ``_id_to_pos``. Otherwise the prior
            # vector remains queryable via FAISS but is unreachable from
            # ``_id_to_pos`` — search() returns the stale-vector entry
            # mapped back to this atom_id with the old content's similarity.
            # Today's call sites only re-add on cold rebuild; this guard
            # preserves the invariant for any future re-embed surface.
            old_pos = self._id_to_pos.get(atom_id)
            if old_pos is not None:
                self._removed.add(old_pos)
                self._pos_to_id.pop(old_pos, None)
            self._index.add(vec)
            pos = self._next_pos
            self._id_to_pos[atom_id] = pos
            self._pos_to_id[pos] = atom_id
            self._next_pos += 1

    def remove(self, atom_id: str) -> None:
        """Mark an atom's position as removed. Subsequent searches
        filter it out; full rebuild scheduled when >10% removed."""
        with self._lock:
            pos = self._id_to_pos.pop(atom_id, None)
            if pos is not None:
                self._removed.add(pos)
                self._pos_to_id.pop(pos, None)

    def search(self, query_vec, top_k: int = 20) -> list[tuple[str, float]]:
        """Return up to ``top_k`` ``(atom_id, cosine_similarity)`` matches.

        Empty list if the index is missing, empty, or FAISS unavailable —
        callers must be tolerant (recall.py treats empty FAISS results
        as "no semantic candidates" and falls back to FTS5).
        """
        if not FAISS_AVAILABLE:
            return []

        q = np.array(query_vec, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(q)

        # chainlink #319: hold the lock for the WHOLE read. The emptiness
        # check, the soft-removed count, the FAISS search, AND the
        # _removed / _pos_to_id post-processing all touch state that add() /
        # remove() mutate under this same lock. Pre-fix only the .search()
        # call was guarded, so a concurrent add/remove could race the
        # pre-check or the position→id mapping (stale/torn reads, missing
        # ids). The post-processing is O(top_k) so the hold stays short.
        out: list[tuple[str, float]] = []
        with self._lock:
            if self._index is None or self._index.ntotal == 0:
                return []
            # Over-fetch to account for soft-removed entries.
            search_k = min(top_k + len(self._removed) + 10, self._index.ntotal)
            scores, indices = self._index.search(q, search_k)
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:
                    continue
                if idx in self._removed:
                    continue
                aid = self._pos_to_id.get(idx)
                if aid is None:
                    continue
                out.append((aid, float(score)))
                if len(out) >= top_k:
                    break
        return out

    def rebuild_if_needed(self, conn: sqlite3.Connection) -> bool:
        """Rebuild from disk if accumulated removals exceed 10% of total."""
        if self._next_pos > 0 and len(self._removed) > self._next_pos * 0.1:
            self.build_from_db(conn)
            return True
        return False


def faiss_search_atoms(
    index: VectorIndex | None, query_vec, top_k: int = 20,
) -> list[tuple[str, float]]:
    """Module-level convenience used by recall — takes an index handle
    instead of looking one up. Returns empty list if the index is None
    (FAISS unavailable or client opted out)."""
    if index is None:
        return []
    return index.search(query_vec, top_k=top_k)
