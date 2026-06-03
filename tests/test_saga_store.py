"""Smoke tests for mimir.saga.client.SagaStore — the
SagaClient-compatible facade.

Validates that the public API methods all run without error against
a fresh in-memory DB. Does NOT validate retrieval quality (FAISS
adapter is stubbed in v1; recall falls through to FTS-only for
candidates). Quality validation comes during the LongMemEval bench
port in tier 5 v2.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.saga.client import SagaStore


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "mimir.saga.db"
    c = SagaStore(db_path=db_path)
    yield c


def _patch_provider(monkeypatch):
    """Replace saga.embeddings.get_provider with a deterministic stub
    so tests don't need real voyage credentials.

    Returns a 4-dim "embedding" derived from text hash. Not useful for
    real retrieval; sufficient to exercise the embed → store → recall
    pipeline.
    """
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]

        def dimensions(self):
            return 4

    def fake_get_provider():
        return _StubProvider()

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", fake_get_provider)
    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.mark.asyncio
async def test_client_health_returns_true_on_fresh_db(client, monkeypatch):
    _patch_provider(monkeypatch)
    ok = await client.health()
    assert ok is True


@pytest.mark.asyncio
async def test_client_store_returns_atom_id(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.store(
        "Alice prefers concise replies", stream="semantic",
    )
    assert result["stored"] is True
    assert "atom_id" in result


@pytest.mark.asyncio
async def test_client_store_dedupes(client, monkeypatch):
    _patch_provider(monkeypatch)
    r1 = await client.store("duplicate content")
    r2 = await client.store("duplicate content")
    assert r1["atom_id"] == r2["atom_id"]
    assert r2["stored"] is False


@pytest.mark.asyncio
async def test_client_query_returns_two_tier_shape(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.store("Alice prefers concise replies")
    result = await client.query("Alice", top_k=5)
    # Saga-compatible shape.
    assert "observations" in result
    assert "raws" in result
    assert "items_returned" in result
    assert "two_tier" in result


@pytest.mark.asyncio
async def test_client_feedback_records_event(client, monkeypatch):
    _patch_provider(monkeypatch)
    r = await client.store("test atom")
    result = await client.feedback(
        [r["atom_id"]], "agent reply text", feedback="positive",
    )
    assert result["marked"] == 1
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_client_end_session_writes_summary(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.end_session(
        "s1", "we discussed PR review",
        topics_discussed=["pr-review"],
    )
    assert result["session_id"] == "s1"
    assert result["session_summary_written"] is True


@pytest.mark.asyncio
async def test_client_end_session_idempotent(client, monkeypatch):
    _patch_provider(monkeypatch)
    r1 = await client.end_session("s1", "first call")
    r2 = await client.end_session("s1", "second call")
    assert r1["session_id"] == r2["session_id"] == "s1"
    assert r1["session_summary_written"] is True
    assert r2["session_summary_written"] is False


# ── get_atoms: batch by-id load (pure read) ────────────────────────────


@pytest.mark.asyncio
async def test_get_atoms_batch_load_preserves_order(client, monkeypatch):
    _patch_provider(monkeypatch)
    a = await client.store("first atom about apples")
    b = await client.store("second atom about bridges")
    res = await client.get_atoms([b["atom_id"], a["atom_id"]])
    assert [x["id"] for x in res["atoms"]] == [b["atom_id"], a["atom_id"]]
    assert res["missing"] == []
    contents = {x["id"]: x["content"] for x in res["atoms"]}
    assert contents[a["atom_id"]] == "first atom about apples"


@pytest.mark.asyncio
async def test_get_atoms_reports_missing(client, monkeypatch):
    _patch_provider(monkeypatch)
    a = await client.store("a real atom")
    res = await client.get_atoms([a["atom_id"], "0000nonexistent0000"])
    assert [x["id"] for x in res["atoms"]] == [a["atom_id"]]
    assert res["missing"] == ["0000nonexistent0000"]


@pytest.mark.asyncio
async def test_get_atoms_excludes_tombstoned(client, monkeypatch):
    _patch_provider(monkeypatch)
    a = await client.store("doomed atom")
    conn = client.connection()
    conn.execute("UPDATE atoms SET tombstoned = 1 WHERE id = ?", (a["atom_id"],))
    conn.commit()
    res = await client.get_atoms([a["atom_id"]])
    assert res["atoms"] == []
    assert res["missing"] == [a["atom_id"]]


@pytest.mark.asyncio
async def test_get_atoms_fires_no_access_events(client, monkeypatch):
    """A by-id load must NOT reinforce activation — unlike query()."""
    _patch_provider(monkeypatch)
    a = await client.store("atom we will load by id")
    conn = client.connection()
    before = conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]
    await client.get_atoms([a["atom_id"]])
    after = conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]
    assert after == before


@pytest.mark.asyncio
async def test_get_atoms_empty_and_dedupe(client, monkeypatch):
    _patch_provider(monkeypatch)
    assert await client.get_atoms([]) == {"atoms": [], "missing": []}
    a = await client.store("dedupe me")
    res = await client.get_atoms([a["atom_id"], a["atom_id"]])
    assert [x["id"] for x in res["atoms"]] == [a["atom_id"]]  # deduped


# ── recall concurrency: access-event write must be best-effort ─────────


@pytest.mark.asyncio
async def test_query_access_write_is_best_effort(client, monkeypatch):
    """Regression: recall's Pass-4 access-event write must not crash the
    query when it can't BEGIN because the shared connection already has a
    transaction open — exactly what concurrent memory_query calls caused.
    Before the fix it raised 'cannot start a transaction within a
    transaction'; now it logs and the query still returns its result.

    AND (mimir-carreira #564 review): the best-effort failure path must NOT
    roll back the OTHER open transaction — only the one this block began. We
    open a transaction, make an uncommitted write, run query(), and assert the
    write survives (the access-event handler's rollback is ownership-guarded)."""
    _patch_provider(monkeypatch)
    r = await client.store("Alice prefers concise replies")
    conn = client.connection()
    conn.execute("BEGIN IMMEDIATE")  # block recall's access-event write
    # An uncommitted write owned by THIS (the caller's) transaction.
    conn.execute("UPDATE atoms SET arousal = 0.99 WHERE id = ?", (r["atom_id"],))
    try:
        result = await client.query("Alice", top_k=5)  # must NOT raise
        # recall's skipped access-event write must NOT have rolled us back.
        arousal = conn.execute(
            "SELECT arousal FROM atoms WHERE id = ?", (r["atom_id"],)
        ).fetchone()[0]
        assert arousal == 0.99  # victim transaction intact
        conn.commit()
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    assert "raws" in result and "observations" in result


# NOTE: a concurrent-query smoke test (asyncio.gather of N query() calls)
# was deliberately NOT added here — it segfaults in fts_search, exposing a
# deeper issue: concurrent READS on the single shared sqlite connection are
# unsafe, not just the BEGIN IMMEDIATE write this PR makes best-effort. That
# needs the connection model reworked (per-call connections / serialized DB
# access) and is tracked separately. See chainlink.


# ── memory_get tool wrapper ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_get_tool_formats_and_reports_missing(client, monkeypatch):
    _patch_provider(monkeypatch)
    from mimir.tools.memory import memory_get, set_memory_client
    a = await client.store("apple atom content")
    set_memory_client(client)
    try:
        out = await memory_get.ainvoke({"atom_ids": [a["atom_id"], "missing-id"]})
    finally:
        set_memory_client(None)
    assert a["atom_id"] in out
    assert "apple atom content" in out
    assert "missing-id" in out and "not found" in out


@pytest.mark.asyncio
async def test_memory_get_tool_no_client():
    from mimir.tools.memory import memory_get, set_memory_client
    set_memory_client(None)
    out = await memory_get.ainvoke({"atom_ids": ["x"]})
    assert "no SagaStore configured" in out


@pytest.mark.asyncio
async def test_client_recent_session_boundaries(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.end_session("s1", "first")
    await client.end_session("s2", "second")
    boundaries = await client.recent_session_boundaries(count=10)
    assert len(boundaries) == 2


@pytest.mark.asyncio
async def test_client_forget_dry_run(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.store("stale atom")
    result = await client.forget(dry_run=True)
    assert result["dry_run"] is True
    # Returns count + preview ids without writing.


@pytest.mark.asyncio
async def test_client_most_retrieved_atoms(client, monkeypatch):
    """The mapping to access_events for "what got retrieved most"."""
    _patch_provider(monkeypatch)
    r = await client.store("atom to retrieve")
    # Fire a few retrievals.
    for _ in range(3):
        await client.query("atom to retrieve")
    top = await client.most_retrieved_atoms(days=7, count=5)
    assert isinstance(top, list)


@pytest.mark.asyncio
async def test_saga_store_async_context_manager(tmp_path, monkeypatch):
    """``async with SagaStore(...) as store:`` opens the connection
    eagerly and closes it on exit — operator + test-fixture
    ergonomics fix so callers don't have to remember
    ``await store.close()`` manually."""
    from mimir.saga.client import SagaStore
    _patch_provider(monkeypatch)
    db_path = tmp_path / "ctx_mgr.db"

    async with SagaStore(db_path=db_path) as store:
        # Connection was opened eagerly on __aenter__.
        assert store._conn is not None
        # Real method call still works.
        await store.store("atom in async-with body")

    # On exit, the connection is closed.
    assert store._conn is None


@pytest.mark.asyncio
async def test_saga_store_async_context_manager_propagates_exceptions(
    tmp_path, monkeypatch,
):
    """An exception raised in the ``async with`` body propagates —
    ``__aexit__`` must not suppress. Defends against accidentally
    introducing exception swallow in close-on-exit."""
    from mimir.saga.client import SagaStore
    _patch_provider(monkeypatch)
    db_path = tmp_path / "ctx_mgr_exc.db"

    with pytest.raises(ValueError, match="from body"):
        async with SagaStore(db_path=db_path) as store:
            raise ValueError("from body")
    # And the connection still got closed.
    assert store._conn is None


# ─── contextual-rewrite default-from-config ──────────────────────────


def _patch_provider_with_rewrite_flag(monkeypatch, *, rewrite_enabled: bool):
    """Like ``_patch_provider`` but the stubbed saga.toml config also
    reports ``[retrieval].enable_contextual_rewrite``."""
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            return [0.1, 0.2, 0.3, 0.4]

        def dimensions(self):
            return 4

    def fake_get_provider():
        return _StubProvider()

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
                ("retrieval", "enable_contextual_rewrite"): rewrite_enabled,
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", fake_get_provider)
    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.mark.asyncio
async def test_query_reads_rewrite_flag_from_config_when_kwarg_omitted(
    client, monkeypatch,
):
    """When the caller omits ``enable_contextual_rewrite=`` and passes
    ``context=``, SagaStore.query consults saga.toml's
    ``[retrieval].enable_contextual_rewrite`` — so the agent doesn't
    have to thread the toml flag through every call site.
    """
    _patch_provider_with_rewrite_flag(monkeypatch, rewrite_enabled=True)
    rewrite_calls: list = []

    async def fake_rewrite(query, context):
        rewrite_calls.append((query, context))
        return "anchor for X"

    monkeypatch.setattr(
        "mimir.saga.query_rewrite.rewrite_query", fake_rewrite,
    )
    payload = await client.query(
        "what about that?",
        context=[{"role": "user", "content": "tell me about X"}],
    )
    assert len(rewrite_calls) == 1
    assert payload.get("rewritten_query") == "anchor for X"


@pytest.mark.asyncio
async def test_query_skips_rewrite_when_config_disabled(client, monkeypatch):
    """Config flag OFF + caller passes context= → rewrite must NOT fire.
    The toml flag is the authoritative gate when the caller defers.
    """
    _patch_provider_with_rewrite_flag(monkeypatch, rewrite_enabled=False)
    rewrite_calls: list = []

    async def fake_rewrite(query, context):
        rewrite_calls.append((query, context))
        return "should not be used"

    monkeypatch.setattr(
        "mimir.saga.query_rewrite.rewrite_query", fake_rewrite,
    )
    payload = await client.query(
        "what about that?",
        context=[{"role": "user", "content": "tell me about X"}],
    )
    assert rewrite_calls == []
    # No rewrite happened — rewritten_query stays empty in the response.
    assert not payload.get("rewritten_query")


@pytest.mark.asyncio
async def test_query_explicit_kwarg_overrides_config_flag(client, monkeypatch):
    """Explicit ``enable_contextual_rewrite=False`` wins over a toml
    flag set to True. Lets bench / test code force-off."""
    _patch_provider_with_rewrite_flag(monkeypatch, rewrite_enabled=True)
    rewrite_calls: list = []

    async def fake_rewrite(query, context):
        rewrite_calls.append((query, context))
        return "anchor"

    monkeypatch.setattr(
        "mimir.saga.query_rewrite.rewrite_query", fake_rewrite,
    )
    await client.query(
        "what about that?",
        context=[{"role": "user", "content": "tell me about X"}],
        enable_contextual_rewrite=False,
    )
    assert rewrite_calls == []


# ─── PR #342 follow-up: warning when NYI forget params are passed ────


@pytest.mark.asyncio
async def test_forget_warns_when_contribution_threshold_passed(
    tmp_path, caplog,
):
    """``SagaStore.forget(contribution_threshold=...)`` accepts the
    param at the surface (so HTTP-path-shape callsites don't break)
    but ``forget_by_criteria`` doesn't implement it. Pre-fix the
    in-process path silently dropped the kwarg — same shape as the
    bug PR #342 was closing for ``agent_id`` + ``min_retrievals``.
    Now it logs a clear warning so operators see the gap.
    """
    import logging
    from mimir.saga.client import SagaStore
    db_path = tmp_path / "test.saga.db"
    store = SagaStore(db_path=db_path)

    with caplog.at_level(logging.WARNING, logger="mimir.saga.client"):
        await store.forget(
            dry_run=True,
            contribution_threshold=0.3,
        )
    warning_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and "contribution_threshold" in r.getMessage()
    ]
    assert warning_msgs, (
        f"expected a WARNING mentioning contribution_threshold; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "ignored" in warning_msgs[0].lower()


@pytest.mark.asyncio
async def test_forget_warns_when_contradiction_threshold_passed(
    tmp_path, caplog,
):
    """Same shape for the contradiction_threshold param."""
    import logging
    from mimir.saga.client import SagaStore
    db_path = tmp_path / "test.saga.db"
    store = SagaStore(db_path=db_path)

    with caplog.at_level(logging.WARNING, logger="mimir.saga.client"):
        await store.forget(
            dry_run=True,
            contradiction_threshold=0.5,
        )
    warning_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and "contradiction_threshold" in r.getMessage()
    ]
    assert warning_msgs
    assert "ignored" in warning_msgs[0].lower()


@pytest.mark.asyncio
async def test_forget_silent_when_only_implemented_params_passed(
    tmp_path, caplog,
):
    """Regression guard: a forget call that only uses the supported
    params (agent_id is injected automatically, plus min_retrievals /
    grace_days / confidence_floor) must NOT emit a warning."""
    import logging
    from mimir.saga.client import SagaStore
    db_path = tmp_path / "test.saga.db"
    store = SagaStore(db_path=db_path)

    with caplog.at_level(logging.WARNING, logger="mimir.saga.client"):
        await store.forget(
            dry_run=True,
            grace_days=30,
            min_retrievals=5,
        )
    nyi_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and (
            "contribution_threshold" in r.getMessage()
            or "contradiction_threshold" in r.getMessage()
        )
    ]
    assert not nyi_warnings, (
        f"unexpected NYI-param warnings on a supported-params-only "
        f"call: {[r.getMessage() for r in nyi_warnings]}"
    )
