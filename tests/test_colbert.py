"""Unit tests for mimir.colbert (chainlink #141 Slice 2).

The real LFM2-ColBERT-350M weights are ~165MB on disk plus a 700MB
safetensors download on first run — way too expensive for CI. These
tests mock the model with a deterministic dummy encoder that returns
hash-derived embeddings of the right shape. The numpy-only pieces
(``maxsim_score``, chunking, sidecar plumbing) get exercised
directly; the pylate boundary is tested via the mock.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from mimir import colbert as mc


# ---------------------------------------------------------------------------
# Fake encoder — deterministic, fast, no network
# ---------------------------------------------------------------------------


class _FakeColBERTModel:
    """Stand-in for pylate.models.ColBERT in tests. Tokenizes on
    whitespace, emits 128-d hash-derived unit vectors per token.
    Mirrors the (n_chunks, seq_len, dim) ragged-list shape pylate's
    real ``.encode()`` returns.
    """

    def __init__(self) -> None:
        self.tokenizer = mock.MagicMock()
        self.tokenizer.pad_token = "<pad>"
        self.tokenizer.eos_token = "<eos>"

    def encode(self, texts, is_query=False, batch_size=4, show_progress_bar=False):
        outs = []
        for t in texts:
            toks = (t or "").split()[:mc.MAX_SEQ_LEN]
            if not toks:
                outs.append(np.zeros((1, mc.EMBED_DIM), dtype=np.float32))
                continue
            rows = []
            for tok in toks:
                h = hashlib.sha256(tok.encode("utf-8")).digest()
                # 32 bytes of sha256 → tile to exactly EMBED_DIM
                # bytes, then read as uint8 → float32. Yields a
                # deterministic 128-d vector per token.
                buf = (h * ((mc.EMBED_DIM // len(h)) + 1))[: mc.EMBED_DIM]
                vec = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
                # Normalize for the cosine-via-IP equivalence pylate gives us.
                vec = vec - vec.mean()
                n = np.linalg.norm(vec) or 1.0
                rows.append(vec / n)
            outs.append(np.stack(rows, axis=0).astype(np.float32))
        return outs


@pytest.fixture
def fake_model():
    mc.reset_model_singleton()
    m = _FakeColBERTModel()
    with mock.patch.object(mc, "load_colbert_model", return_value=m):
        yield m
    mc.reset_model_singleton()


# ---------------------------------------------------------------------------
# encode_chunks
# ---------------------------------------------------------------------------


def test_encode_chunks_shapes(fake_model):
    chunks = ["hello world", "foo bar baz quux", "x"]
    embs, lens = mc.encode_chunks(fake_model, chunks, batch_size=2)
    assert embs.shape == (3, mc.MAX_SEQ_LEN, mc.EMBED_DIM)
    assert lens.tolist() == [2, 4, 1]
    # padding rows are zero
    assert np.allclose(embs[0, 2:], 0.0)
    assert np.allclose(embs[1, 4:], 0.0)


def test_encode_chunks_empty_input(fake_model):
    embs, lens = mc.encode_chunks(fake_model, [])
    assert embs.shape == (0, mc.MAX_SEQ_LEN, mc.EMBED_DIM)
    assert lens.shape == (0,)


def test_encode_query_shape(fake_model):
    q = mc.encode_query(fake_model, "what is quantum entanglement")
    # (1, q_tokens, dim) — 4 whitespace tokens in the query
    assert q.shape == (1, 4, mc.EMBED_DIM)


# ---------------------------------------------------------------------------
# maxsim_score
# ---------------------------------------------------------------------------


def test_maxsim_score_handcrafted():
    # 2 query tokens, 2 chunks, 3 chunk tokens each.
    # Construct so that:
    # - chunk 0 has token 0 perfectly aligned with q_token 0,
    #   and token 2 perfectly aligned with q_token 1 → score = 2.0
    # - chunk 1 is mediocre everywhere → lower score.
    q = np.array([
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    ], dtype=np.float32)
    c = np.array([
        [[1.0, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 1.0, 0.0]],  # perfect alignment
        [[0.5, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 0.5, 0.0]],  # half magnitude
    ], dtype=np.float32)
    lens = np.array([3, 3], dtype=np.int64)
    scores = mc.maxsim_score(q, c, lens)
    assert scores.shape == (2,)
    assert scores[0] == pytest.approx(2.0, abs=1e-6)
    assert scores[0] > scores[1]


def test_maxsim_score_respects_mask():
    # Same query as above; in chunk_b only the padded positions are
    # perfect — mask must zero them out so chunk_a (real tokens) wins.
    q = np.array([[1.0, 0.0]], dtype=np.float32)
    c = np.array([
        [[0.6, 0.0], [0.0, 0.0], [0.0, 0.0]],   # only token 0 valid
        [[0.1, 0.0], [1.0, 0.0], [1.0, 0.0]],   # padded "perfect" tokens
    ], dtype=np.float32)
    lens = np.array([1, 1], dtype=np.int64)
    scores = mc.maxsim_score(q, c, lens)
    # chunk_a's best valid is 0.6; chunk_b's best valid is 0.1 (only
    # position 0 is unmasked).
    assert scores[0] == pytest.approx(0.6, abs=1e-6)
    assert scores[1] == pytest.approx(0.1, abs=1e-6)


def test_maxsim_score_empty_chunks():
    q = np.array([[[1.0, 0.0]]], dtype=np.float32)
    c = np.zeros((0, 5, 2), dtype=np.float32)
    lens = np.zeros((0,), dtype=np.int64)
    out = mc.maxsim_score(q, c, lens)
    assert out.shape == (0,)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunk_file_heading_rule():
    text = (
        "# Title\n\nIntro paragraph.\n\n"
        "## Section A\n\nBody of A.\n\n"
        "## Section B\n\nBody of B.\n"
    )
    chunks = mc.chunk_file("state/wiki/topics/foo.md", text)
    # Intro is its own chunk, then one per ## section.
    assert len(chunks) == 3
    assert chunks[0].startswith("# Title")
    assert "Section A" in chunks[1]
    assert "Section B" in chunks[2]


def test_chunk_file_oversize_section_slides():
    # Build a section that exceeds MAX_SEQ_LEN tokens.
    big = "word " * (mc.MAX_SEQ_LEN + 100)
    text = f"## big section\n{big}"
    chunks = mc.chunk_file("memory/issues/foo.md", text)
    assert len(chunks) >= 2
    # Each sub-chunk fits the cap (approximate whitespace counter).
    for c in chunks:
        assert mc._approx_token_count(c) <= mc.MAX_SEQ_LEN


def test_chunk_file_raw_uses_sliding_window():
    text = " ".join(["word"] * 1000)
    chunks = mc.chunk_file("state/raw/extract.md", text)
    # Sliding 400 / 50 → step 350 → ceil(1000/350) = 3 chunks
    assert len(chunks) == 3
    assert mc._approx_token_count(chunks[0]) == mc.SLIDING_WINDOW_TOKENS


def test_chunk_file_default_is_heading_rule():
    text = "## A\nbody a\n## B\nbody b"
    out = mc.chunk_file("memory/notes/foo.md", text)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# walk_corpus
# ---------------------------------------------------------------------------


def test_walk_corpus_skips_index_and_socials(tmp_path):
    home = tmp_path
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "state" / "wiki").mkdir(parents=True)
    (home / "state" / "social").mkdir(parents=True)
    (home / "memory" / "topics" / "a.md").write_text("a")
    (home / "memory" / "INDEX.md").write_text("auto")
    (home / "state" / "wiki" / "b.md").write_text("b")
    (home / "state" / "INDEX.md").write_text("auto")
    (home / "state" / "social" / "c.md").write_text("c")  # skipped
    (home / "state" / "heartbeat-backlog.md").write_text("hb")  # skipped

    found = sorted(p.name for p in mc.walk_corpus(home))
    assert found == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# ColBERTIndex (build → open → search round-trip)
# ---------------------------------------------------------------------------


@pytest.fixture
def voyager_or_skip():
    """Skip the index-build round-trip if voyager isn't installed —
    these tests are useful when running with the colbert extra but
    shouldn't fail the default CI matrix."""
    try:
        import voyager  # noqa: F401
    except ImportError:
        pytest.skip("voyager not installed; colbert extra absent")


def test_build_and_search_roundtrip(tmp_path, fake_model, voyager_or_skip):
    home = tmp_path
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "memory" / "topics" / "quantum.md").write_text(
        "## quantum\nquantum mechanics describes nature at atomic scales"
    )
    (home / "memory" / "topics" / "flocking.md").write_text(
        "## boids\nboids flocking simulation by Craig Reynolds with three rules"
    )

    idx = mc.ColBERTIndex.build_from_corpus(home=home, batch_size=2)
    assert mc.index_available(home)

    # Query that should retrieve quantum, not boids.
    hits = idx.search("quantum mechanics nature", k=5)
    assert hits, "expected at least one hit"
    paths = [h[0].path for h in hits]
    assert "memory/topics/quantum.md" in paths


def test_index_available_false_when_missing(tmp_path):
    assert not mc.index_available(tmp_path)


def test_default_index_dir(tmp_path):
    assert mc.default_index_dir(tmp_path) == tmp_path / ".colbert-index"
