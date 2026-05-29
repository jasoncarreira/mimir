"""Tests for mimir.skill_memory — skill-learning atom substrate (chainlink #266).

Covers the convention (source_type + metadata.skill + valence kind),
scoped recall (newest-first, kind-filterable), the negative-learning
count that drives #267's reflection surfacing, and — through the real
SagaStore — that skill_learning atoms are EXCLUDED from general recall
(a skill's gotcha must not surface as a memory in an unrelated turn).
"""
from __future__ import annotations

import pytest

from mimir.saga.client import SagaStore
from mimir.skill_memory import (
    ALL_KINDS,
    NEGATIVE_KINDS,
    POSITIVE_KINDS,
    SKILL_LEARNING_SOURCE_TYPE,
    build_metadata,
    count_negative_learnings,
    is_negative_kind,
    recall_skill_learnings,
)


# ── stub provider (mirrors test_search_sessions) ─────────────────────


def _patch_provider(monkeypatch, dim: int = 4):
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(dim)]

        def dimensions(self):
            return dim

    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider", lambda: _StubProvider()
    )

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): f"stub-{dim}d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.fixture
def store(tmp_path, monkeypatch):
    _patch_provider(monkeypatch)
    return SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)


async def _add_learning(store, skill, kind, content):
    return await store.store(
        content,
        source_type=SKILL_LEARNING_SOURCE_TYPE,
        metadata=build_metadata(skill, kind),
    )


# ── convention / valence ─────────────────────────────────────────────


class TestValence:
    def test_kind_partition(self):
        assert NEGATIVE_KINDS.isdisjoint(POSITIVE_KINDS)
        assert ALL_KINDS == NEGATIVE_KINDS | POSITIVE_KINDS

    def test_is_negative_kind(self):
        assert is_negative_kind("failure-mode")
        assert is_negative_kind("input-quirk")
        assert not is_negative_kind("tip")
        assert not is_negative_kind("success-pattern")

    def test_build_metadata_ok(self):
        assert build_metadata("circuit-breaker", "failure-mode") == {
            "skill": "circuit-breaker", "kind": "failure-mode",
        }

    def test_build_metadata_trims_skill(self):
        assert build_metadata("  memory  ", "tip")["skill"] == "memory"

    def test_build_metadata_rejects_empty_skill(self):
        with pytest.raises(ValueError):
            build_metadata("", "tip")

    def test_build_metadata_rejects_unknown_kind(self):
        # A typo'd kind would silently drop a learning out of the
        # negative-count — closed enum guards #267's surfacing filter.
        with pytest.raises(ValueError):
            build_metadata("circuit-breaker", "gotcha")  # not in the enum


# ── scoped recall ────────────────────────────────────────────────────


class TestRecallSkillLearnings:
    @pytest.mark.asyncio
    async def test_returns_only_that_skill(self, store):
        await _add_learning(store, "circuit-breaker", "failure-mode", "cb gotcha")
        await _add_learning(store, "memory", "tip", "mem tip")
        conn = store._ensure_conn()
        cb = recall_skill_learnings(conn, "circuit-breaker")
        assert [r["content"] for r in cb] == ["cb gotcha"]
        assert cb[0]["kind"] == "failure-mode"

    @pytest.mark.asyncio
    async def test_all_kinds_returned_newest_first(self, store):
        await _add_learning(store, "s", "tip", "first")
        await _add_learning(store, "s", "failure-mode", "second")
        await _add_learning(store, "s", "perf-caveat", "third")
        conn = store._ensure_conn()
        got = recall_skill_learnings(conn, "s")
        # Both valences surface on load (a tip and a gotcha both help).
        assert "tip" in {r["kind"] for r in got}
        assert "failure-mode" in {r["kind"] for r in got}
        # Newest-first.
        assert got[0]["content"] == "third"

    @pytest.mark.asyncio
    async def test_kind_filter(self, store):
        await _add_learning(store, "s", "tip", "a tip")
        await _add_learning(store, "s", "failure-mode", "a failure")
        conn = store._ensure_conn()
        negs = recall_skill_learnings(conn, "s", kinds=NEGATIVE_KINDS)
        assert [r["content"] for r in negs] == ["a failure"]

    @pytest.mark.asyncio
    async def test_limit(self, store):
        for i in range(5):
            await _add_learning(store, "s", "tip", f"tip-{i}")
        conn = store._ensure_conn()
        assert len(recall_skill_learnings(conn, "s", limit=2)) == 2

    @pytest.mark.asyncio
    async def test_empty_skill_returns_empty(self, store):
        conn = store._ensure_conn()
        assert recall_skill_learnings(conn, "") == []
        assert recall_skill_learnings(conn, "never-used") == []


# ── negative-learning count (#267 surfacing input) ───────────────────


class TestCountNegativeLearnings:
    @pytest.mark.asyncio
    async def test_counts_only_negative_kinds(self, store):
        await _add_learning(store, "s", "failure-mode", "f1")
        await _add_learning(store, "s", "input-quirk", "q1")
        await _add_learning(store, "s", "tip", "t1")  # positive — not counted
        await _add_learning(store, "s", "success-pattern", "p1")  # not counted
        conn = store._ensure_conn()
        assert count_negative_learnings(conn, "s") == 2

    @pytest.mark.asyncio
    async def test_since_window(self, store):
        await _add_learning(store, "s", "failure-mode", "old then new")
        conn = store._ensure_conn()
        # A future since-bound excludes everything; an epoch bound includes it.
        assert count_negative_learnings(conn, "s", since_iso="2099-01-01") == 0
        assert count_negative_learnings(conn, "s", since_iso="2000-01-01") == 1

    @pytest.mark.asyncio
    async def test_unknown_skill_zero(self, store):
        conn = store._ensure_conn()
        assert count_negative_learnings(conn, "nope") == 0


# ── general-recall exclusion (the isolation invariant) ───────────────


class TestGeneralRecallExcludesSkillLearning:
    @pytest.mark.asyncio
    async def test_skill_learning_atom_not_in_query_results(self, store):
        """A skill_learning atom is embedded/FTS-indexed like any atom, but
        must NOT surface via general query() — only via skill-load recall.
        Store a skill_learning atom and a normal atom with overlapping text;
        the normal one may surface, the skill_learning one must not."""
        sl = await _add_learning(
            store, "circuit-breaker", "failure-mode",
            "circuit breaker backoff resets on reconnect",
        )
        await store.store(
            "circuit breaker backoff behavior notes",
            source_type="conversation",
        )
        res = await store.query("circuit breaker backoff", top_k=12)
        ids = {a["id"] for a in res.get("observations", []) + res.get("raws", [])}
        assert sl["atom_id"] not in ids, (
            "skill_learning atom leaked into general recall — it must only "
            "surface via the skill-load injection"
        )


# ── load-injection helpers (chainlink #266, slice 2) ─────────────────


class TestRenderAndAugment:
    def test_render_empty(self):
        from mimir.skill_memory import render_skill_learnings
        assert render_skill_learnings([]) == ""

    def test_render_one_bullet_per_learning_with_kind(self):
        from mimir.skill_memory import render_skill_learnings
        out = render_skill_learnings([
            {"kind": "failure-mode", "content": "resets on reconnect"},
            {"kind": "tip", "content": "pass --foo"},
        ])
        assert out == "- [failure-mode] resets on reconnect\n- [tip] pass --foo"

    def test_render_single_lines_multiline_content(self):
        from mimir.skill_memory import render_skill_learnings
        out = render_skill_learnings([{"kind": "tip", "content": "line1\n\nline2"}])
        assert "\n" not in out.split("] ", 1)[1]  # content portion is one line
        assert out == "- [tip] line1 line2"

    @pytest.mark.asyncio
    async def test_augment_appends_learnings(self, store):
        await _add_learning(store, "cb", "failure-mode", "resets on reconnect")
        from mimir.skill_memory import augment_skill_body
        conn = store._ensure_conn()
        out = augment_skill_body(conn, "cb", "ORIGINAL BODY")
        assert out.startswith("ORIGINAL BODY")
        assert "## Learnings from past runs" in out
        assert "[failure-mode] resets on reconnect" in out

    @pytest.mark.asyncio
    async def test_augment_no_learnings_returns_body_unchanged(self, store):
        from mimir.skill_memory import augment_skill_body
        conn = store._ensure_conn()
        assert augment_skill_body(conn, "never-used", "ORIGINAL") == "ORIGINAL"

    @pytest.mark.asyncio
    async def test_augment_swallows_db_error(self, store, monkeypatch):
        """A recall error must not fail the skill load — body returned as-is."""
        import mimir.skill_memory as sm
        monkeypatch.setattr(sm, "recall_skill_learnings",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        conn = store._ensure_conn()
        assert sm.augment_skill_body(conn, "cb", "BODY") == "BODY"


class TestSagaStoreConnection:
    @pytest.mark.asyncio
    async def test_connection_accessor_returns_usable_conn(self, store):
        await _add_learning(store, "cb", "tip", "x")
        from mimir.skill_memory import recall_skill_learnings
        conn = store.connection()
        # The public accessor returns a conn the skill_memory helpers can use.
        assert recall_skill_learnings(conn, "cb")[0]["content"] == "x"
