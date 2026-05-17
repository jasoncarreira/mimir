"""LFM2-ColBERT-350M late-interaction retrieval (chainlink #141 Slice 2).

Optional third channel for ``file_search``. Loaded only when the
``colbert`` extra is installed AND a built index exists at
``<home>/.colbert-index/``. When either is missing, ``file_search``
falls back to its existing BM25 + dense pipeline with no change in
behavior — see ``mimir/search.py`` for the wiring.

Architecture:

- ``load_colbert_model()`` lazy-loads ``LiquidAI/LFM2-ColBERT-350M``
  via pylate's ``models.ColBERT`` wrapper. The LFM2 tokenizer ships
  without a ``pad_token``; we patch ``tokenizer.pad_token =
  tokenizer.eos_token`` right after init (one-liner that prevents
  the "Asking to pad but the tokenizer does not have a padding
  token" ValueError on first encode). Singleton cache — the 165MB
  model is loaded once per process.
- ``encode_chunks`` / ``encode_query`` return token-level
  embeddings (128-dim, max_seq_len=511 per recon).
- ``maxsim_score`` is the standard ColBERT late-interaction score:
  for each chunk, sum over query tokens of max over (masked) chunk
  tokens of the dot product. ~20 LOC of numpy.
- ``ColBERTIndex`` persists token-level vectors into a voyager-HNSW
  index plus a SQLite sidecar mapping chunk_id → (path, chunk_no,
  original_text). Pylate 1.2.0 lacks the PLAID indexer because
  fast-plaid has no aarch64 Linux wheel; voyager-HNSW is the
  closest thing pylate already pulls in transitively. ``search()``
  retrieves a candidate pool via HNSW KNN over query tokens and
  re-ranks with full MaxSim — the standard ColBERT lookup shape.

Storage shape (sidecar SQLite at ``.colbert-index/sidecar.db``):

  CREATE TABLE chunks (
      chunk_id    INTEGER PRIMARY KEY,
      path        TEXT NOT NULL,
      chunk_no    INTEGER NOT NULL,
      n_tokens    INTEGER NOT NULL,
      content     TEXT NOT NULL
  )

Token-level vectors live in ``embeddings.npy`` (fp16, shape
``(total_tokens, 128)``); ``offsets.npy`` is a ``(n_chunks, 2)``
int64 array of ``(start, length)`` slices into ``embeddings`` so
the per-chunk masked re-rank knows where each chunk lives.

The voyager index file (``hnsw.bin``) maps a per-TOKEN id back to a
chunk_id via a parallel ``token_to_chunk.npy`` int64 array. Query
tokens find their nearest corpus tokens; we dedupe to candidate
chunks and re-rank with full MaxSim on the masked chunk vectors.

Chunking rules by directory (see chunking.py docstring or
chainlink-141-pylate-recon.md §"Open questions"):

- ``state/wiki/``, ``memory/issues/``, ``memory/core/``,
  ``state/spec/``: chunk on markdown ``##`` heading boundaries.
  Sections exceeding 511 tokens slide-window inside the section.
- ``state/raw/``: sliding window 400 tokens, 50 overlap.
- Everything else: same heading rule (default).

The whole module is import-safe without the ``colbert`` extra —
loading pylate is deferred to ``load_colbert_model`` and
``ColBERTIndex.open``. ``mimir/search.py`` calls
``index_available(home)`` for cheap probing.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

MODEL_NAME = "LiquidAI/LFM2-ColBERT-350M"
EMBED_DIM = 128
MAX_SEQ_LEN = 511  # pylate cap; tokens beyond this are silently truncated
DEFAULT_BATCH_SIZE = 4

# RRF candidate-pool size when re-ranking from voyager-HNSW. A modest
# multiple of the requested k keeps recall while bounded the MaxSim
# cost (~k_pool × q_tokens × chunk_tokens × dim multiplies). 64 is
# what pylate's defaults shake out to in practice for k=10 queries.
DEFAULT_CANDIDATE_POOL = 64

# Per-token query neighbors to pull from voyager before deduping to
# the chunk-level candidate set. Higher = more recall, more compute.
DEFAULT_KNN_PER_TOKEN = 16

# ---------------------------------------------------------------------------
# Model singleton
# ---------------------------------------------------------------------------

_MODEL_SINGLETON: Any = None


def load_colbert_model(device: str = "cpu") -> Any:
    """Load LFM2-ColBERT-350M via pylate, patch pad_token, warm encode.

    Singleton-cached: subsequent calls return the same instance. The
    165MB model load is ~1.8s warm / ~41s cold (first download). The
    pad_token patch is non-negotiable — without it the first encode
    raises ``ValueError: Asking to pad but the tokenizer does not
    have a padding token``. See
    ``memory/issues/pylate-lfm2-aarch64-install-gotchas.md`` §3.
    """
    global _MODEL_SINGLETON
    if _MODEL_SINGLETON is not None:
        return _MODEL_SINGLETON

    try:
        from pylate import models  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "pylate not installed. Install the ColBERT extra: "
            '`pip install -e ".[colbert]"`'
        ) from exc

    t0 = time.time()
    model = models.ColBERT(model_name_or_path=MODEL_NAME, device=device)
    # LFM2 tokenizer ships without pad_token. Without this line the
    # first call to ``encode_documents`` raises ValueError. Pad
    # tokens are only used for attention-masking, not content —
    # using eos_token here is safe and is the fix the HF error
    # message itself recommends.
    if getattr(model.tokenizer, "pad_token", None) is None:
        model.tokenizer.pad_token = model.tokenizer.eos_token
    # Warm: encode a sentinel so subsequent latency reflects the
    # steady-state (first ONNX call lazy-builds the graph).
    try:
        model.encode(["warmup"], is_query=False, show_progress_bar=False)
    except Exception:  # noqa: BLE001 — warm-up failures shouldn't block load
        log.exception("colbert warmup encode failed; continuing without warm")
    log.info("colbert: loaded %s in %.1fs", MODEL_NAME, time.time() - t0)
    _MODEL_SINGLETON = model
    return model


def reset_model_singleton() -> None:
    """Test-only hook to drop the cached model (e.g. between tests)."""
    global _MODEL_SINGLETON
    _MODEL_SINGLETON = None


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def encode_chunks(
    model: Any,
    chunks: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
):
    """Encode a list of chunks to padded token embeddings + lengths.

    Returns a tuple ``(embeddings, lengths)`` where:

    - ``embeddings`` is a float32 numpy array of shape
      ``(n_chunks, max_seq_len, EMBED_DIM)`` — zero-padded so the
      caller doesn't have to deal with ragged tensors.
    - ``lengths`` is an int64 numpy array of shape ``(n_chunks,)``
      with the unpadded token count per chunk. MaxSim must mask
      out the padding positions; ``maxsim_score`` does this using
      ``lengths``.

    The token cap is ``MAX_SEQ_LEN`` (511). Longer chunks are
    silently truncated by pylate. Caller is responsible for
    chunking the corpus before this call.
    """
    import numpy as np

    if not chunks:
        return (
            np.zeros((0, MAX_SEQ_LEN, EMBED_DIM), dtype=np.float32),
            np.zeros((0,), dtype=np.int64),
        )

    # pylate's encode returns a list of (seq_len, dim) arrays per
    # input — variable-length, since sequences pad/truncate
    # independently per document. We pad to MAX_SEQ_LEN here.
    raw = model.encode(
        chunks,
        is_query=False,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    n = len(raw)
    out = np.zeros((n, MAX_SEQ_LEN, EMBED_DIM), dtype=np.float32)
    lens = np.zeros((n,), dtype=np.int64)
    for i, arr in enumerate(raw):
        arr = np.asarray(arr, dtype=np.float32)
        # Defensive — sequences should never exceed MAX_SEQ_LEN but
        # if pylate's truncation behavior changes upstream, this
        # clip prevents an array-shape blow-up.
        seq_len = min(arr.shape[0], MAX_SEQ_LEN)
        out[i, :seq_len] = arr[:seq_len]
        lens[i] = seq_len
    return out, lens


def encode_query(model: Any, query: str):
    """Encode a single query string to (1, q_tokens, EMBED_DIM)."""
    import numpy as np

    raw = model.encode(
        [query],
        is_query=True,
        batch_size=1,
        show_progress_bar=False,
    )
    arr = np.asarray(raw[0], dtype=np.float32)
    return arr[None, :, :]  # shape (1, q_tokens, dim)


def maxsim_score(query_emb, chunk_embs, chunk_lens):
    """Standard ColBERT MaxSim late-interaction score.

    For each chunk, score = sum over query tokens of max over
    (unmasked) chunk tokens of <q_tok, c_tok>. Returns a
    ``(n_chunks,)`` float32 array.

    Shapes:
    - ``query_emb``: ``(1, q_tokens, dim)`` or ``(q_tokens, dim)``.
    - ``chunk_embs``: ``(n_chunks, max_seq_len, dim)``.
    - ``chunk_lens``: ``(n_chunks,)`` int — unpadded length per chunk.
    """
    import numpy as np

    q = np.asarray(query_emb, dtype=np.float32)
    if q.ndim == 3:
        # (1, q_tokens, dim) → (q_tokens, dim)
        q = q[0]
    c = np.asarray(chunk_embs, dtype=np.float32)
    lens = np.asarray(chunk_lens, dtype=np.int64)

    n_chunks = c.shape[0]
    max_len = c.shape[1]
    if n_chunks == 0:
        return np.zeros((0,), dtype=np.float32)

    # Build a (n_chunks, max_len) boolean mask of valid positions.
    pos = np.arange(max_len)[None, :]  # (1, max_len)
    valid = pos < lens[:, None]  # (n_chunks, max_len)

    # Dot products: (n_chunks, max_len, dim) @ (dim, q_tokens)
    # → (n_chunks, max_len, q_tokens)
    dots = c @ q.T

    # Mask padded positions to -inf so they never win the max.
    neg_inf = np.float32(-1.0e30)
    dots = np.where(valid[:, :, None], dots, neg_inf)

    # Max over chunk tokens (axis=1) → (n_chunks, q_tokens)
    per_q = dots.max(axis=1)

    # If a chunk has zero valid tokens, per_q is all -inf — replace
    # with 0 to keep the sum meaningful (no signal rather than
    # poisoning).
    per_q = np.where(per_q > neg_inf / 2, per_q, np.float32(0.0))

    # Sum over query tokens → (n_chunks,)
    return per_q.sum(axis=1).astype(np.float32)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


# Rough whitespace-token approximator: word-ish runs. Good enough for
# the per-section "does this exceed 511 tokens?" check; the model's
# real tokenizer is invoked during encode and the cap is enforced by
# pylate's truncation anyway.
_TOKEN_RE = re.compile(r"\S+")


def _approx_token_count(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def _sliding_window_token(text: str, window: int, overlap: int) -> list[str]:
    """Sliding-window split by whitespace tokens. Returns chunks of
    up to ``window`` tokens with ``overlap``-token overlap."""
    toks = _TOKEN_RE.findall(text)
    if not toks:
        return []
    if len(toks) <= window:
        return [text.strip()] if text.strip() else []
    if overlap >= window:
        raise ValueError("overlap must be smaller than window")
    step = window - overlap
    out: list[str] = []
    i = 0
    while i < len(toks):
        out.append(" ".join(toks[i : i + window]))
        i += step
    return out


def _heading_chunks(
    text: str, max_tokens: int = MAX_SEQ_LEN, overlap: int = 50
) -> list[str]:
    """Split markdown by ``##`` (level-2) headings. Each section is
    a chunk; sections exceeding ``max_tokens`` slide-window split.

    Material above the first ``##`` (e.g. front-matter + intro) is
    its own chunk. Empty trailing chunks are dropped.
    """
    lines = text.splitlines(keepends=True)
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    out: list[str] = []
    for sec in sections:
        body = "".join(sec).strip()
        if not body:
            continue
        if _approx_token_count(body) <= max_tokens:
            out.append(body)
        else:
            # Section too big — slide-window inside it. ~50-token
            # overlap matches the recon spec.
            out.extend(_sliding_window_token(body, max_tokens, overlap))
    return out


# Directory prefixes that get the heading rule. Anything else under
# state/ uses the sliding-window rule (raw extracts often lack clean
# headings; trying to honor them yields tiny noisy chunks).
_HEADING_PREFIXES: tuple[str, ...] = (
    "state/wiki/",
    "state/spec/",
    "memory/issues/",
    "memory/core/",
)
# Sliding-window rule explicitly for raw extracts.
_SLIDING_PREFIXES: tuple[str, ...] = (
    "state/raw/",
)

# Sliding-window defaults per the spec.
SLIDING_WINDOW_TOKENS = 400
SLIDING_OVERLAP_TOKENS = 50


def chunk_file(rel_path: str, text: str) -> list[str]:
    """Apply the directory-routed chunking rule.

    - Heading rule (level-2 ``##``) for state/wiki, state/spec,
      memory/issues, memory/core. Oversize sections slide-window
      with 50-token overlap.
    - Sliding window 400 tokens / 50 overlap for state/raw.
    - Default (other paths under memory/ or state/): heading rule,
      same shape as the wiki/issues/core bucket.
    """
    if any(rel_path.startswith(p) for p in _SLIDING_PREFIXES):
        return _sliding_window_token(text, SLIDING_WINDOW_TOKENS, SLIDING_OVERLAP_TOKENS)
    # Heading rule covers everything else (explicit heading prefixes
    # plus the catch-all default).
    return _heading_chunks(text, max_tokens=MAX_SEQ_LEN, overlap=SLIDING_OVERLAP_TOKENS)


# ---------------------------------------------------------------------------
# Corpus walk
# ---------------------------------------------------------------------------


# Mirror search.py's INDEX_SKIP_PATHS / INDEX_SKIP_PREFIXES so the
# ColBERT index doesn't pick up operator-shared workspace files
# either. Kept as a separate definition (not imported from search.py)
# to keep import surface minimal — the colbert module is opt-in and
# shouldn't pull search.py's transitive deps just for two frozensets.
_INDEX_SKIP_PATHS: frozenset[str] = frozenset(
    {
        "state/heartbeat-backlog.md",
        "state/proposed-changes.md",
        "state/identities.yaml",
    }
)
_INDEX_SKIP_PREFIXES: tuple[str, ...] = ("state/social/",)


def walk_corpus(home: Path) -> Iterator[Path]:
    """Walk memory/ + state/ for indexable markdown files.

    Filters mirror ``mimir/search.py``'s _classify_scope. Excludes
    ``INDEX.md`` files (auto-generated) and the operator-shared
    workspace files in _INDEX_SKIP_*.
    """
    for root_name in ("memory", "state"):
        root = home / root_name
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(home).as_posix()
            except ValueError:
                continue
            if p.name == "INDEX.md" or p.name == "index.md":
                continue
            if rel in _INDEX_SKIP_PATHS:
                continue
            if any(rel.startswith(pref) for pref in _INDEX_SKIP_PREFIXES):
                continue
            yield p


# ---------------------------------------------------------------------------
# Index storage
# ---------------------------------------------------------------------------


@dataclass
class ColBERTChunk:
    chunk_id: int
    path: str
    chunk_no: int
    content: str


_SCHEMA_SIDECAR = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    chunk_no    INTEGER NOT NULL,
    n_tokens    INTEGER NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
"""


def default_index_dir(home: Path) -> Path:
    """Standard location for the ColBERT sidecar index. Bind-mount-
    stable under MIMIR_HOME so a docker restart preserves it."""
    return home / ".colbert-index"


def index_available(home: Path) -> bool:
    """Cheap probe: does a built ColBERT index exist at the standard
    location? Used by ``search.py`` to decide whether to fuse the
    third channel without paying the pylate-import cost.
    """
    d = default_index_dir(home)
    if not d.is_dir():
        return False
    return (
        (d / "sidecar.db").is_file()
        and (d / "embeddings.npy").is_file()
        and (d / "offsets.npy").is_file()
        and (d / "token_to_chunk.npy").is_file()
        and (d / "hnsw.bin").is_file()
    )


class ColBERTIndex:
    """voyager-HNSW + numpy sidecar index over token-level vectors.

    pylate 1.2.0 lacks the PLAID indexer (fast-plaid has no aarch64
    Linux wheel). voyager is a transitive dep of pylate; we re-use
    it for KNN over token-level vectors and re-rank candidates with
    full MaxSim — the standard ColBERT-with-vanilla-ANN lookup.

    File layout under ``index_dir``:
    - ``sidecar.db``       SQLite (chunk_id → path/chunk_no/content)
    - ``embeddings.npy``   (total_tokens, EMBED_DIM) float16
    - ``offsets.npy``      (n_chunks, 2) int64 — (start, n_tokens)
    - ``token_to_chunk.npy`` (total_tokens,) int64 — chunk_id per token
    - ``hnsw.bin``         voyager binary

    Build with ``build_from_corpus``; load existing index with
    ``open``. Query with ``search``.
    """

    def __init__(self, index_dir: Path) -> None:
        self.index_dir = index_dir
        self._db: sqlite3.Connection | None = None
        self._voyager_index: Any = None
        self._embeddings: Any = None  # np.ndarray (total_tokens, dim) fp16
        self._offsets: Any = None  # np.ndarray (n_chunks, 2) int64
        self._token_to_chunk: Any = None  # np.ndarray (total_tokens,) int64

    # -- lifecycle --

    @classmethod
    def open(cls, index_dir: Path) -> "ColBERTIndex":
        """Open an existing on-disk index. Lazy-loads the heavy
        arrays on first query."""
        if not index_dir.is_dir():
            raise FileNotFoundError(f"ColBERT index dir not found: {index_dir}")
        return cls(index_dir)

    def _ensure_loaded(self) -> None:
        if self._db is not None:
            return
        import numpy as np

        try:
            from voyager import Index, Space  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "voyager not installed. The colbert extra installs it "
                "transitively via pylate."
            ) from exc

        self._db = sqlite3.connect(str(self.index_dir / "sidecar.db"))
        self._embeddings = np.load(self.index_dir / "embeddings.npy")
        self._offsets = np.load(self.index_dir / "offsets.npy")
        self._token_to_chunk = np.load(self.index_dir / "token_to_chunk.npy")
        # Cosine via inner-product (vectors are L2-normalized by pylate).
        self._voyager_index = Index.load(
            str(self.index_dir / "hnsw.bin"),
            space=Space.InnerProduct,
            num_dimensions=EMBED_DIM,
        )

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # -- build --

    @classmethod
    def build_from_corpus(
        cls,
        home: Path,
        index_dir: Path | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_every: int = 25,
        progress_cb=None,
    ) -> "ColBERTIndex":
        """One-shot rebuild: walk corpus, chunk, encode, persist.

        Idempotent in the sense that re-running blows the directory
        away and rebuilds from scratch — v1 has no incremental
        update path (see chainlink-141 spec §"Re-encode cadence").

        ``progress_cb(n_chunks_done, n_chunks_total)`` is called
        every ``progress_every`` chunks; default falls back to
        log.info emission so an operator running interactively sees
        throughput.
        """
        import numpy as np

        try:
            from voyager import Index, Space  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "voyager not installed. Install the ColBERT extra: "
                '`pip install -e ".[colbert]"`'
            ) from exc

        if index_dir is None:
            index_dir = default_index_dir(home)
        index_dir = Path(index_dir)
        # Wipe previous index — v1 is rebuild-from-scratch only.
        if index_dir.exists():
            for p in index_dir.iterdir():
                if p.is_file():
                    p.unlink()
        index_dir.mkdir(parents=True, exist_ok=True)

        model = load_colbert_model()

        # Pass 1: walk + chunk; gather (path, chunk_no, content).
        log.info("colbert build: walking corpus under %s", home)
        records: list[tuple[str, int, str]] = []
        for p in walk_corpus(home):
            rel = p.relative_to(home).as_posix()
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            chunks = chunk_file(rel, text)
            for i, ch in enumerate(chunks):
                records.append((rel, i, ch))
        n_chunks = len(records)
        log.info("colbert build: %d files → %d chunks", _count_paths(records), n_chunks)
        if progress_cb:
            progress_cb(0, n_chunks)

        # Pass 2: encode in batches. Streaming-append-style — we
        # don't hold all chunk embeddings in RAM simultaneously
        # beyond a batch + the accumulator that becomes
        # embeddings.npy. At ~165MB total this fits, but the
        # streaming shape generalizes if the corpus grows.
        all_tokens: list[Any] = []
        offsets: list[tuple[int, int]] = []
        token_to_chunk: list[int] = []
        cursor = 0
        n_done = 0
        last_progress_log = time.time()
        for i in range(0, n_chunks, batch_size):
            batch = records[i : i + batch_size]
            texts = [r[2] for r in batch]
            embs, lens = encode_chunks(model, texts, batch_size=batch_size)
            for j, (emb, length) in enumerate(zip(embs, lens)):
                chunk_id = i + j  # 0-indexed; matches DB primary key
                length_i = int(length)
                # Trim padding before persisting — we don't need
                # zero-vectors on disk.
                tokens = emb[:length_i].astype(np.float16)
                all_tokens.append(tokens)
                offsets.append((cursor, length_i))
                token_to_chunk.extend([chunk_id] * length_i)
                cursor += length_i
            n_done += len(batch)
            if progress_cb and (
                n_done % progress_every == 0 or n_done >= n_chunks
            ):
                progress_cb(n_done, n_chunks)
            elif (time.time() - last_progress_log) > 30.0:
                # Default emission path when no callback given.
                log.info(
                    "colbert build: %d/%d chunks encoded (%.0f%%)",
                    n_done, n_chunks, 100 * n_done / max(n_chunks, 1),
                )
                last_progress_log = time.time()

        if all_tokens:
            embeddings = np.concatenate(all_tokens, axis=0)
        else:
            embeddings = np.zeros((0, EMBED_DIM), dtype=np.float16)
        offsets_arr = np.array(offsets, dtype=np.int64) if offsets else \
            np.zeros((0, 2), dtype=np.int64)
        token_to_chunk_arr = np.array(token_to_chunk, dtype=np.int64) \
            if token_to_chunk else np.zeros((0,), dtype=np.int64)

        np.save(index_dir / "embeddings.npy", embeddings)
        np.save(index_dir / "offsets.npy", offsets_arr)
        np.save(index_dir / "token_to_chunk.npy", token_to_chunk_arr)

        # Build HNSW. Cosine via InnerProduct since pylate emits
        # L2-normalized vectors; inner-product is then equivalent to
        # cosine and ~30% faster in voyager.
        log.info("colbert build: building HNSW over %d tokens", embeddings.shape[0])
        hnsw = Index(
            space=Space.InnerProduct,
            num_dimensions=EMBED_DIM,
            M=16,
            ef_construction=200,
        )
        if embeddings.shape[0] > 0:
            # voyager expects float32 — promote from fp16 storage
            # for the index build but keep on-disk vectors fp16.
            hnsw.add_items(embeddings.astype(np.float32))
        hnsw.save(str(index_dir / "hnsw.bin"))

        # Sidecar SQLite for chunk metadata.
        db_path = index_dir / "sidecar.db"
        if db_path.exists():
            db_path.unlink()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_SCHEMA_SIDECAR)
            conn.executemany(
                "INSERT INTO chunks (chunk_id, path, chunk_no, n_tokens, content) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (i, r[0], r[1], int(offsets_arr[i, 1]) if i < len(offsets_arr) else 0, r[2])
                    for i, r in enumerate(records)
                ],
            )
            conn.commit()
        finally:
            conn.close()

        log.info("colbert build: complete — %d chunks indexed at %s", n_chunks, index_dir)
        return cls(index_dir)

    # -- search --

    def search(
        self,
        query: str,
        k: int = 10,
        candidate_pool: int = DEFAULT_CANDIDATE_POOL,
        knn_per_token: int = DEFAULT_KNN_PER_TOKEN,
    ) -> list[tuple[ColBERTChunk, float]]:
        """KNN-then-MaxSim. Returns ``[(ColBERTChunk, score), ...]``
        sorted by descending MaxSim score, length up to ``k``.

        - Encode the query → ``(q_tokens, dim)``.
        - For each query token, voyager KNN → top ``knn_per_token``
          token ids. Dedupe by chunk_id to a candidate pool.
        - For each candidate chunk, gather its token slice from
          ``embeddings.npy`` and run full MaxSim against the query.
        - Return the top-k by MaxSim.
        """
        import numpy as np

        self._ensure_loaded()
        model = load_colbert_model()
        q_emb = encode_query(model, query)[0]  # (q_tokens, dim)
        if q_emb.shape[0] == 0 or self._embeddings is None or \
                self._embeddings.shape[0] == 0:
            return []

        # voyager.query expects float32.
        q32 = q_emb.astype(np.float32)
        # voyager.query takes a single vector OR a batch — use batch
        # form for fewer round-trips.
        try:
            neighbors, _dists = self._voyager_index.query(  # type: ignore[union-attr]
                q32, k=min(knn_per_token, self._embeddings.shape[0]),
            )
        except Exception:  # noqa: BLE001
            log.exception("voyager query failed")
            return []
        # neighbors shape (q_tokens, knn_per_token)
        token_ids = np.asarray(neighbors, dtype=np.int64).reshape(-1)
        chunk_ids = np.unique(self._token_to_chunk[token_ids])  # type: ignore[index]
        if chunk_ids.size == 0:
            return []
        # Cap candidate pool — keep highest-coverage chunks. Without
        # a coverage signal, just take the first ``candidate_pool``
        # unique chunk ids (np.unique returns sorted).
        if chunk_ids.size > candidate_pool:
            chunk_ids = chunk_ids[:candidate_pool]

        # Gather + pad candidate token vectors. Build a
        # (n_candidates, max_seq_len, dim) array masked by per-chunk
        # length to feed maxsim_score.
        n_cand = chunk_ids.size
        cand = np.zeros((n_cand, MAX_SEQ_LEN, EMBED_DIM), dtype=np.float32)
        lens = np.zeros((n_cand,), dtype=np.int64)
        for i, cid in enumerate(chunk_ids):
            start, length = self._offsets[int(cid)]  # type: ignore[index]
            length = min(int(length), MAX_SEQ_LEN)
            cand[i, :length] = self._embeddings[start : start + length].astype(  # type: ignore[index]
                np.float32
            )
            lens[i] = length

        scores = maxsim_score(q32, cand, lens)

        # Top-k by score.
        order = np.argsort(-scores)[:k]
        out: list[tuple[ColBERTChunk, float]] = []
        for idx in order:
            cid = int(chunk_ids[idx])
            row = self._lookup_chunk(cid)
            if row is not None:
                out.append((row, float(scores[idx])))
        return out

    def _lookup_chunk(self, chunk_id: int) -> ColBERTChunk | None:
        assert self._db is not None
        row = self._db.execute(
            "SELECT chunk_id, path, chunk_no, content FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return ColBERTChunk(
            chunk_id=row[0], path=row[1], chunk_no=row[2], content=row[3],
        )


def _count_paths(records: list[tuple[str, int, str]]) -> int:
    return len({r[0] for r in records})


# ---------------------------------------------------------------------------
# CLI dispatch (registered by mimir/cli.py)
# ---------------------------------------------------------------------------


def add_argparse(p) -> None:
    """Attach ``mimir colbert build`` flags."""
    import argparse  # noqa: F401 — type hint only

    sub = p.add_subparsers(dest="colbert_action")

    build_p = sub.add_parser(
        "build",
        help=(
            "One-shot build of the ColBERT sidecar index over memory/ + "
            "state/. Idempotent (rebuilds from scratch); estimate ~13min "
            "on aarch64 CPU per chainlink-141 recon."
        ),
    )
    build_p.add_argument(
        "--home", type=Path, default=None,
        help="MIMIR_HOME (default: read from env or cwd).",
    )
    build_p.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"Encode batch size (default: {DEFAULT_BATCH_SIZE}).",
    )
    build_p.add_argument(
        "--progress-every", type=int, default=25,
        help="Emit a progress line every N chunks (default: 25).",
    )


def dispatch(args) -> int:
    """Called from mimir.cli for the ``colbert`` subcommand."""
    import os

    action = getattr(args, "colbert_action", None)
    if action != "build":
        print("usage: mimir colbert build [--home PATH] [--batch-size N]")
        return 1

    home = args.home
    if home is None:
        home_env = os.environ.get("MIMIR_HOME")
        home = Path(home_env) if home_env else Path.cwd()
    home = Path(home).resolve()

    started = time.time()

    def _progress(done: int, total: int) -> None:
        pct = 100.0 * done / max(total, 1)
        elapsed = time.time() - started
        if done == 0:
            print(f"colbert build: starting, {total} chunks to encode")
            return
        eta = elapsed * (total - done) / max(done, 1)
        print(
            f"colbert build: {done}/{total} chunks ({pct:.0f}%) "
            f"elapsed={elapsed:.0f}s eta={eta:.0f}s"
        )

    try:
        ColBERTIndex.build_from_corpus(
            home=home,
            batch_size=args.batch_size,
            progress_every=args.progress_every,
            progress_cb=_progress,
        )
    except ImportError as exc:
        print(f"colbert build: {exc}", flush=True)
        return 2
    elapsed = time.time() - started
    print(f"colbert build: done in {elapsed:.1f}s")
    return 0
