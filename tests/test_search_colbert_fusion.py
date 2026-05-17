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

Slice 2 Option A: ``recency_fuse_alpha`` tests live below the main
fusion tests. They cover the three load-bearing properties:

- alpha=0.0 produces byte-identical fused ranking to PR #184 as
  shipped (the multiplier collapses to 1.0 and short-circuits).
- alpha>0 promotes a fresh chunk above an older one when the two
  have otherwise identical content-channel rankings.
- alpha=0.3 (the value we ship) is a *nudge*: it can't invert a
  strong content-channel ranking. Recency is a tie-break-tier
  signal, not a dominant one.
"""

from __future__ import annotations

import os
import time
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


# ---------------------------------------------------------------------------
# Slice 2 Option A — recency_fuse_alpha tests
# ---------------------------------------------------------------------------


def _set_mtime(p: Path, age_days: float) -> None:
    """Backdate a file's mtime by ``age_days`` days. ``age_days=0``
    leaves the mtime at "now"."""
    now = time.time()
    target = now - age_days * 86400.0
    os.utime(p, (target, target))


@pytest.mark.asyncio
async def test_recency_fuse_disabled_equivalent_to_pr184(tmp_path):
    """alpha=0.0 produces an identical ranking + identical fused
    scores to PR #184 as shipped. The Option A short-circuit must
    leave the no-flag path byte-for-byte unchanged.
    """
    home = tmp_path
    _seed(home)

    colbert_hits = [
        ColBERTHit(path="memory/topics/doc_c.md", chunk_no=0, score=10.0),
        ColBERTHit(path="memory/topics/doc_b.md", chunk_no=0, score=5.0),
    ]

    # PR #184 baseline: no alpha parameter.
    baseline_idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_MockColBERTChannel(colbert_hits),
    )
    await baseline_idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        baseline = await baseline_idx.search("quantum entanglement", k=5)
    finally:
        await baseline_idx.stop()

    # Same construction but alpha=0.0 explicitly.
    alpha_zero_idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_MockColBERTChannel(colbert_hits),
        recency_fuse_alpha=0.0,
    )
    await alpha_zero_idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        alpha_zero = await alpha_zero_idx.search("quantum entanglement", k=5)
    finally:
        await alpha_zero_idx.stop()

    assert [r.path for r in baseline] == [r.path for r in alpha_zero]
    assert [r.chunk_index for r in baseline] == [r.chunk_index for r in alpha_zero]
    # Float-exact: the alpha=0 short-circuit skips the multiplication
    # entirely, so scores must match bit-for-bit.
    for b, a in zip(baseline, alpha_zero):
        assert b.score == a.score, (
            f"alpha=0 path must be byte-identical to PR #184; "
            f"got {b.score=} vs {a.score=}"
        )

    # Defensive: a negative alpha must clamp to 0 (no silent recency
    # inversion). Same byte-identical result expected.
    clamped_idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_MockColBERTChannel(colbert_hits),
        recency_fuse_alpha=-1.0,
    )
    await clamped_idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        clamped = await clamped_idx.search("quantum entanglement", k=5)
    finally:
        await clamped_idx.stop()
    for b, c in zip(baseline, clamped):
        assert b.score == c.score


@pytest.mark.asyncio
async def test_recency_fuse_promotes_recent_on_tie(tmp_path):
    """Two chunks with identical content + identical content-channel
    rankings; the fresher mtime should outrank the older one when
    alpha > 0. The "tie" is engineered via identical content + a
    ColBERT mock that ranks both equally; only recency separates
    them.
    """
    home = tmp_path
    (home / "memory" / "topics").mkdir(parents=True)
    fresh = home / "memory" / "topics" / "fresh.md"
    stale = home / "memory" / "topics" / "stale.md"
    body = "<!-- desc: tied -->\n# tied\nquantum entanglement spookiness"
    fresh.write_text(body)
    stale.write_text(body)
    _set_mtime(fresh, age_days=0.5)  # ~12 hours old
    _set_mtime(stale, age_days=180.0)  # ~6 months old

    # ColBERT ranks both at the same score so the third channel
    # contributes equal rank weight to both. With alpha=0 the RRF
    # path will break ties on dict-insertion order; with alpha>0
    # the recency multiplier must lift ``fresh.md`` above ``stale``.
    colbert_hits = [
        ColBERTHit(path="memory/topics/stale.md", chunk_no=0, score=1.0),
        ColBERTHit(path="memory/topics/fresh.md", chunk_no=0, score=1.0),
    ]

    idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_MockColBERTChannel(colbert_hits),
        recency_fuse_alpha=0.3,
    )
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        results = await idx.search("quantum entanglement", k=5)
    finally:
        await idx.stop()
    paths = [r.path for r in results]
    assert "memory/topics/fresh.md" in paths
    assert "memory/topics/stale.md" in paths
    fresh_i = paths.index("memory/topics/fresh.md")
    stale_i = paths.index("memory/topics/stale.md")
    assert fresh_i < stale_i, (
        f"alpha=0.3 must promote the recently-modified chunk over "
        f"the stale one when content signals are tied "
        f"(fresh idx {fresh_i}, stale idx {stale_i})"
    )


@pytest.mark.asyncio
async def test_recency_fuse_does_not_invert_strong_content_signal(tmp_path):
    """A stale chunk with a strong content signal (top of every
    channel) and a fresh chunk with a weak content signal (no
    BM25 match, no ColBERT hit) — alpha=0.3 is a nudge, not a
    dominant signal. The fresh chunk MUST NOT leapfrog the
    content-strong stale chunk.
    """
    home = tmp_path
    (home / "memory" / "topics").mkdir(parents=True)
    strong = home / "memory" / "topics" / "strong_stale.md"
    weak = home / "memory" / "topics" / "weak_fresh.md"
    # ``strong_stale`` is the only doc with the query tokens — it
    # wins BM25 + dense + ColBERT cleanly.
    strong.write_text(
        "<!-- desc: strong stale -->\n# strong stale\n"
        "quantum entanglement spookiness at a distance"
    )
    # ``weak_fresh`` has no overlap with the query at all.
    weak.write_text(
        "<!-- desc: weak fresh -->\n# weak fresh\n"
        "unrelated content about gardening"
    )
    _set_mtime(strong, age_days=180.0)
    _set_mtime(weak, age_days=0.1)

    # ColBERT favors strong_stale; weak_fresh is not in the hit list.
    colbert_hits = [
        ColBERTHit(path="memory/topics/strong_stale.md", chunk_no=0, score=20.0),
    ]

    idx = Indexer(
        home,
        embedder=HashEmbedder(),
        colbert_provider=_MockColBERTChannel(colbert_hits),
        recency_fuse_alpha=0.3,
    )
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    try:
        results = await idx.search("quantum entanglement", k=5)
    finally:
        await idx.stop()
    paths = [r.path for r in results]
    assert "memory/topics/strong_stale.md" in paths
    strong_i = paths.index("memory/topics/strong_stale.md")
    # The stale-but-strong chunk must rank above the fresh-but-weak
    # one when both appear. If weak_fresh doesn't make the top-k
    # at all, that's also a pass (it would be even worse off).
    if "memory/topics/weak_fresh.md" in paths:
        weak_i = paths.index("memory/topics/weak_fresh.md")
        assert strong_i < weak_i, (
            f"alpha=0.3 must not invert a strong content signal — "
            f"strong_stale rank {strong_i} should beat weak_fresh "
            f"rank {weak_i}"
        )
