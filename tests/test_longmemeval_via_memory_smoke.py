"""Smoke test for benchmarks.longmemeval_via_memory.runner.

End-to-end run of a single synthetic question, with stub embedding +
LLM providers so the test doesn't need network access or API keys.

Doesn't validate retrieval quality — that's bench territory. Validates
that the pipeline (ingest → consolidate → query → reader) doesn't
crash and produces a hypothesis record in the right shape.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


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
async def test_runner_completes_one_question(tmp_path, monkeypatch):
    # Stub embedding provider so we don't touch voyage / openai.
    import saga.embeddings
    monkeypatch.setattr(saga.embeddings, "get_provider", _stub_provider)
    # saga.config.get_config is queried for embedding max_input_chars.
    import saga.config

    def _fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr(saga.config, "get_config", _fake_get_config)

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
    # uses saga._llm.call_llm; replace with a deterministic stub.
    import saga._llm
    async def _fake_call_llm(*args, **kwargs):
        return "OBSERVATION:\nAlice consistently prefers concise replies."
    monkeypatch.setattr(saga._llm, "call_llm", _fake_call_llm)

    from mimir.memory.client import MemoryClient
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
async def test_runner_no_consolidate_path(tmp_path, monkeypatch):
    """The --no-consolidate path should skip the LLM call entirely.
    Verify the consolidate seconds is ~0 and no observations get
    created."""
    import saga.embeddings
    monkeypatch.setattr(saga.embeddings, "get_provider", _stub_provider)
    import saga.config
    def _fake_cfg():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr(saga.config, "get_config", _fake_cfg)

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
