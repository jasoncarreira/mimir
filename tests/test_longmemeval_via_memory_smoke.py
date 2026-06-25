"""Smoke test for benchmarks.longmemeval_via_memory.runner.

End-to-end run of a single synthetic question, with stub embedding +
LLM providers so the test doesn't need network access or API keys.

Doesn't validate retrieval quality — that's bench territory. Validates
that the pipeline (ingest → consolidate → query → reader) doesn't
crash and produces a hypothesis record in the right shape.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

# Skip the bench-smoke tests if the optional workspace ``saga`` package
# isn't installed. ``saga`` lives behind the ``[bench]`` extra (PR
# #329); ``pip install -e ".[dev]"`` doesn't pull it. Without the
# package, the runner can't import ``saga.benchmarks.longmemeval``
# and these tests can't exercise the pipeline. Operators who run
# benches install ``[bench]`` from a workspace checkout.
#
# Guard on the EXACT module the test imports (``…longmemeval.harness``),
# not just ``saga.benchmarks``: when a stray top-level ``saga`` package
# shadows the workspace member, ``saga.benchmarks`` resolves as an empty
# PEP-420 namespace (so a coarse guard passes) while the harness submodule
# is missing — which turned a clean skip into a hard ModuleNotFoundError.
pytest.importorskip("saga.benchmarks.longmemeval.harness")


def _stub_provider():
    """4d "bag-of-keywords" embedding provider — matches the integration
    test's stub. Atoms sharing 'concise' / 'verbose' / etc. cluster
    together; unrelated atoms don't."""
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            text_l = (text or "").lower()
            return [
                float(text_l.count("alice")),
                float(text_l.count("concise")),
                float(text_l.count("verbose")),
                float(text_l.count("bob")),
            ]

        def dimensions(self):
            return 4
    return _StubProvider()


def _make_synthetic_question() -> dict:
    """Tiny synthetic LongMemEval-shaped question. 2 sessions × 2 turns
    each. The "answer" is contained in session 0, turn 1."""
    return {
        "question_id": "synth_q1",
        "question_type": "single-session-preference",
        "question": "What does Alice prefer in replies?",
        "question_date": "2026/05/12 (Tue) 12:00",
        "answer": "concise replies",
        "haystack_session_ids": ["s_0", "s_1"],
        "haystack_dates": ["2026/05/01 (Fri) 10:00", "2026/05/05 (Tue) 14:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I'm Alice.", "has_answer": False},
                {"role": "assistant",
                 "content": "Hi Alice! How can I help?",
                 "has_answer": False},
                {"role": "user",
                 "content": "I prefer concise replies, please.",
                 "has_answer": True},
                {"role": "assistant",
                 "content": "Got it, I'll keep things short.",
                 "has_answer": False},
            ],
            [
                {"role": "user", "content": "Bob here. What's up?",
                 "has_answer": False},
                {"role": "assistant", "content": "Hi Bob!",
                 "has_answer": False},
            ],
        ],
    }


@pytest.mark.asyncio
async def test_session_boundary_rrf_pathway_searches_sessions_and_expands_atoms():
    from benchmarks.longmemeval_via_memory import runner as r

    class _FakeClient:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.conn.execute(
                """
                CREATE TABLE atoms (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    created_at TEXT,
                    tombstoned INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.conn.executemany(
                "INSERT INTO atoms (id, session_id, created_at) VALUES (?, ?, ?)",
                [
                    ("a_late", "s_a", "2026-05-01T10:02:00+00:00"),
                    ("a_early", "s_a", "2026-05-01T10:01:00+00:00"),
                    ("b_early", "s_b", "2026-05-02T10:01:00+00:00"),
                    ("other", "s_other", "2026-05-03T10:01:00+00:00"),
                ],
            )
            self.calls = []

        async def search_sessions(self, question, *, alpha, limit):
            self.calls.append({"question": question, "alpha": alpha, "limit": limit})
            return [
                {"session_id": "s_a", "blended_score": 0.9},
                {"session_id": "s_b", "blended_score": 0.8},
            ]

        def _ensure_conn(self):
            return self.conn

    client = _FakeClient()
    atom_ids, debug = await r._session_boundary_rrf_pathway(
        client,
        "What does Alice prefer?",
        limit=2,
        alpha=0.6,
        atoms_per_session=1,
        weight=0.4,
    )

    assert client.calls == [
        {"question": "What does Alice prefer?", "alpha": 0.6, "limit": 2}
    ]
    assert atom_ids == ["a_early", "b_early"]
    assert debug["session_boundary_atoms_by_session"] == {
        "s_a": ["a_early"],
        "s_b": ["b_early"],
    }
    assert debug["session_boundary_atom_candidates"] == 2
    assert debug["session_boundary_weight"] == 0.4


@pytest.mark.asyncio
async def test_runner_completes_one_question(tmp_path, monkeypatch):
    # Stub embedding provider so we don't touch voyage / openai.
    import mimir.saga.embeddings as _mm_embeddings
    monkeypatch.setattr(_mm_embeddings, "get_provider", _stub_provider)
    # mimir.saga._config_io.get_config is queried for embedding max_input_chars.
    import mimir.saga._config_io as _mm_config

    def _fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr(_mm_config, "get_config", _fake_get_config)

    # Stub the reader — replace harness.read with a deterministic mock.
    import saga.benchmarks.longmemeval.harness as _h
    def _fake_read(question, question_date, retrieved):
        # Pull content from retrieved raws to confirm the runner passed
        # data through. The "hypothesis" stitches one snippet so the
        # bench scorer has something to look at.
        raws = retrieved.get("raws", []) if isinstance(retrieved, dict) else retrieved
        contents = [r.get("content", "") for r in raws[:3]]
        return {
            "hypothesis": " | ".join(contents) or "(no atoms retrieved)",
            "reader_latency_ms": 1,
            "reader_prompt_tokens": 100,
            "reader_completion_tokens": 10,
            "reader_model": "stub-reader",
        }
    monkeypatch.setattr(_h, "read", _fake_read)

    # Stub the consolidate LLM call — synthesize.make_async_observation_synth_fn
    # uses mimir.saga._llm.call_llm; replace with a deterministic stub.
    import mimir.saga._llm as _mm_llm
    async def _fake_call_llm(*args, **kwargs):
        return "OBSERVATION:\nAlice consistently prefers concise replies."
    monkeypatch.setattr(_mm_llm, "call_llm", _fake_call_llm)

    from mimir.saga.client import SagaStore
    from benchmarks.longmemeval_via_memory import runner as r

    # Override the default embedding_dim to match the stub (4d).
    orig_make_client = r._make_client

    def _make_4d_client(db_path, *, embedding_dim=4):
        return orig_make_client(db_path, embedding_dim=4)

    monkeypatch.setattr(r, "_make_client", _make_4d_client)

    q = _make_synthetic_question()
    record, metrics, err = await r._run_one(
        q=q,
        work_dir=tmp_path,
        keep_db=False,
        consolidate_enabled=True,
    )
    assert err is None, f"runner raised: {err}"
    assert record is not None
    assert record["question_id"] == "synth_q1"
    assert "hypothesis" in record
    assert metrics["n_atoms_ingested"] > 0
    # 6 turns in the synthetic question.
    assert metrics["n_atoms_ingested"] == 6
    # At least the raws path returns something.
    assert metrics["n_atoms_retrieved"] >= 0


@pytest.mark.asyncio
async def test_session_boundary_rrf_lane_keeps_summaries_out_of_reader(
    tmp_path, monkeypatch,
):
    import mimir.saga.embeddings as _mm_embeddings
    monkeypatch.setattr(_mm_embeddings, "get_provider", _stub_provider)
    import mimir.saga._config_io as _mm_config

    def _fake_cfg():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr(_mm_config, "get_config", _fake_cfg)

    boundary_summary = "Generated boundary summary from stub LLM."
    import mimir.saga._llm as _mm_llm

    async def _fake_call_llm(*args, **kwargs):
        return json.dumps({
            "summary": boundary_summary,
            "topics_discussed": ["reply preference"],
            "decisions_made": ["keep replies concise"],
            "unfinished": [],
            "emotional_state": "neutral",
        })

    monkeypatch.setattr(_mm_llm, "call_llm", _fake_call_llm)

    captured = {}
    import saga.benchmarks.longmemeval.harness as _h

    def _fake_read(question, question_date, retrieved):
        captured["retrieved"] = retrieved
        rendered = json.dumps(retrieved)
        assert boundary_summary not in rendered
        assert "session_boundary" not in retrieved
        return {
            "hypothesis": "stub",
            "reader_latency_ms": 1,
            "reader_prompt_tokens": 0,
            "reader_completion_tokens": 0,
            "reader_model": "stub",
        }

    monkeypatch.setattr(_h, "read", _fake_read)

    from benchmarks.longmemeval_via_memory import runner as r

    orig = r._make_client
    monkeypatch.setattr(r, "_make_client",
                        lambda db, *, embedding_dim=4: orig(db, embedding_dim=4))

    q = _make_synthetic_question()
    record, metrics, err = await r._run_one(
        q=q,
        work_dir=tmp_path,
        keep_db=False,
        consolidate_enabled=False,
        session_boundary_rrf_lane=True,
        session_boundary_limit=1,
        session_boundary_alpha=0.7,
        session_boundary_weight=0.5,
        session_boundary_atoms_per_session=2,
    )

    assert err is None
    assert record is not None
    assert captured["retrieved"]["raws"]
    assert metrics["session_boundary_rrf_enabled"] is True
    assert metrics["session_boundaries_written"] == 2
    assert metrics["session_boundary_indexed_sessions"] == 2
    assert len(metrics["session_boundary_matched_sessions"]) == 1
    assert metrics["session_boundary_atom_candidates"] > 0
    assert metrics["session_boundary_atom_candidates"] <= 2
    assert metrics["session_boundary_weight"] == 0.5


@pytest.mark.asyncio
async def test_runner_no_consolidate_path(tmp_path, monkeypatch):
    """The --no-consolidate path should skip the LLM call entirely.
    Verify the consolidate seconds is ~0 and no observations get
    created."""
    import mimir.saga.embeddings as _mm_embeddings
    monkeypatch.setattr(_mm_embeddings, "get_provider", _stub_provider)
    import mimir.saga._config_io as _mm_config
    def _fake_cfg():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr(_mm_config, "get_config", _fake_cfg)

    import saga.benchmarks.longmemeval.harness as _h
    monkeypatch.setattr(_h, "read", lambda *a, **k: {
        "hypothesis": "stub", "reader_latency_ms": 1,
        "reader_prompt_tokens": 0, "reader_completion_tokens": 0,
        "reader_model": "stub",
    })

    from benchmarks.longmemeval_via_memory import runner as r

    orig = r._make_client
    monkeypatch.setattr(r, "_make_client",
                        lambda db, *, embedding_dim=4: orig(db, embedding_dim=4))

    q = _make_synthetic_question()
    record, metrics, err = await r._run_one(
        q=q, work_dir=tmp_path, keep_db=False,
        consolidate_enabled=False,
    )
    assert err is None
    assert record is not None
    assert metrics["clusters_consolidated"] == 0


@pytest.mark.asyncio
async def test_generated_session_boundaries_persist_real_sessions(tmp_path, monkeypatch):
    import mimir.saga.embeddings as _mm_embeddings
    monkeypatch.setattr(_mm_embeddings, "get_provider", _stub_provider)
    import mimir.saga._config_io as _mm_config

    def _fake_cfg():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr(_mm_config, "get_config", _fake_cfg)

    import saga.benchmarks.longmemeval.harness as _h
    monkeypatch.setattr(_h, "read", lambda *a, **k: {
        "hypothesis": "stub", "reader_latency_ms": 1,
        "reader_prompt_tokens": 0, "reader_completion_tokens": 0,
        "reader_model": "stub",
    })

    import mimir.saga._llm as _mm_llm

    async def _fake_call_llm(*args, **kwargs):
        return json.dumps({
            "summary": "Generated boundary summary from stub LLM.",
            "topics_discussed": ["reply preference"],
            "decisions_made": ["keep replies concise"],
            "unfinished": ["none"],
            "emotional_state": "neutral",
        })

    monkeypatch.setattr(_mm_llm, "call_llm", _fake_call_llm)

    from benchmarks.longmemeval_via_memory import runner as r

    orig = r._make_client
    monkeypatch.setattr(r, "_make_client",
                        lambda db, *, embedding_dim=4: orig(db, embedding_dim=4))

    q = _make_synthetic_question()
    record, metrics, err = await r._run_one(
        q=q,
        work_dir=tmp_path,
        keep_db=True,
        consolidate_enabled=False,
        session_boundary_treatment="generated",
    )
    assert err is None
    assert record is not None
    assert metrics["session_boundaries_written"] == 2

    db_path = tmp_path / "q_synth_q1.db"
    with sqlite3.connect(db_path) as conn:
        atom_session_ids = {
            row[0] for row in conn.execute(
                """
                SELECT DISTINCT session_id
                  FROM atoms
                 WHERE source_type = 'longmemeval'
                """
            ).fetchall()
        }
        assert atom_session_ids == {"s_0", "s_1"}

        row = conn.execute(
            """
            SELECT summary, topics_discussed, decisions_made, unfinished,
                   started_at, ended_at, reflected_at, embedding_dim
              FROM sessions
             WHERE id = 's_0'
            """
        ).fetchone()

    assert row is not None
    (
        summary, topics, decisions, unfinished,
        started_at, ended_at, reflected_at, emb_dim,
    ) = row
    assert summary == "Generated boundary summary from stub LLM."
    assert json.loads(topics) == ["reply preference"]
    assert json.loads(decisions) == ["keep replies concise"]
    assert json.loads(unfinished) == ["none"]
    assert started_at == "2026-05-01T10:00:00+00:00"
    assert ended_at == "2026-05-01T10:00:00+00:00"
    assert reflected_at == "2026-05-01T10:00:00+00:00"
    assert emb_dim == 4


@pytest.mark.asyncio
async def test_capture_reader_prompt_writes_debug_without_bloating_metrics(
    tmp_path, monkeypatch,
):
    import mimir.saga.embeddings as _mm_embeddings
    monkeypatch.setattr(_mm_embeddings, "get_provider", _stub_provider)
    import mimir.saga._config_io as _mm_config

    def _fake_cfg():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr(_mm_config, "get_config", _fake_cfg)

    import saga.benchmarks.longmemeval.harness as _h

    prompt_messages = [
        {"role": "system", "content": "reader system"},
        {"role": "user", "content": "reader prompt"},
    ]

    monkeypatch.setattr(
        _h,
        "build_prompt",
        lambda question, question_date, retrieved: prompt_messages,
    )
    monkeypatch.setattr(_h, "call_reader", lambda messages: {
        "text": "stub hypothesis",
        "latency_ms": 1,
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "model": "stub-reader",
    })

    from benchmarks.longmemeval_via_memory import runner as r

    orig = r._make_client
    monkeypatch.setattr(r, "_make_client",
                        lambda db, *, embedding_dim=4: orig(db, embedding_dim=4))

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps([_make_synthetic_question()]))
    output_dir = tmp_path / "out"
    debug_path = output_dir / "retrieval_debug.jsonl"
    args = types.SimpleNamespace(
        saga_config=None,
        dataset=str(dataset_path),
        question_types=None,
        limit=None,
        output_dir=str(output_dir),
        work_dir=str(output_dir / "work"),
        run_tag="capture_smoke",
        resume=False,
        keep_dbs=False,
        no_consolidate=True,
        session_boundary_treatment="none",
        session_boundary_rrf_lane=False,
        session_boundary_limit=3,
        session_boundary_alpha=0.7,
        session_boundary_weight=0.5,
        session_boundary_atoms_per_session=30,
        capture_reader_prompt=True,
        retrieval_debug_jsonl=str(debug_path),
    )

    assert await r._amain(args) == 0

    metric_row = json.loads(
        (output_dir / "metrics_capture_smoke.jsonl").read_text().splitlines()[0]
    )
    debug_row = json.loads(debug_path.read_text().splitlines()[0])
    assert "_reader_prompt_messages" not in metric_row
    assert "reader_prompt_messages" not in metric_row
    assert debug_row["reader_prompt_messages"] == prompt_messages
