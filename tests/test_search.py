"""Indexer + file_search tool (SPEC §6, §8.1).

Uses the deterministic ``HashEmbedder`` to keep tests offline and fast — the
real ``FastEmbedder`` cold-starts an ONNX model and downloads weights, which
isn't appropriate for unit tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from mimir.search import (
    HashEmbedder,
    Indexer,
    SearchResult,
    chunk_text,
    _classify_scope,
    _to_fts_query,
)

# ``mimir.hooks`` and ``mimir.searchtools`` were retired in the
# deepagents migration (post-PR #185 merge target). Tests that use
# them are skipped when the modules aren't importable; the remaining
# tests (including the PR #185 _to_fts_query regression coverage)
# still exercise mimir.search directly.
try:
    from mimir.hooks import make_post_tool_use_hook  # type: ignore[import-not-found]
except ImportError:
    make_post_tool_use_hook = None  # type: ignore[assignment]

try:
    from mimir.searchtools import build_search_tools  # type: ignore[import-not-found]
except ImportError:
    build_search_tools = None  # type: ignore[assignment]


def _seed(home: Path) -> None:
    (home / "memory" / "core").mkdir(parents=True)
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "memory" / "channels" / "alice").mkdir(parents=True)
    (home / "state" / "transcripts").mkdir(parents=True)

    # Core file — must be excluded from indexing.
    (home / "memory" / "core" / "00-persona.md").write_text(
        "<!-- desc: persona -->\n# Persona\nI am Mimir."
    )
    # Memory entries.
    (home / "memory" / "topics" / "quantum.md").write_text(
        "<!-- desc: quantum mechanics notes -->\n# Quantum\n"
        "Quantum mechanics describes nature at atomic and subatomic scales. "
        "Particles exhibit wave-particle duality."
    )
    (home / "memory" / "topics" / "boids.md").write_text(
        "<!-- desc: boids flocking -->\n# Boids\n"
        "Boids is a flocking simulation by Craig Reynolds with three rules: "
        "separation, alignment, cohesion."
    )
    (home / "memory" / "channels" / "alice" / "preferences.md").write_text(
        "<!-- desc: alice preferences -->\nAlice prefers terse messages and dark mode."
    )
    # State entry.
    (home / "state" / "transcripts" / "kickoff.md").write_text(
        "<!-- desc: kickoff transcript -->\n# Kickoff\n"
        "We discussed quantum entanglement at length."
    )
    # INDEX files — should be skipped by the indexer.
    (home / "memory" / "INDEX.md").write_text("# auto")
    (home / "state" / "INDEX.md").write_text("# auto")


def _make_indexer(home: Path) -> Indexer:
    return Indexer(home, embedder=HashEmbedder())


# ---- chunk_text ----------------------------------------------------------


def test_chunk_text_short_returns_one():
    assert chunk_text("short", size=100, overlap=10) == ["short"]


def test_chunk_text_overlaps():
    text = "x" * 250
    chunks = chunk_text(text, size=100, overlap=20)
    assert len(chunks) == 4
    # Each successive chunk starts size-overlap chars later.
    assert chunks[0] == "x" * 100
    # Verify overlap on a non-uniform text:
    text2 = "0123456789" * 12  # 120 chars
    chunks2 = chunk_text(text2, size=50, overlap=10)
    assert chunks2[0][-10:] == chunks2[1][:10]


def test_chunk_text_overlap_too_big_raises():
    long_text = "x" * 100
    with pytest.raises(ValueError):
        chunk_text(long_text, size=10, overlap=10)


def test_chunk_text_empty():
    assert chunk_text("") == []


# ---- _classify_scope -----------------------------------------------------


def test_classify_scope_excludes_core_and_indexes():
    assert _classify_scope("memory/core/00-persona.md") is None
    assert _classify_scope("memory/INDEX.md") is None
    assert _classify_scope("state/INDEX.md") is None
    assert _classify_scope("memory/topics/foo.md") == "memory"
    assert _classify_scope("state/seeds/x.md") == "state"
    assert _classify_scope("logs/events.jsonl") is None


def test_classify_scope_excludes_skip_paths():
    assert _classify_scope("state/heartbeat-backlog.md") is None
    assert _classify_scope("state/proposed-changes.md") is None
    assert _classify_scope("state/identities.yaml") is None
    # Adjacent files in state/ still index normally.
    assert _classify_scope("state/transcripts/kickoff.md") == "state"


def test_classify_scope_excludes_skip_prefixes():
    assert _classify_scope("state/social/inbox.md") is None
    assert _classify_scope("state/social/nested/deep.md") is None
    # Sibling state subtree unaffected.
    assert _classify_scope("state/seeds/x.md") == "state"


# ---- FTS sanitization ----------------------------------------------------


def test_fts_query_strips_operators():
    # Parentheses get stripped to nothing; alnum tokens kept and OR-joined.
    assert _to_fts_query("foo (bar) baz") == "foo OR bar OR baz"
    # Dashes split into separate OR-joined tokens — FTS5 parses
    # ``-foo`` as the unary NOT operator on a column named ``foo``,
    # which raises OperationalError for chunks_fts (no such column).
    # Underscores stay intact (they're term-internal for FTS5
    # tokenizers).
    assert _to_fts_query("hello-world_v2") == "hello OR world_v2"
    assert _to_fts_query("") == ""
    assert _to_fts_query("   ") == ""


# ---- Indexer init + sweep + scope filtering -----------------------------


@pytest.mark.asyncio
async def test_init_schema_creates_tables(tmp_path: Path):
    idx = _make_indexer(tmp_path)
    await asyncio.to_thread(idx.init_schema)
    stats = await idx.stats()
    assert stats.files == 0
    assert stats.chunks == 0


@pytest.mark.asyncio
async def test_sweep_indexes_memory_and_state(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    stats = await idx.stats()
    assert stats.files == 4  # excludes core + 2 INDEX.md
    assert stats.chunks >= 4


@pytest.mark.asyncio
async def test_sweep_skips_core_and_indexes(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    results = await idx.search("Mimir", scope="all", k=10)
    paths = {r.path for r in results}
    assert "memory/core/00-persona.md" not in paths
    assert "memory/INDEX.md" not in paths
    assert "state/INDEX.md" not in paths


@pytest.mark.asyncio
async def test_sweep_skips_workspace_paths(tmp_path: Path):
    """v0.4 §7: heartbeat-backlog / proposed-changes / state/social are
    operator-agent workspace, not knowledge — must not be embedded."""
    _seed(tmp_path)
    # Skip-listed exact paths.
    (tmp_path / "state" / "heartbeat-backlog.md").write_text(
        "<!-- desc: backlog -->\n# Backlog\ntokenmjzrtq items here."
    )
    (tmp_path / "state" / "proposed-changes.md").write_text(
        "<!-- desc: proposals -->\n# Proposals\ntokenmjzrtq here too."
    )
    # Skip-listed prefix.
    (tmp_path / "state" / "social").mkdir()
    (tmp_path / "state" / "social" / "inbox.md").write_text(
        "<!-- desc: social inbox -->\ntokenmjzrtq social-cli artifact."
    )
    # Adjacent state file that SHOULD index — control case.
    (tmp_path / "state" / "neighbor.md").write_text(
        "<!-- desc: neighbor -->\ntokenmjzrtq in a regular state file."
    )

    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    results = await idx.search("tokenmjzrtq", scope="all", k=20)
    paths = {r.path for r in results}
    assert "state/heartbeat-backlog.md" not in paths
    assert "state/proposed-changes.md" not in paths
    assert "state/social/inbox.md" not in paths
    assert "state/neighbor.md" in paths


# ---- Incremental reindex --------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_path_picks_up_new_file(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    before = (await idx.stats()).files

    new_file = tmp_path / "memory" / "topics" / "fresh.md"
    new_file.write_text("<!-- desc: fresh -->\nA shiny new topic about physics.")
    ok = await idx.reindex_path("memory/topics/fresh.md")
    assert ok is True

    after = (await idx.stats()).files
    assert after == before + 1


@pytest.mark.asyncio
async def test_reindex_path_drops_deleted_file(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    target = tmp_path / "memory" / "topics" / "boids.md"
    target.unlink()
    ok = await idx.reindex_path("memory/topics/boids.md")
    assert ok is False
    stats = await idx.stats()
    # Sweep would also drop it, but reindex_path should handle deletion directly.
    assert stats.files == 3


@pytest.mark.asyncio
async def test_sweep_detects_mtime_drift(tmp_path: Path):
    """SPEC §6.3: 60s sweep detects bash-driven writes the tool runner can't see."""
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    target = tmp_path / "memory" / "topics" / "boids.md"
    target.write_text(
        "<!-- desc: boids updated -->\n# Boids\nUpdated content here."
    )
    # Bump mtime explicitly to ensure drift is detectable on fast filesystems.
    new_mtime = time.time() + 5
    os.utime(target, (new_mtime, new_mtime))

    stats = await idx.sweep()
    assert stats["updated"] >= 1


# ---- Search semantics -----------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_keyword_match(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    results = await idx.search("quantum", scope="all", k=5)
    paths = [r.path for r in results]
    assert any("quantum.md" in p for p in paths)


@pytest.mark.asyncio
async def test_search_scope_filter(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    mem = await idx.search("quantum", scope="memory", k=5)
    state = await idx.search("quantum", scope="state", k=5)

    assert all(r.scope == "memory" for r in mem)
    assert all(r.scope == "state" for r in state)
    assert any("memory/topics/quantum.md" in r.path for r in mem)
    assert any("state/transcripts/kickoff.md" in r.path for r in state)


@pytest.mark.asyncio
async def test_search_score_in_unit_range(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    results = await idx.search("flocking", scope="all", k=5)
    for r in results:
        assert 0.0 <= r.score <= 1.0


@pytest.mark.asyncio
async def test_search_returns_empty_for_blank(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)
    assert await idx.search("", scope="all", k=5) == []


# ---- query-embedding LRU cache (CR#12) -----------------------------------


class _CountingEmbedder(HashEmbedder):
    """HashEmbedder that records every embed() call so we can verify the
    LRU short-circuits the call path on repeats."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts, input_type: str = "passage"):  # type: ignore[override]
        self.calls.append(list(texts))
        return super().embed(texts, input_type=input_type)


@pytest.mark.asyncio
async def test_search_caches_query_embedding_within_indexer(tmp_path: Path):
    _seed(tmp_path)
    counting = _CountingEmbedder()
    idx = Indexer(tmp_path, embedder=counting)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    # ``start`` issued passage-embedding calls during the sweep — clear them so
    # the assertions below isolate the query-side cache behavior.
    counting.calls.clear()

    await idx.search("quantum", scope="all", k=5)
    await idx.search("quantum", scope="all", k=5)
    await idx.search("quantum", scope="memory", k=5)  # same query, diff scope
    await idx.search("flocking", scope="all", k=5)

    query_calls = [c for c in counting.calls if c == ["quantum"] or c == ["flocking"]]
    assert query_calls == [["quantum"], ["flocking"]]
    info = idx._embed_query.cache_info()
    assert info.hits == 2  # two repeats of "quantum"
    assert info.misses == 2  # one each for "quantum" and "flocking"


@pytest.mark.asyncio
async def test_query_embedding_cache_is_per_instance(tmp_path: Path):
    """Two indexers share no cache — important for tests that reuse tmp_path
    or for any future code that holds multiple Indexers."""
    _seed(tmp_path)
    a = Indexer(tmp_path, embedder=HashEmbedder())
    b = Indexer(tmp_path, embedder=HashEmbedder())
    await a.start(run_initial_sweep=True, sweep_loop=False)
    await b.start(run_initial_sweep=True, sweep_loop=False)

    await a.search("quantum", scope="all", k=5)
    await a.search("quantum", scope="all", k=5)

    a_info = a._embed_query.cache_info()
    b_info = b._embed_query.cache_info()
    assert a_info.hits == 1 and a_info.misses == 1
    assert b_info.hits == 0 and b_info.misses == 0


def test_query_embedding_cache_returns_immutable_tuple(tmp_path: Path):
    """The cached value is a tuple so callers can't mutate the cached entry
    and corrupt later searches. Mirrors saga's ``cached_embed_query``."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "state").mkdir()
    idx = Indexer(tmp_path, embedder=HashEmbedder())
    vec = idx._embed_query("hello")
    assert isinstance(vec, tuple)
    with pytest.raises(TypeError):
        vec[0] = 0.0  # type: ignore[index]


# ---- file_search tool wrapper --------------------------------------------


@pytest.mark.skipif(
    build_search_tools is None,
    reason="mimir.searchtools retired in deepagents migration; tool surface "
    "is now mimir.tools.* (covered by separate tests).",
)
@pytest.mark.asyncio
async def test_file_search_tool_returns_json(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    tools = {t.name: t for t in build_search_tools(idx)}
    out = await tools["file_search"].handler({"query": "boids", "scope": "memory", "k": 3})
    assert out.get("is_error") is not True
    text = out["content"][0]["text"]
    payload = json.loads(text)
    assert isinstance(payload, list)
    assert any("boids.md" in r["path"] for r in payload)


@pytest.mark.skipif(
    build_search_tools is None,
    reason="mimir.searchtools retired in deepagents migration",
)
@pytest.mark.asyncio
async def test_file_search_tool_invalid_scope(tmp_path: Path):
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=False, sweep_loop=False)
    tools = {t.name: t for t in build_search_tools(idx)}
    out = await tools["file_search"].handler({"query": "x", "scope": "weird"})
    assert out.get("is_error") is True


@pytest.mark.skipif(
    build_search_tools is None,
    reason="mimir.searchtools retired in deepagents migration",
)
@pytest.mark.asyncio
async def test_rebuild_index_tool_reports_counts(tmp_path: Path):
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=False, sweep_loop=False)

    tools = {t.name: t for t in build_search_tools(idx)}
    out = await tools["rebuild_index"].handler({"scope": "all"})
    assert out.get("is_error") is not True
    text = out["content"][0]["text"]
    # First-time sweep should add the seeded files.
    assert "added=" in text


# ---- PostToolUse hook → indexer reindex ---------------------------------


@pytest.mark.skipif(
    make_post_tool_use_hook is None,
    reason="mimir.hooks retired in deepagents migration; reindex-on-write is "
    "wired via WikiBacklinksHook + post-turn hooks in mimir/agent.py "
    "(covered by separate tests).",
)
@pytest.mark.asyncio
async def test_post_tool_use_hook_reindexes_after_write(tmp_path: Path):
    """SDK preset Write fires PostToolUse; the hook calls indexer.reindex_path."""
    _seed(tmp_path)
    idx = _make_indexer(tmp_path)
    await idx.start(run_initial_sweep=True, sweep_loop=False)

    async def reindex(rel: str) -> None:
        await idx.reindex_path(rel)

    hook = make_post_tool_use_hook(tmp_path, reindex)

    # Simulate the SDK invoking Write successfully then firing PostToolUse.
    target = tmp_path / "memory" / "topics" / "relativity.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "<!-- desc: special and general -->\nE=mc^2 and curved spacetime."
    )

    await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"is_error": False},
            "tool_use_id": "tu-1",
        },
        "tu-1",
        {"signal": None},
    )

    results = await idx.search("relativity", scope="memory", k=5)
    assert any("relativity.md" in r.path for r in results)


# ---- SagaProviderEmbedder routing (PR feat/mimir-file-search-via-saga-provider) ----


class _MockSagaProvider:
    """In-memory provider that records calls. Lets us verify
    SagaProviderEmbedder threads ``input_type`` through to saga's
    provider chain without actually loading any embedding model."""

    def __init__(self, dim: int = 8):
        self._dim = dim
        self.calls: list[tuple[list[str], str]] = []

    def dimensions(self) -> int:
        return self._dim

    def batch_embed(self, texts: list[str], input_type: str = "passage"):
        self.calls.append((list(texts), input_type))
        # Return deterministic-ish fake embeddings — index encodes input_type
        # so a query call vs passage call produce DIFFERENT vectors (the
        # whole point of the input_type plumbing).
        tag = 0.5 if input_type == "query" else 0.1
        return [[tag + i * 0.01 for i in range(self._dim)] for _ in texts]


def test_saga_provider_embedder_dimensions_lazy(monkeypatch):
    """``SagaProviderEmbedder.dim`` defers provider construction until
    first access — matches FastEmbedder's lazy-load semantics."""
    from mimir.search import SagaProviderEmbedder
    import mimir.saga.embeddings as saga_emb

    construction_count = [0]

    def fake_get_provider():
        construction_count[0] += 1
        return _MockSagaProvider(dim=384)

    monkeypatch.setattr(saga_emb, "get_provider", fake_get_provider)
    emb = SagaProviderEmbedder()
    assert construction_count[0] == 0  # not yet
    assert emb.dim == 384  # triggers init
    assert construction_count[0] == 1
    _ = emb.dim  # cached
    assert construction_count[0] == 1


def test_saga_provider_embedder_passes_input_type(monkeypatch):
    """SagaProviderEmbedder threads ``input_type`` through to the
    saga provider's batch_embed — the load-bearing fix that lets
    voyage produce different query-vs-document embeddings."""
    from mimir.search import SagaProviderEmbedder
    import mimir.saga.embeddings as saga_emb

    mock = _MockSagaProvider(dim=4)
    monkeypatch.setattr(saga_emb, "get_provider", lambda: mock)

    emb = SagaProviderEmbedder()
    emb.embed(["a doc"], input_type="passage")
    emb.embed(["a query"], input_type="query")

    assert len(mock.calls) == 2
    assert mock.calls[0] == (["a doc"], "passage")
    assert mock.calls[1] == (["a query"], "query")


def test_saga_provider_embedder_empty_input_skips_provider(monkeypatch):
    """Empty input list short-circuits before touching the provider —
    avoids needlessly initializing voyage/openai/fastembed on a no-op."""
    from mimir.search import SagaProviderEmbedder
    import mimir.saga.embeddings as saga_emb

    construction_count = [0]

    def fake_get_provider():
        construction_count[0] += 1
        return _MockSagaProvider()

    monkeypatch.setattr(saga_emb, "get_provider", fake_get_provider)
    emb = SagaProviderEmbedder()
    result = emb.embed([], input_type="passage")
    assert result == []
    assert construction_count[0] == 0  # never initialized


# ---- Dim-mismatch detection (PR #145 review blocker) -------------------


class _CustomDimEmbedder:
    """HashEmbedder-style test fake with a configurable ``dim``. Used to
    simulate the post-provider-switch scenario where existing chunks
    have a different dim than the new embedder expects."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts, input_type: str = "passage"):
        # Match HashEmbedder's shape but with the configured dim.
        import hashlib
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            # Repeat / truncate the hash bytes to fill ``dim`` floats.
            vec = [(h[i % len(h)] / 127.5) - 1.0 for i in range(self.dim)]
            out.append(vec)
        return out


@pytest.mark.asyncio
async def test_dim_mismatch_warns_loudly(tmp_path: Path, caplog):
    """After an operator switches providers, existing chunks in
    index.db are in the OLD vector space. Indexer.start() must emit a
    loud warning pointing at `mimir reindex` so the operator gets
    diagnostic visibility instead of silently-degraded retrieval."""
    _seed(tmp_path)
    # First Indexer: 16-dim (HashEmbedder default). Index everything.
    idx_a = Indexer(tmp_path, embedder=HashEmbedder())
    await idx_a.start(run_initial_sweep=True, sweep_loop=False)
    await idx_a.stop()

    # Second Indexer: 32-dim — mismatched against the 16-dim BLOBs on
    # disk. start() should detect + warn.
    import logging
    idx_b = Indexer(tmp_path, embedder=_CustomDimEmbedder(dim=32))
    with caplog.at_level(logging.WARNING, logger="mimir.search"):
        await idx_b.start(run_initial_sweep=False, sweep_loop=False)
    matching = [r for r in caplog.records if "dim mismatch" in r.message]
    assert len(matching) == 1, \
        f"expected 1 dim-mismatch warning, got {[r.message for r in matching]}"
    msg = matching[0].message
    assert "mimir reindex" in msg
    # Verify the actual + expected byte counts surface in the warning.
    assert "64 bytes" in msg or "128 bytes" in msg  # 16d * 4 or 32d * 4


@pytest.mark.asyncio
async def test_no_warning_on_empty_index(tmp_path: Path, caplog):
    """First-boot with no chunks yet shouldn't fire the warning."""
    _seed(tmp_path)
    idx = Indexer(tmp_path, embedder=HashEmbedder())
    import logging
    with caplog.at_level(logging.WARNING, logger="mimir.search"):
        await idx.start(run_initial_sweep=False, sweep_loop=False)
    matching = [r for r in caplog.records if "dim mismatch" in r.message]
    assert matching == []


@pytest.mark.asyncio
async def test_no_warning_when_dims_match(tmp_path: Path, caplog):
    """Same-dim re-open shouldn't fire the warning."""
    _seed(tmp_path)
    idx_a = Indexer(tmp_path, embedder=HashEmbedder())
    await idx_a.start(run_initial_sweep=True, sweep_loop=False)
    await idx_a.stop()

    idx_b = Indexer(tmp_path, embedder=HashEmbedder())
    import logging
    with caplog.at_level(logging.WARNING, logger="mimir.search"):
        await idx_b.start(run_initial_sweep=False, sweep_loop=False)
    matching = [r for r in caplog.records if "dim mismatch" in r.message]
    assert matching == []
