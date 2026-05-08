"""v0.5 §2: factory + in-process adapter for the unified SagaClient.

Verifies:
- `make_saga_client` selects `_InProcessSaga` for empty/localhost endpoints.
- `make_saga_client` selects `_HttpSaga` for non-localhost URLs.
- `_InProcessSaga` delegates query/store/feedback/etc. to saga.core via
  asyncio.to_thread (we mock the saga modules to keep the test hermetic).
- `_InProcessSaga.health()` returns False when saga is broken, True when ok.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from mimir.saga_client import (
    SagaClient,
    SagaError,
    _HttpSaga,
    _InProcessSaga,
    make_saga_client,
)


# ─── Factory selection ───────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    ["", None, "http://localhost:3002", "http://127.0.0.1:3002",
     "http://[::1]:8080", "http://0.0.0.0:3002"],
)
def test_factory_returns_inprocess_for_localhost_or_empty(endpoint):
    client = make_saga_client(endpoint=endpoint)
    assert isinstance(client, _InProcessSaga)
    assert isinstance(client, SagaClient)  # Protocol check (runtime_checkable)


@pytest.mark.parametrize(
    "endpoint",
    ["http://saga.internal", "https://my-saga.example.com",
     "http://10.0.0.5:3002"],
)
def test_factory_returns_http_for_non_localhost(endpoint):
    client = make_saga_client(endpoint=endpoint, api_key="ak")
    assert isinstance(client, _HttpSaga)


# ─── In-process delegation ──────────────────────────────────────


def _install_fake_saga(monkeypatch, *, calls: dict, query_result=None,
                       stats=None, store_result="atom-id-1",
                       last_sessions=None, most_retrieved=None,
                       fail_stats=False) -> None:
    """Install fake `saga.core`, `saga.config`, `saga.annotate`,
    `saga.consolidation` modules so _InProcessSaga can call into them
    without booting the real saga stack (which needs SQLite, embeddings)."""

    # saga.core
    saga_core = types.ModuleType("saga.core")

    def get_stats():
        if fail_stats:
            raise RuntimeError("DB unreachable")
        return stats or {"est_active_tokens": 100}

    async def hybrid_retrieve(query, **kw):
        calls.setdefault("hybrid_retrieve", []).append({"query": query, "kwargs": kw})
        return query_result or {
            "observations": [{"id": "o1", "content": "obs", "_confidence_tier": "high",
                              "_similarity": 0.5, "_combined_score": 0.7}],
            "raws": [{"id": "r1", "content": "raw", "_confidence_tier": "low",
                      "_similarity": 0.2, "_combined_score": 0.3}],
        }

    def store_atom(content, **kw):
        calls.setdefault("store_atom", []).append({"content": content, "kwargs": kw})
        return store_result

    def mark_contributions(atom_ids, response_text, session_id=None):
        calls.setdefault("mark_contributions", []).append(
            {"atom_ids": atom_ids, "response_text": response_text, "session_id": session_id}
        )
        return {"contributed": len(atom_ids)}

    def record_outcome(atom_ids, feedback, session_id=None, query=None):
        calls.setdefault("record_outcome", []).append(
            {"atom_ids": atom_ids, "feedback": feedback,
             "session_id": session_id, "query": query}
        )
        return {"recorded": len(atom_ids)}

    def store_session_boundary(*, session_id, summary, **kw):
        calls.setdefault("store_session_boundary", []).append(
            {"session_id": session_id, "summary": summary, "kwargs": kw}
        )
        return "boundary-atom-1"

    def get_last_sessions(count=3, channel=None, **kw):
        calls.setdefault("get_last_sessions", []).append(
            {"count": count, "channel": channel}
        )
        return last_sessions or [{"session_id": "s1", "summary": "x"}]

    def get_most_retrieved(*, days, count, channel=None, contributed_only=False,
                            trend=None):
        calls.setdefault("get_most_retrieved", []).append(
            {"days": days, "count": count, "channel": channel,
             "contributed_only": contributed_only, "trend": trend}
        )
        return most_retrieved or [{"id": "a1", "retrieval_count": 5}]

    saga_core.get_stats = get_stats
    saga_core.hybrid_retrieve = hybrid_retrieve
    saga_core.store_atom = store_atom
    saga_core.mark_contributions = mark_contributions
    saga_core.record_outcome = record_outcome
    saga_core.store_session_boundary = store_session_boundary
    saga_core.get_last_sessions = get_last_sessions
    saga_core.get_most_retrieved = get_most_retrieved
    monkeypatch.setitem(sys.modules, "saga.core", saga_core)

    # saga.config — get_config returns a callable that returns config values.
    saga_config = types.ModuleType("saga.config")

    def get_config():
        def cfg(section, key, default=None):
            return default
        return cfg
    saga_config.get_config = get_config
    monkeypatch.setitem(sys.modules, "saga.config", saga_config)

    # saga.annotate
    saga_annotate = types.ModuleType("saga.annotate")
    async def _smart_annotate(content, use_llm=False):
        return {
            "arousal": 0.5, "valence": 0.0, "topics": [],
            "encoding_confidence": 0.7,
        }
    saga_annotate.smart_annotate = _smart_annotate
    saga_annotate.classify_stream = lambda content, source="conversation": "semantic"
    saga_annotate.classify_profile = lambda content: "standard"
    monkeypatch.setitem(sys.modules, "saga.annotate", saga_annotate)

    # saga.consolidation
    saga_consol = types.ModuleType("saga.consolidation")

    class ConsolidationEngine:
        async def consolidate(self, dry_run=False, max_clusters=None):
            calls.setdefault("consolidate", []).append(
                {"dry_run": dry_run, "max_clusters": max_clusters}
            )
            return {"clusters": 3}
    saga_consol.ConsolidationEngine = ConsolidationEngine
    monkeypatch.setitem(sys.modules, "saga.consolidation", saga_consol)


@pytest.mark.asyncio
async def test_inprocess_query_delegates_and_formats(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    result = await client.query(
        "what's up?", top_k=5, session_id="sid", context=[{"role": "user", "content": "hi"}],
    )

    # hybrid_retrieve was called with two_tier=True.
    assert calls["hybrid_retrieve"][0]["query"] == "what's up?"
    assert calls["hybrid_retrieve"][0]["kwargs"]["two_tier"] is True
    assert calls["hybrid_retrieve"][0]["kwargs"]["session_id"] == "sid"

    # Response shape mirrors saga's /v1/query.
    assert result["two_tier"] is True
    assert len(result["observations"]) == 1
    assert result["observations"][0]["id"] == "o1"
    assert result["observations"][0]["confidence_tier"] == "high"
    assert result["observations"][0]["similarity"] == 0.5
    assert len(result["raws"]) == 1
    assert "latency_ms" in result


@pytest.mark.asyncio
async def test_inprocess_query_clamps_long_input(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    long_q = " ".join(f"tok{i}" for i in range(200))
    await client.query(long_q)
    received = calls["hybrid_retrieve"][0]["query"]
    # _MAX_QUERY_TOKENS = 64
    assert len(received.split()) == 64


@pytest.mark.asyncio
async def test_inprocess_store_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    result = await client.store("hello", source_type="api")
    assert calls["store_atom"][0]["content"] == "hello"
    assert result["stored"] is True
    assert result["atom_id"] == "atom-id-1"
    assert result["stream"] == "semantic"


@pytest.mark.asyncio
async def test_inprocess_store_returns_unstored_when_atom_id_none(monkeypatch):
    """saga's store_atom returns None when content is empty/duplicate;
    the in-process adapter should report stored=False with a reason."""
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls, store_result=None)

    client = _InProcessSaga()
    result = await client.store("dup")
    assert result["stored"] is False
    assert result["atom_id"] is None
    assert result["reason"] == "duplicate content"


@pytest.mark.asyncio
async def test_inprocess_feedback_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    await client.feedback(["a1", "a2"], "response", session_id="sid")
    assert calls["mark_contributions"][0]["atom_ids"] == ["a1", "a2"]
    assert calls["mark_contributions"][0]["session_id"] == "sid"


@pytest.mark.asyncio
async def test_inprocess_outcome_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    await client.outcome(["a1"], "positive", session_id="sid", query="q")
    assert calls["record_outcome"][0]["feedback"] == "positive"
    assert calls["record_outcome"][0]["query"] == "q"


@pytest.mark.asyncio
async def test_inprocess_end_session_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    result = await client.end_session("s1", "wrap", topics_discussed=["t1"])
    assert calls["store_session_boundary"][0]["session_id"] == "s1"
    assert result["atom_id"] == "boundary-atom-1"


@pytest.mark.asyncio
async def test_inprocess_recent_session_boundaries_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    out = await client.recent_session_boundaries(channel_id="c1", count=5)
    assert calls["get_last_sessions"][0]["channel"] == "c1"
    assert len(out) == 1


@pytest.mark.asyncio
async def test_inprocess_most_retrieved_atoms_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    out = await client.most_retrieved_atoms(days=14, count=20, contributed_only=True)
    assert calls["get_most_retrieved"][0]["days"] == 14
    assert calls["get_most_retrieved"][0]["contributed_only"] is True
    assert len(out) == 1


@pytest.mark.asyncio
async def test_inprocess_consolidate_delegates(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    out = await client.consolidate(dry_run=True, max_clusters=10)
    assert calls["consolidate"][0]["dry_run"] is True
    assert calls["consolidate"][0]["max_clusters"] == 10
    assert out["clusters"] == 3


# ─── Health checks ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inprocess_health_ok_when_stats_works(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    assert await client.health() is True


@pytest.mark.asyncio
async def test_inprocess_health_false_when_stats_raises(monkeypatch):
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls, fail_stats=True)

    client = _InProcessSaga()
    assert await client.health() is False


@pytest.mark.asyncio
async def test_inprocess_first_call_surfaces_health_error(monkeypatch):
    """If saga's DB is broken, the first method call should raise SagaError
    rather than silently failing — operators see the issue at boot."""
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls, fail_stats=True)

    client = _InProcessSaga()
    with pytest.raises(SagaError):
        await client.query("anything")


@pytest.mark.asyncio
async def test_inprocess_recent_boundaries_returns_empty_on_failure(monkeypatch):
    """recent_session_boundaries swallows failures and returns [] —
    matching the HTTP client's best-effort contract used by feedback.py.

    Re-installs the fake saga.core with a get_last_sessions that raises,
    so monkeypatch tracks the swap and restores it cleanly on teardown.
    Direct attribute mutation on the fake module would bypass monkeypatch
    and leak into other tests' saga.core lookups."""
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    # First, prime the health check so _ensure_ready passes.
    client = _InProcessSaga()
    await client.health()

    # Replace the fake saga.core with one whose get_last_sessions raises.
    broken_core = types.ModuleType("saga.core")
    for name in ("get_stats", "hybrid_retrieve", "store_atom",
                 "mark_contributions", "record_outcome",
                 "store_session_boundary", "get_most_retrieved"):
        setattr(broken_core, name, getattr(sys.modules["saga.core"], name))

    def _boom(**kw):
        raise RuntimeError("boom")
    broken_core.get_last_sessions = _boom
    monkeypatch.setitem(sys.modules, "saga.core", broken_core)

    out = await client.recent_session_boundaries()
    assert out == []


@pytest.mark.asyncio
async def test_inprocess_close_is_noop(monkeypatch):
    """In-process saga has no resources to release; close() must not raise."""
    calls: dict[str, Any] = {}
    _install_fake_saga(monkeypatch, calls=calls)

    client = _InProcessSaga()
    await client.close()  # no exception
