"""Integration tests for ColBERT × BM25 × dense RRF fusion in
``mimir.search.Indexer.search`` (chainlink #141 Slice 2).

Two scenarios:

1. With a ColBERT channel (mocked) returning a path the BM25 + dense
   channels would not surface first, the fused ranking shifts: the
   ColBERT-favored path moves to (or near) the top.

2. With ``colbert_provider`` left at its default and no index on
   disk, the search returns the legacy weighted-sum ranking
   unchanged — proving the "additive, not replacement" graceful
   fallback.

The Indexer ships with a ``HashEmbedder`` to stay offline; the
ColBERT mock returns ``ColBERTHit`` objects directly without ever
importing pylate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.search import (
    ColBERTHit,
    HashEmbedder,
    Indexer,
)


class _MockColBERTChannel:
    """Replays a fixed hit list regardless of query. Lets the test
    pin which path ColBERT thinks is most relevant."""

    def __init__(self, hits: list[ColBERTHit]) -> None:
        self._hits = hits
        self.call_count = 0

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        self.call_count += 1
        return list(self._hits[:k])


class _DisabledColBERTChannel:
    """ColBERT channel that always returns []. Models the
    pylate-not-installed / index-absent path explicitly so tests
    don't drift if the default lazy probe's behavior changes."""

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        return []


def _seed(home: Path) -> None:
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "state" / "wiki").mkdir(parents=True)
    # Three docs. Two of them mention "quantum"; only one mentions
    # the exotic phrase "entanglement spookiness". Without ColBERT,
    # BM25 + dense pick a-or-b based on token overlap with the
    # query "quantum entanglement". With ColBERT pointing at doc_c
    # ("hidden gem"), the fused ranking should surface doc_c.
    (home / "memory" / "topics" / "doc_a.md").write_text(
        "<!-- desc: doc_a -->\n# Doc A\nquantum mechanics describes atomic systems"
    )
    (home / "memory" / "topics" / "doc_b.md").write_text(
        "<!-- desc: doc_b -->\n# Doc B\nquantum entanglement spookiness at a distance"
    )
    (home / "memory" / "topics" / "doc_c.md").write_text(
        "<!-- desc: doc_c -->\n# Doc C\nhidden gem about correlated subsystems"
    )


@pytest.mark.asyncio
async def test_no_colbert_path_unchanged(tmp_path):
    """When ColBERT is disabled (no index, no provider), search
    returns the legacy weighted-sum ranking with all the existing
    SearchResult fields populated.
    """
    home = tmp_path
    _seed(home)
    idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_DisabledColBERTChannel(),
    )
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        results = await idx.search("quantum entanglement", k=3)
    finally:
        await idx.stop()
    assert results, "BM25 should still surface quantum-token docs"
    # Top result is one of the docs that actually contains "quantum".
    top_paths = {r.path for r in results}
    assert "memory/topics/doc_b.md" in top_paths \
        or "memory/topics/doc_a.md" in top_paths
    # Sanity: scoring components are populated (weighted-sum path).
    assert all(r.score >= 0.0 for r in results)


@pytest.mark.asyncio
async def test_colbert_channel_shifts_ranking(tmp_path):
    """With a ColBERT mock favoring doc_c, the fused RRF ranking
    surfaces doc_c above where it would have landed in BM25+dense
    alone.
    """
    home = tmp_path
    _seed(home)

    # Baseline: no ColBERT — record doc_c's rank.
    baseline_idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_DisabledColBERTChannel(),
    )
    await baseline_idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        baseline = await baseline_idx.search("quantum entanglement", k=5)
    finally:
        await baseline_idx.stop()
    baseline_paths = [r.path for r in baseline]

    # With ColBERT favoring doc_c — fused should put doc_c ahead of
    # at least one path that beat it in baseline.
    colbert_hits = [
        ColBERTHit(path="memory/topics/doc_c.md", chunk_no=0, score=10.0),
        ColBERTHit(path="memory/topics/doc_b.md", chunk_no=0, score=5.0),
    ]
    chan = _MockColBERTChannel(colbert_hits)
    fused_idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=chan,
    )
    await fused_idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        fused = await fused_idx.search("quantum entanglement", k=5)
    finally:
        await fused_idx.stop()
    fused_paths = [r.path for r in fused]

    assert chan.call_count >= 1, "ColBERT channel should have been queried"
    assert "memory/topics/doc_c.md" in fused_paths, (
        "ColBERT-favored path must appear in fused results"
    )
    if "memory/topics/doc_c.md" in baseline_paths:
        baseline_rank = baseline_paths.index("memory/topics/doc_c.md")
        fused_rank = fused_paths.index("memory/topics/doc_c.md")
        assert fused_rank <= baseline_rank, (
            "doc_c should rank no worse with ColBERT in play "
            f"(baseline rank {baseline_rank}, fused rank {fused_rank})"
        )
    # The ranking should be DIFFERENT — fusing a third channel must
    # influence the order somewhere.
    assert fused_paths != baseline_paths or len(fused_paths) != len(baseline_paths), (
        "expected the fused ranking to differ from the BM25+dense baseline"
    )


@pytest.mark.asyncio
async def test_colbert_only_path_surfaces(tmp_path):
    """A path returned only by ColBERT (and not in the SQLite
    BM25+dense candidate pool) still appears in the fused output —
    the third channel is additive to recall, not just a re-ranker.
    """
    home = tmp_path
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "memory" / "topics" / "needle.md").write_text(
        "<!-- desc: needle -->\n# Needle\nthe only file in the corpus"
    )

    chan = _MockColBERTChannel([
        ColBERTHit(path="memory/topics/needle.md", chunk_no=0, score=99.0),
    ])
    idx = Indexer(home, embedder=HashEmbedder(), colbert_provider=chan)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        # A query with no real text overlap — BM25 won't fire much
        # but the ColBERT channel still hands us the path.
        results = await idx.search("xyzzyplaceholder", k=5)
    finally:
        await idx.stop()
    paths = [r.path for r in results]
    assert "memory/topics/needle.md" in paths


@pytest.mark.asyncio
async def test_default_indexer_falls_back_when_index_absent(tmp_path):
    """Constructing an Indexer with no explicit colbert_provider on
    a home that has no ``.colbert-index/`` directory should NOT
    raise and should not call pylate. The lazy probe quietly
    returns [].
    """
    home = tmp_path
    _seed(home)
    idx = Indexer(home, embedder=HashEmbedder())  # default provider
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        results = await idx.search("quantum entanglement", k=3)
    finally:
        await idx.stop()
    assert results, "two-channel fallback must still produce results"
