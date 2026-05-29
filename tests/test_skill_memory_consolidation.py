"""Tests for per-skill consolidation isolation (chainlink #266, slice 4).

The general consolidation/dedup passes must NOT touch ``skill_learning``
atoms: a skill's gotcha must never merge into a cross-session observation,
two unrelated skills must never dedup together, and a skill learning must
never be tombstoned by the general dedup. A SEPARATE per-skill dedup pass
collapses a skill's own near-duplicate learnings, scoped so it never
crosses into another skill or the general corpus.

Covers:
- ``_candidate_raws_for_dedup`` (dedup) scoping: None excludes
  skill_learning; a skill name selects only that skill.
- ``_candidate_raws`` (thematic consolidate) scoping: same partition —
  this is the gate the LLM synth pass sees.
- ``dedup_pass(skill_scope=...)`` end-to-end: per-skill collapse, no
  cross-skill contamination, general pass ignores skill atoms.
- ``distinct_skill_scopes`` enumeration (excludes tombstoned/empty).
- ``SagaStore.consolidate_skill_memories`` driver: enumerate + per-skill
  scope + general atoms untouched.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.saga.cluster import make_default_cluster_fn
from mimir.saga.consolidate import _candidate_raws, distinct_skill_scopes
from mimir.saga.client import SagaStore
from mimir.saga.dedup import (
    DEFAULT_DEDUP_THRESHOLD,
    _candidate_raws_for_dedup,
    dedup_pass,
)
from mimir.saga.store import store
from mimir.skill_memory import SKILL_LEARNING_SOURCE_TYPE, build_metadata


# ── low-level fixtures (controllable embeddings) ─────────────────────


@pytest.fixture
def conn():
    schema = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "saga" / "schema.sql"
    ).read_text()
    c = sqlite3.connect(":memory:")
    c.executescript(schema)
    yield c
    c.close()


def _emb(vec):
    return struct.pack(f"{len(vec)}f", *vec), "test", "test-model", len(vec)


def _embed_fn_factory(vectors_by_content):
    def fn(text):
        return _emb(vectors_by_content.get(text, [0.0] * 4))
    return fn


def _skill_meta(skill, kind="tip"):
    return build_metadata(skill, kind)


# ── _candidate_raws_for_dedup scoping (dedup.py) ─────────────────────


class TestDedupCandidateScoping:
    def test_general_excludes_skill_learning(self, conn):
        embed_fn = _embed_fn_factory({"g": [1.0, 0, 0, 0], "s": [1.0, 0, 0, 0]})
        store(conn, "g", embed_fn=embed_fn)  # general (conversation) raw
        store(
            conn, "s", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        got = _candidate_raws_for_dedup(
            conn, lookback_days=None, agent_id="default",
        )
        assert [r["content"] for r in got] == ["g"]

    def test_skill_scope_selects_only_that_skill(self, conn):
        embed_fn = _embed_fn_factory(
            {"a1": [1.0, 0, 0, 0], "b1": [1.0, 0, 0, 0], "g": [1.0, 0, 0, 0]}
        )
        store(conn, "g", embed_fn=embed_fn)
        store(
            conn, "a1", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        store(
            conn, "b1", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        got = _candidate_raws_for_dedup(
            conn, lookback_days=None, agent_id="default", skill_scope="A",
        )
        assert [r["content"] for r in got] == ["a1"]


# ── _candidate_raws scoping (consolidate.py — thematic gate) ─────────


class TestThematicCandidateScoping:
    def test_general_excludes_skill_learning(self, conn):
        # _candidate_raws JOINs access_events; store() emits the initial
        # access event, so a freshly-stored atom is in-window.
        embed_fn = _embed_fn_factory({"g": [1.0, 0, 0, 0], "s": [1.0, 0, 0, 0]})
        store(conn, "g", embed_fn=embed_fn)
        store(
            conn, "s", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        got = _candidate_raws(conn, lookback_days=30, agent_id="default")
        assert [r["content"] for r in got] == ["g"]

    def test_skill_scope_selects_only_that_skill(self, conn):
        embed_fn = _embed_fn_factory({"a1": [1.0, 0, 0, 0], "b1": [1.0, 0, 0, 0]})
        store(
            conn, "a1", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        store(
            conn, "b1", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        got = _candidate_raws(
            conn, lookback_days=30, agent_id="default", skill_scope="B",
        )
        assert [r["content"] for r in got] == ["b1"]


# ── dedup_pass(skill_scope=...) end-to-end ───────────────────────────


def _live(conn, content):
    row = conn.execute(
        "SELECT tombstoned FROM atoms WHERE content = ?", (content,)
    ).fetchone()
    return row is not None and row[0] == 0


class TestPerSkillDedup:
    def test_collapses_within_skill(self, conn):
        # Two near-identical learnings for skill A (same vector, distinct
        # content) cluster and collapse; skill B's stays untouched.
        embed_fn = _embed_fn_factory({
            "A dup one": [1.0, 0, 0, 0],
            "A dup two": [1.0, 0, 0, 0],
            "B note": [0, 1.0, 0, 0],
        })
        for c in ("A dup one", "A dup two"):
            store(
                conn, c, embed_fn=embed_fn,
                source_type=SKILL_LEARNING_SOURCE_TYPE,
                metadata=_skill_meta("A"),
            )
        store(
            conn, "B note", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        cluster_fn = make_default_cluster_fn(
            conn, threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        res = dedup_pass(
            conn, cluster_fn=cluster_fn, agent_id="default",
            min_cluster_size=2, skill_scope="A",
        )
        assert res.candidates_scanned == 2
        assert len(res.duplicates_tombstoned) == 1
        assert _live(conn, "B note")  # untouched

    def test_no_cross_skill_contamination(self, conn):
        # A and B each have one learning with the SAME vector. Scoped to
        # A, B is not even a candidate, so nothing merges across skills.
        embed_fn = _embed_fn_factory(
            {"A only": [1.0, 0, 0, 0], "B only": [1.0, 0, 0, 0]}
        )
        store(
            conn, "A only", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        store(
            conn, "B only", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        cluster_fn = make_default_cluster_fn(
            conn, threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        res = dedup_pass(
            conn, cluster_fn=cluster_fn, agent_id="default",
            min_cluster_size=2, skill_scope="A",
        )
        assert res.candidates_scanned == 1  # only A's atom
        assert res.duplicates_tombstoned == []
        assert _live(conn, "A only")
        assert _live(conn, "B only")

    def test_general_pass_ignores_skill_learning(self, conn):
        # Two near-dup skill_learning atoms would collapse IF the general
        # pass considered them — it must not.
        embed_fn = _embed_fn_factory(
            {"s dup one": [1.0, 0, 0, 0], "s dup two": [1.0, 0, 0, 0]}
        )
        for c in ("s dup one", "s dup two"):
            store(
                conn, c, embed_fn=embed_fn,
                source_type=SKILL_LEARNING_SOURCE_TYPE,
                metadata=_skill_meta("A"),
            )
        cluster_fn = make_default_cluster_fn(
            conn, threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        res = dedup_pass(
            conn, cluster_fn=cluster_fn, agent_id="default",
            min_cluster_size=2,  # skill_scope=None (general)
        )
        assert res.candidates_scanned == 0
        assert res.duplicates_tombstoned == []
        assert _live(conn, "s dup one")
        assert _live(conn, "s dup two")


# ── distinct_skill_scopes enumeration ────────────────────────────────


class TestDistinctSkillScopes:
    def test_enumerates_live_skills_only(self, conn):
        embed_fn = _embed_fn_factory({})
        store(conn, "g", embed_fn=embed_fn)  # general → not a scope
        store(
            conn, "a", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        store(
            conn, "b", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        # Tombstoned skill-C learning drops out of the enumeration.
        store(
            conn, "c", embed_fn=embed_fn,
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("C"),
        )
        conn.execute("UPDATE atoms SET tombstoned = 1 WHERE content = 'c'")
        assert distinct_skill_scopes(conn) == ["A", "B"]

    def test_agent_id_filter(self, conn):
        embed_fn = _embed_fn_factory({})
        store(
            conn, "a", embed_fn=embed_fn, agent_id="agent1",
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("A"),
        )
        store(
            conn, "b", embed_fn=embed_fn, agent_id="agent2",
            source_type=SKILL_LEARNING_SOURCE_TYPE, metadata=_skill_meta("B"),
        )
        assert distinct_skill_scopes(conn, agent_id="agent1") == ["A"]
        assert distinct_skill_scopes(conn) == ["A", "B"]


# ── SagaStore.consolidate_skill_memories driver ──────────────────────


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
def store_obj(tmp_path, monkeypatch):
    _patch_provider(monkeypatch)
    return SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)


async def _add(store_obj, skill, content, kind="tip"):
    return await store_obj.store(
        content, source_type=SKILL_LEARNING_SOURCE_TYPE,
        metadata=build_metadata(skill, kind),
    )


class TestDriver:
    @pytest.mark.asyncio
    async def test_no_skills_returns_empty(self, store_obj):
        out = await store_obj.consolidate_skill_memories(dry_run=True)
        assert out["skills_scanned"] == 0
        assert out["skills"] == {}

    @pytest.mark.asyncio
    async def test_enumerates_and_scopes_per_skill(self, store_obj):
        await _add(store_obj, "A", "a one")
        await _add(store_obj, "A", "a two")
        await _add(store_obj, "B", "b one")
        out = await store_obj.consolidate_skill_memories(dry_run=True)
        assert out["skills_scanned"] == 2
        assert set(out["skills"]) == {"A", "B"}
        # Each per-skill pass scans ONLY that skill's atoms.
        assert out["skills"]["A"]["candidates_scanned"] == 2
        assert out["skills"]["B"]["candidates_scanned"] == 1
        assert isinstance(out["threshold"], float)

    @pytest.mark.asyncio
    async def test_general_atoms_excluded_from_driver(self, store_obj):
        # A plain conversation atom is not a skill scope and is never
        # scanned by the per-skill driver.
        await store_obj.store("just a memory", source_type="conversation")
        await _add(store_obj, "A", "a learning")
        out = await store_obj.consolidate_skill_memories(dry_run=True)
        assert out["skills_scanned"] == 1
        assert set(out["skills"]) == {"A"}
        assert out["skills"]["A"]["candidates_scanned"] == 1
