"""Tests for mimir.skill_memory — skill-learning atom substrate (chainlink #266).

Covers the convention (source_type + metadata.skill + valence kind),
scoped recall (newest-first, kind-filterable), the negative-learning
count that drives #267's reflection surfacing, and — through the real
SagaStore — that skill_learning atoms are EXCLUDED from general recall
(a skill's gotcha must not surface as a memory in an unrelated turn).
"""

from __future__ import annotations

import pytest

from mimir.models import AuthContext
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


ADMIN_AUTH = AuthContext(
    principal="test-admin",
    canonical_principal="test-admin",
    roles=("admin",),
    event_ingress="test",
    trigger="user_message",
    channel_id="test-channel",
    interactivity=None,
)


# ── stub provider (mirrors test_search_sessions) ─────────────────────


def _patch_provider(monkeypatch, dim: int = 4):
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(dim)]

        def dimensions(self):
            return dim

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: _StubProvider())

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
            "skill": "circuit-breaker",
            "kind": "failure-mode",
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
            store,
            "circuit-breaker",
            "failure-mode",
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

        out = render_skill_learnings(
            [
                {"kind": "failure-mode", "content": "resets on reconnect"},
                {"kind": "tip", "content": "pass --foo"},
            ]
        )
        assert out == "- [failure-mode] resets on reconnect\n- [tip] pass --foo"

    def test_render_single_lines_multiline_content(self):
        from mimir.skill_memory import render_skill_learnings

        out = render_skill_learnings([{"kind": "tip", "content": "line1\n\nline2"}])
        assert "\n" not in out.split("] ", 1)[1]  # content portion is one line
        assert out == "- [tip] line1 line2"

    @pytest.mark.asyncio
    async def test_augment_appends_learnings(self, store):
        sl = await _add_learning(store, "cb", "failure-mode", "resets on reconnect")
        from mimir.skill_memory import augment_skill_body

        conn = store._ensure_conn()
        out, ids = augment_skill_body(conn, "cb", "ORIGINAL BODY")
        assert out.startswith("ORIGINAL BODY")
        assert "## Learnings from past runs" in out
        assert "[failure-mode] resets on reconnect" in out
        # slice 6: the injected learning's atom_id is returned so the turn
        # can record it for session-boundary voting.
        assert ids == [sl["atom_id"]]

    @pytest.mark.asyncio
    async def test_augment_no_learnings_returns_body_unchanged(self, store):
        from mimir.skill_memory import augment_skill_body

        conn = store._ensure_conn()
        assert augment_skill_body(conn, "never-used", "ORIGINAL") == ("ORIGINAL", [])

    @pytest.mark.asyncio
    async def test_augment_swallows_db_error(self, store, monkeypatch):
        """A recall error must not fail the skill load — body returned as-is."""
        import mimir.skill_memory as sm

        monkeypatch.setattr(
            sm,
            "recall_skill_learnings",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        conn = store._ensure_conn()
        assert sm.augment_skill_body(conn, "cb", "BODY") == ("BODY", [])


class TestSagaStoreConnection:
    @pytest.mark.asyncio
    async def test_connection_accessor_returns_usable_conn(self, store):
        await _add_learning(store, "cb", "tip", "x")
        from mimir.skill_memory import recall_skill_learnings

        conn = store.connection()
        # The public accessor returns a conn the skill_memory helpers can use.
        assert recall_skill_learnings(conn, "cb")[0]["content"] == "x"


class TestRunLockedRead:
    """chainlink #411: skill-memory reads go through the store's own
    serialization (``run_locked_read`` holds ``_db_lock``) instead of
    touching the shared check_same_thread=False connection from a bare
    worker thread."""

    @pytest.mark.asyncio
    async def test_run_locked_read_matches_direct_augment(self, store):
        sl = await _add_learning(store, "cb", "tip", "pass --foo")
        from mimir import skill_memory

        direct = skill_memory.augment_skill_body(
            store._ensure_conn(),
            "cb",
            "BODY",
        )
        via_store = store.run_locked_read(
            lambda conn: skill_memory.augment_skill_body(conn, "cb", "BODY")
        )
        assert via_store == direct
        assert via_store[1] == [sl["atom_id"]]
        assert "[tip] pass --foo" in via_store[0]

    @pytest.mark.asyncio
    async def test_run_locked_read_holds_db_lock_during_fn(self, store):
        """The shared-connection lock must be held while *fn* runs: a
        non-blocking acquire from another thread inside *fn* must fail
        (``_db_lock`` is an RLock, so the probe has to come from a
        different thread)."""
        import threading

        store._ensure_conn()
        real_lock = store._db_lock
        observed: dict[str, bool] = {}

        def _fn(conn):
            def _probe():
                got = real_lock.acquire(blocking=False)
                if got:
                    real_lock.release()
                observed["contended"] = not got

            t = threading.Thread(target=_probe)
            t.start()
            t.join()
            return "ok"

        assert store.run_locked_read(_fn) == "ok"
        assert observed["contended"] is True, (
            "_db_lock was not held while run_locked_read's fn executed"
        )


# ── activation ranking (chainlink #266 slice 6) ──────────────────────


class TestActivationRanking:
    @pytest.mark.asyncio
    async def test_useful_voted_learning_outranks_newer_unvoted(self, store):
        """A learning the agent later marked *useful* (a feedback_positive
        access event, weight 2.0) must out-rank a newer, never-voted learning
        — that's the point of activation ranking over pure recency."""
        old = await _add_learning(store, "s", "tip", "old but useful")
        await _add_learning(store, "s", "tip", "newer but unused")
        conn = store._ensure_conn()

        # Pre-vote: recency wins → newest first.
        before = recall_skill_learnings(conn, "s")
        assert before[0]["content"] == "newer but unused"

        # The agent curates: marks the OLD learning useful (weight-2.0 event).
        await store.outcome(
            [old["atom_id"]],
            feedback="positive",
            auth_context=ADMIN_AUTH,
        )

        after = recall_skill_learnings(conn, "s")
        assert after[0]["content"] == "old but useful", (
            "a useful-voted learning should rise above a newer un-voted one"
        )

    @pytest.mark.asyncio
    async def test_unvoted_falls_back_to_recency(self, store):
        """With no curation, activation == recency-decay, so ranking is
        newest-first (degrades gracefully to the old behavior)."""
        await _add_learning(store, "s", "tip", "first")
        await _add_learning(store, "s", "tip", "second")
        await _add_learning(store, "s", "tip", "third")
        conn = store._ensure_conn()
        got = recall_skill_learnings(conn, "s")
        assert [r["content"] for r in got] == ["third", "second", "first"]
