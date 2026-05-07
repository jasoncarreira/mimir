"""SAGA MCP tools (SPEC §8.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir import _context
from mimir.event_logger import init_logger
from mimir.models import TurnContext, make_turn_id
from mimir.sagatools import build_saga_tools

from ._fake_saga import FakeSaga


@pytest.fixture(autouse=True)
def _ensure_event_logger(tmp_path):
    """saga_* tools emit ``saga_<tool>_ctx_resolution`` events (chainlink
    #23 subissues #25 + #26) which require the event_logger to be
    initialized. Tests that monkeypatch ``mimir.sagatools.log_event``
    bypass this; tests that don't get a real logger pointed at a temp
    file."""
    init_logger(tmp_path / "test-events.jsonl", session_id="test-sagatools")


@pytest.fixture(autouse=True)
def _isolate_active_turns():
    """``_active_turns`` is a module-global registry. Tests that register
    a turn but don't reset_current_turn (e.g. a test failure interrupts
    cleanup) can leak state into subsequent tests. Snapshot + restore
    around each test keeps the lookup-chain tests honest about the
    multi-active vs single-active vs no-active cases."""
    snapshot = dict(_context._active_turns)
    yield
    _context._active_turns.clear()
    _context._active_turns.update(snapshot)


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not registered")


def _ctx(channel_id: str = "c1", saga_session_id: str = "saga-c1-1") -> TurnContext:
    return TurnContext(
        turn_id=make_turn_id(),
        session_id=channel_id,
        trigger="user_message",
        channel_id=channel_id,
        started_at=0.0,
        saga_session_id=saga_session_id,
    )


@pytest.mark.asyncio
async def test_saga_query_passes_session_id_and_appends_atom_ids():
    fake = FakeSaga(
        query_response={
            "_raw_atoms": [
                {"id": "a1", "stream": "semantic", "content": "alpha"},
                {"id": "a2", "stream": "episodic", "content": "beta"},
            ]
        }
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await saga_query.handler({"query": "anything", "top_k": 5})
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    assert ctx.saga_atom_ids == ["a1", "a2"]
    payload = fake.last("query")
    assert payload["session_id"] == "saga-c1-1"
    assert payload["top_k"] == 5


@pytest.mark.asyncio
async def test_saga_query_extracts_from_live_atoms_key():
    """Real SAGA (saga.server.api_query) returns atoms
    under the ``atoms`` key, not ``_raw_atoms``. Regression for the bug
    where contributions never marked because the extractor only looked
    at the legacy/never-shipped key."""
    fake = FakeSaga(
        query_response={
            "atoms": [
                {"id": "live-1", "stream": "semantic", "content": "x"},
                {"id": "live-2", "stream": "semantic", "content": "y"},
            ]
        }
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        await saga_query.handler({"query": "hi"})
    finally:
        _context.reset_current_turn(token)

    assert ctx.saga_atom_ids == ["live-1", "live-2"]


@pytest.mark.asyncio
async def test_saga_query_extracts_from_two_tier_observations_and_raws():
    """When two_tier_enabled = true, SAGA returns observations and raws as
    separate lists (saga.core._two_tier_split). Both
    contribute atom IDs to the contribution-tracking set, with observations
    surfacing first since they're the higher-level consolidated atoms."""
    fake = FakeSaga(
        query_response={
            "observations": [
                {"id": "obs-1", "memory_type": "observation",
                 "stream": "semantic", "content": "synthesized inference"},
            ],
            "raws": [
                {"id": "raw-1", "memory_type": "raw",
                 "stream": "semantic", "content": "evidence atom 1"},
                {"id": "raw-2", "memory_type": "raw",
                 "stream": "semantic", "content": "evidence atom 2"},
            ],
        }
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        result = await saga_query.handler({"query": "hi"})
    finally:
        _context.reset_current_turn(token)

    # Observations come before raws, both feed the contribution-tracking set.
    assert ctx.saga_atom_ids == ["obs-1", "raw-1", "raw-2"]
    # The rendered hits list reflects memory_type so the agent can tell
    # observations apart from raw evidence.
    text = result["content"][0]["text"]
    assert "obs-1" in text  # atom id
    assert "observation" in text  # the memory_type label leaked through


@pytest.mark.asyncio
async def test_saga_query_renders_per_atom_confidence_tier():
    """Per-atom confidence_tier (post SAGA commit with per-atom gating)
    surfaces in both the slim hits dict and downstream label rendering, so
    the agent can prefer observation/high atoms over raw/low ones."""
    from mimir.sagatools import _atom_label, _hits_summary
    payload = {
        "two_tier": True,
        "observations": [
            {"id": "obs-h", "memory_type": "observation",
             "confidence_tier": "high", "similarity": 0.7,
             "score": 0.05, "evidence_count": 4, "stream": "semantic",
             "content": "high-confidence observation"},
        ],
        "raws": [
            {"id": "raw-m", "memory_type": "raw",
             "confidence_tier": "medium", "similarity": 0.32,
             "score": 0.04, "stream": "semantic",
             "content": "medium raw"},
            {"id": "raw-l", "memory_type": "raw",
             "confidence_tier": "low", "similarity": 0.18,
             "score": 0.02, "stream": "semantic",
             "content": "low raw"},
        ],
    }
    hits = _hits_summary(payload)
    assert [h["atom_id"] for h in hits] == ["obs-h", "raw-m", "raw-l"]
    assert hits[0]["memory_type"] == "observation"
    assert hits[0]["confidence_tier"] == "high"
    assert hits[0]["evidence_count"] == 4
    assert hits[1]["confidence_tier"] == "medium"
    assert hits[2]["confidence_tier"] == "low"

    assert _atom_label(payload["observations"][0]) == "observation/high"
    assert _atom_label(payload["raws"][0]) == "semantic/medium"
    assert _atom_label(payload["raws"][1]) == "semantic/low"
    # Legacy single-tier (no tier field) — fall back to base label.
    assert _atom_label({"stream": "semantic"}) == "semantic"


@pytest.mark.asyncio
async def test_saga_client_passes_min_confidence_tier_when_set():
    """SagaClient.query forwards min_confidence_tier into the request body
    only when explicitly set; omitting it lets SAGA use its config default."""
    from mimir.saga_client import _HttpSaga as SagaClient
    captured = {}

    class _StubResp:
        status = 200
        async def text(self): return "{}"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _StubSess:
        @property
        def closed(self): return False
        async def close(self): pass
        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _StubResp()

    client = SagaClient("http://stub:3002")
    client._session = _StubSess()  # type: ignore[assignment]

    await client.query("q1", top_k=5)
    assert "min_confidence_tier" not in captured["body"]

    await client.query("q2", top_k=5, min_confidence_tier="medium")
    assert captured["body"]["min_confidence_tier"] == "medium"


@pytest.mark.asyncio
async def test_saga_query_handles_saga_error_gracefully():
    fake = FakeSaga(fail_on={"query"})
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")
    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await saga_query.handler({"query": "x"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True
    assert ctx.saga_atom_ids == []


@pytest.mark.asyncio
async def test_saga_feedback_maps_signal_to_outcome_vocab():
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_feedback = _by_name(tools, "saga_feedback")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await saga_feedback.handler({"atom_id": "a1", "signal": "useful"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is not True

    payload = fake.last("outcome")
    assert payload["atom_ids"] == ["a1"]
    assert payload["feedback"] == "positive"
    assert payload["session_id"] == "saga-c1-1"


@pytest.mark.asyncio
async def test_saga_feedback_rejects_unknown_signal():
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_feedback = _by_name(tools, "saga_feedback")
    out = await saga_feedback.handler({"atom_id": "a1", "signal": "fancy"})
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_saga_mark_contributions_passes_session_id():
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    mark = _by_name(tools, "saga_mark_contributions")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await mark.handler({
            "atom_ids": ["a1", "a2"],
            "response_text": "ok",
        })
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is not True
    payload = fake.last("feedback")
    assert payload["atom_ids"] == ["a1", "a2"]
    assert payload["session_id"] == "saga-c1-1"


@pytest.mark.asyncio
async def test_saga_end_session_drops_empty_optionals():
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    end = _by_name(tools, "saga_end_session")

    out = await end.handler({
        "session_id": "saga-c1-1",
        "summary": "we discussed quantum",
        "topics_discussed": ["quantum"],
        "decisions_made": [],
        "unfinished": [],
        "emotional_state": "",
    })
    assert out.get("is_error") is not True
    payload = fake.last("end_session")
    assert payload["topics_discussed"] == ["quantum"]
    # Empty lists/strings drop on the wire.
    assert payload["decisions_made"] is None
    assert payload["unfinished"] is None
    assert payload["emotional_state"] is None


@pytest.mark.asyncio
async def test_saga_end_session_appends_to_local_mirror(tmp_path):
    """v0.4 §3a: a successful end_session also writes a local mirror
    record so the prompt-time render can fall back if SAGA is briefly
    down. Mirror writing is best-effort; never fails the tool turn."""
    import json

    from mimir.session_boundary_log import SessionBoundaryLog

    fake = FakeSaga()
    mirror = SessionBoundaryLog(path=tmp_path / ".mimir" / "sb.jsonl")
    tools = build_saga_tools(fake, session_boundary_log=mirror)  # type: ignore[arg-type]
    end = _by_name(tools, "saga_end_session")

    out = await end.handler({
        "session_id": "saga-x-1",
        "summary": "wrap-up",
        "topics_discussed": ["alpha"],
        "decisions_made": [],
        "unfinished": ["draft response"],
        "emotional_state": "",
    })
    assert out.get("is_error") is not True

    body = (tmp_path / ".mimir" / "sb.jsonl").read_text()
    rec = json.loads(body.splitlines()[0])
    assert rec["saga_session_id"] == "saga-x-1"
    assert rec["summary"] == "wrap-up"
    assert rec["unfinished"] == ["draft response"]
    assert rec["topics_discussed"] == ["alpha"]
    assert rec["atom_id"] == fake.end_session_atom_id


@pytest.mark.asyncio
async def test_saga_end_session_flips_ctx_flag(tmp_path):
    """CR#19: a successful end_session call flips
    ``ctx.saga_end_session_called = True`` so the agent's post-message
    hook can tell that step 3 of the synthesis prompt actually ran.
    Without this flag the synthesis-skipped-boundary check has nothing
    to reference."""
    from mimir import _context
    from mimir.models import TurnContext

    ctx = TurnContext(
        turn_id="t-synth-1",
        session_id="c-x",
        trigger="saga_session_end",
        channel_id="c-x",
        started_at=0.0,
    )
    assert ctx.saga_end_session_called is False  # default
    token = _context.set_current_turn(ctx)
    try:
        fake = FakeSaga()
        tools = build_saga_tools(fake)  # type: ignore[arg-type]
        end = _by_name(tools, "saga_end_session")
        out = await end.handler({
            "session_id": "saga-x-1",
            "summary": "ok",
        })
        assert out.get("is_error") is not True
        assert ctx.saga_end_session_called is True
    finally:
        _context.reset_current_turn(token)


@pytest.mark.asyncio
async def test_saga_end_session_does_not_flip_ctx_flag_on_failure():
    """CR#19: when SAGA raises, the flag must stay False so the
    post-message check reports the real outcome. (Synthesis turn fired
    the tool but the boundary atom didn't actually land.)"""
    from mimir import _context
    from mimir.models import TurnContext
    from mimir.saga_client import SagaError

    class FailingSaga(FakeSaga):
        async def end_session(self, **kwargs):  # type: ignore[override]
            raise SagaError("simulated server error")

    ctx = TurnContext(
        turn_id="t-synth-2",
        session_id="c-y",
        trigger="saga_session_end",
        channel_id="c-y",
        started_at=0.0,
    )
    token = _context.set_current_turn(ctx)
    try:
        tools = build_saga_tools(FailingSaga())  # type: ignore[arg-type]
        end = _by_name(tools, "saga_end_session")
        out = await end.handler({
            "session_id": "saga-y-1",
            "summary": "ok",
        })
        # Tool returns an error block but doesn't raise.
        assert out.get("is_error") is True
        # Flag stays False — the check sees the real failure.
        assert ctx.saga_end_session_called is False
    finally:
        _context.reset_current_turn(token)


@pytest.mark.asyncio
async def test_saga_end_session_resolves_via_saga_session_id_under_sdk_fork(
    monkeypatch,
):
    """chainlink #23 subissue #25: when the MCP handler is dispatched on
    a fresh-context asyncio task (the SDK's production path), the
    ``_current_turn`` ContextVar is invisible inside the handler — but
    the turn is still registered in ``_active_turns`` keyed by turn_id.

    The fix in ``saga_end_session`` is to look up the turn by the
    ``session_id`` arg the model already passes (= ``ctx.saga_session_id``)
    rather than relying on contextvar inheritance. This regression test
    drives the handler through ``dispatch_via_sdk_task_fork`` to prove
    the fix: ``ctx.saga_end_session_called`` flips True even though the
    contextvar is invisible inside the forked handler. Without the fix
    the assertion fails (the handler can't see the ctx and the flag
    stays False — which is the production false-positive
    ``synth_skip_boundary`` algedonic at the heart of chainlink #23)."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", fake_log_event)

    ctx = TurnContext(
        turn_id="t-fork-1",
        session_id="c-fork",
        trigger="saga_session_end",
        channel_id="c-fork",
        started_at=0.0,
        saga_session_id="saga-fork-1",
    )
    token = _context.set_current_turn(ctx)
    try:
        fake = FakeSaga()
        tools = build_saga_tools(fake)  # type: ignore[arg-type]
        end = _by_name(tools, "saga_end_session")
        out = await dispatch_via_sdk_task_fork(
            end.handler,
            {"session_id": "saga-fork-1", "summary": "done"},
        )
        assert out.get("is_error") is not True
        # The flag must flip even though the handler ran in a
        # fresh-context fork that can't see the contextvar.
        assert ctx.saga_end_session_called is True
    finally:
        _context.reset_current_turn(token)

    # Resolution path observability: this dispatch hit the
    # saga_session_id-based lookup, not the contextvar fallback.
    resolution_events = [
        f for k, f in captured if k == "saga_synthesis_ctx_resolution"
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0]["resolution_path"] == "saga_session_id"
    assert resolution_events[0]["saga_session_id"] == "saga-fork-1"
    assert resolution_events[0]["turn_id"] == "t-fork-1"


@pytest.mark.asyncio
async def test_saga_end_session_resolution_path_single_active_in_direct_call(
    monkeypatch,
):
    """When the session_id arg doesn't match any registered turn but
    exactly one turn is active, the resolution chain falls through to
    ``single_active`` (the unique-active heuristic from the lifted
    ``resolve_active_ctx`` shared with saga_query/store/feedback).

    Pre-migration this hit the bare ``get_current_turn()`` contextvar
    fallback and recorded ``contextvar``. Post-migration ``single_active``
    catches it first since the test setup leaves exactly one turn
    registered. ``contextvar`` is now the path only when zero turns are
    registered — that scenario is the orphan-call test below."""
    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", fake_log_event)

    # ctx with NO saga_session_id set (default None) — so the
    # saga_session_id lookup misses; with one active turn, single_active
    # picks it up.
    ctx = TurnContext(
        turn_id="t-direct-1",
        session_id="c-direct",
        trigger="saga_session_end",
        channel_id="c-direct",
        started_at=0.0,
    )
    token = _context.set_current_turn(ctx)
    try:
        fake = FakeSaga()
        tools = build_saga_tools(fake)  # type: ignore[arg-type]
        end = _by_name(tools, "saga_end_session")
        out = await end.handler({
            "session_id": "saga-direct-1",
            "summary": "ok",
        })
        assert out.get("is_error") is not True
        assert ctx.saga_end_session_called is True
    finally:
        _context.reset_current_turn(token)

    resolution_events = [
        f for k, f in captured if k == "saga_synthesis_ctx_resolution"
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0]["resolution_path"] == "single_active"


@pytest.mark.asyncio
async def test_saga_end_session_resolution_path_missing_when_no_ctx(
    monkeypatch,
):
    """No turn registered + contextvar not set: the handler still
    succeeds at the SAGA level (the call doesn't depend on ctx) but
    the flag-flip is a no-op. resolution_path=missing surfaces the
    rate of orphaned-call cases for monitoring."""
    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", fake_log_event)

    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    end = _by_name(tools, "saga_end_session")
    out = await end.handler({
        "session_id": "saga-orphan-1",
        "summary": "ok",
    })
    assert out.get("is_error") is not True

    resolution_events = [
        f for k, f in captured if k == "saga_synthesis_ctx_resolution"
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0]["resolution_path"] == "missing"
    assert resolution_events[0]["turn_id"] is None


@pytest.mark.asyncio
async def test_saga_end_session_no_mirror_when_log_unset():
    """build_saga_tools without a SessionBoundaryLog must still work —
    the mirror is optional; absent means no mirror writes."""
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    end = _by_name(tools, "saga_end_session")
    out = await end.handler({"session_id": "x", "summary": "y"})
    assert out.get("is_error") is not True


@pytest.mark.asyncio
async def test_saga_store_passes_through():
    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    store = _by_name(tools, "saga_store")
    out = await store.handler({"content": "x", "stream": "semantic"})
    assert out.get("is_error") is not True
    payload = fake.last("store")
    assert payload["content"] == "x"
    assert payload["stream"] == "semantic"


@pytest.mark.asyncio
async def test_subagent_isolation_does_not_leak_atom_ids():
    """SPEC §9.3: subagents run in distinct asyncio tasks. ContextVars copy
    the parent's value at task creation, so when the *child* mutates its own
    TurnContext, the parent's list stays clean.

    We approximate the subagent boundary with ``asyncio.create_task`` — the
    child task's contextvars are a copy, not a shared reference."""
    fake = FakeSaga(
        query_response={"_raw_atoms": [{"id": "child-atom", "content": "x"}]}
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    parent_ctx = _ctx("parent")
    parent_token = _context.set_current_turn(parent_ctx)
    try:
        async def subagent_run():
            child_ctx = _ctx("child", saga_session_id="saga-child-1")
            child_token = _context.set_current_turn(child_ctx)
            try:
                await saga_query.handler({"query": "x", "top_k": 1})
            finally:
                _context.reset_current_turn(child_token)
            return child_ctx

        child = await asyncio.create_task(subagent_run())  # type: ignore[name-defined]
    finally:
        _context.reset_current_turn(parent_token)

    assert child.saga_atom_ids == ["child-atom"]
    assert parent_ctx.saga_atom_ids == []  # parent untouched


# ─── P42: triples surfacing ────────────────────────────────────────────


def test_triples_in_payload_extracts_list():
    from mimir.sagatools import _triples_in_payload
    payload = {
        "triples": [
            {"subject": "user", "predicate": "lives_in", "object": "Oakland"},
            {"subject": "user", "predicate": "profession", "object": "engineer"},
        ],
    }
    out = _triples_in_payload(payload)
    assert len(out) == 2


def test_triples_in_payload_returns_empty_when_missing():
    from mimir.sagatools import _triples_in_payload
    assert _triples_in_payload({}) == []
    assert _triples_in_payload({"triples": None}) == []
    assert _triples_in_payload({"triples": []}) == []


def test_format_triples_renders_with_dates_and_confidence():
    from mimir.sagatools import _format_triples
    out = _format_triples([
        {
            "subject": "user", "predicate": "subscription", "object": "pro",
            "valid_from": "2024-03-15T12:00:00+00:00",
            "valid_until": "2025-03-15T12:00:00+00:00",
            "confidence": 0.92,
        },
    ])
    assert "(user, subscription, pro)" in out
    assert "valid 2024-03-15 → 2025-03-15" in out
    assert "conf 0.92" in out


def test_format_triples_omits_dates_when_absent():
    from mimir.sagatools import _format_triples
    out = _format_triples([
        {"subject": "user", "predicate": "favorite_color", "object": "blue",
         "valid_from": None, "valid_until": None, "confidence": 1.0},
    ])
    assert "(user, favorite_color, blue)" in out
    assert "valid" not in out
    assert "conf" not in out  # confidence==1.0 is the default; omit


def test_format_triples_present_when_only_valid_from():
    from mimir.sagatools import _format_triples
    out = _format_triples([
        {"subject": "user", "predicate": "is_at", "object": "office",
         "valid_from": "2026-05-01T09:00:00+00:00",
         "valid_until": None, "confidence": 1.0},
    ])
    assert "valid 2026-05-01 → present" in out


def test_format_saga_payload_combines_atoms_and_triples():
    from mimir.sagatools import _format_saga_payload
    payload = {
        "observations": [{"id": "o1", "content": "User works on saga.",
                          "memory_type": "observation"}],
        "raws": [],
        "triples": [
            {"subject": "user", "predicate": "project", "object": "saga",
             "valid_from": None, "valid_until": None, "confidence": 1.0},
        ],
    }
    out = _format_saga_payload(payload)
    assert "User works on saga." in out
    assert "Triples:" in out
    assert "(user, project, saga)" in out


def test_format_saga_payload_atoms_only_omits_triples_section():
    from mimir.sagatools import _format_saga_payload
    payload = {
        "atoms": [{"id": "a1", "content": "hello"}],
    }
    out = _format_saga_payload(payload)
    assert "hello" in out
    assert "Triples:" not in out


def test_source_atom_ids_from_triples_dedups_and_orders():
    """Each triple's source_atom_id flows into ctx.saga_atom_ids so the
    post-message hook credits the originating atom via
    mark_contributions. Same path as for surfaced atoms."""
    from mimir.sagatools import _source_atom_ids_from_triples
    payload = {
        "triples": [
            {"subject": "user", "predicate": "p1", "object": "o1",
             "source_atom_id": "atom-A"},
            {"subject": "user", "predicate": "p2", "object": "o2",
             "source_atom_id": "atom-B"},
            {"subject": "user", "predicate": "p3", "object": "o3",
             "source_atom_id": "atom-A"},  # duplicate — dropped
        ],
    }
    out = _source_atom_ids_from_triples(payload)
    assert out == ["atom-A", "atom-B"]


def test_source_atom_ids_skips_missing_field():
    """Legacy / non-P42 responses without source_atom_id are silently
    skipped — never crashes the credit pass."""
    from mimir.sagatools import _source_atom_ids_from_triples
    payload = {
        "triples": [
            {"subject": "user", "predicate": "p", "object": "o"},  # no source_atom_id
            {"subject": "user", "predicate": "p2", "object": "o2",
             "source_atom_id": "atom-X"},
            {"subject": "user", "predicate": "p3", "object": "o3",
             "source_atom_id": ""},  # empty string — skip
        ],
    }
    out = _source_atom_ids_from_triples(payload)
    assert out == ["atom-X"]


def test_source_atom_ids_empty_when_no_triples():
    from mimir.sagatools import _source_atom_ids_from_triples
    assert _source_atom_ids_from_triples({}) == []
    assert _source_atom_ids_from_triples({"triples": []}) == []


def test_format_saga_payload_triples_only_renders():
    """When P42 is on but the atom pathways returned nothing, the
    triples block alone is still surfaced."""
    from mimir.sagatools import _format_saga_payload
    payload = {
        "observations": [], "raws": [],
        "triples": [
            {"subject": "user", "predicate": "lives_in", "object": "Oakland",
             "valid_from": None, "valid_until": None, "confidence": 1.0},
        ],
    }
    out = _format_saga_payload(payload)
    assert "Triples:" in out
    assert "(user, lives_in, Oakland)" in out


# ---- chainlink #23 subissue #26: saga_query / saga_store / saga_feedback /
# ---- saga_mark_contributions resolution-path tests under SDK task fork.
# ---- Same shape as the saga_end_session tests above (subissue #25), one
# ---- per tool × resolution path. These are the regression net for
# ---- the Option P fix.


@pytest.mark.asyncio
async def test_saga_query_resolves_via_saga_session_id_under_sdk_fork(
    monkeypatch,
):
    """chainlink #23 #26: saga_query under fresh-context fork must still
    auto-credit retrieved atom_ids to the calling turn's saga_atom_ids
    list (SPEC §9.3 mid-turn tracking). The fix passes session_id as a
    tool arg; the handler resolves it via _resolve_ctx and finds the
    matching ctx in _active_turns."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", fake_log_event)

    fake = FakeSaga(
        query_response={
            "atoms": [
                {"id": "fork-a1", "stream": "semantic", "content": "x"},
                {"id": "fork-a2", "stream": "semantic", "content": "y"},
            ]
        }
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    ctx = _ctx(channel_id="c-fork-q", saga_session_id="saga-fork-q-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await dispatch_via_sdk_task_fork(
            saga_query.handler,
            {"query": "hi", "session_id": "saga-fork-q-1"},
        )
        assert out.get("is_error") is not True
        # The atom-credit invariant: ctx.saga_atom_ids populated even
        # though the handler ran in a fresh-context fork.
        assert ctx.saga_atom_ids == ["fork-a1", "fork-a2"]
    finally:
        _context.reset_current_turn(token)

    payload = fake.last("query")
    # session_id propagated to the saga client call.
    assert payload["session_id"] == "saga-fork-q-1"

    res_events = [f for k, f in captured if k == "saga_query_ctx_resolution"]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "saga_session_id"
    assert res_events[0]["turn_id"] == ctx.turn_id


@pytest.mark.asyncio
async def test_saga_query_falls_back_to_single_active_when_session_id_omitted(
    monkeypatch,
):
    """If the model omits session_id (legacy callers, prompt-tax slip),
    the lookup chain falls through to ``get_only_active_turn()``. Works
    in single-channel deployments where exactly one turn is in flight."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def _record(kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", _record)

    fake = FakeSaga(
        query_response={"atoms": [{"id": "single-1", "stream": "semantic", "content": "z"}]}
    )
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    ctx = _ctx(channel_id="c-single", saga_session_id="saga-single-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await dispatch_via_sdk_task_fork(
            saga_query.handler, {"query": "hi"}  # no session_id
        )
        assert out.get("is_error") is not True
        assert ctx.saga_atom_ids == ["single-1"]
    finally:
        _context.reset_current_turn(token)

    res_events = [f for k, f in captured if k == "saga_query_ctx_resolution"]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "single_active"


@pytest.mark.asyncio
async def test_saga_query_returns_missing_when_multi_active_and_no_session_id(
    monkeypatch,
):
    """Multi-channel concurrent turns + no session_id arg = the lookup
    chain has nothing to disambiguate. Returns ``missing``; handler
    runs but atom auto-credit silently no-ops. The observability event
    surfaces the rate of this case in production."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def _record(kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", _record)

    fake = FakeSaga(query_response={"atoms": [{"id": "x", "stream": "s", "content": "y"}]})
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_query = _by_name(tools, "saga_query")

    # Two concurrent turns registered → get_only_active_turn returns None.
    ctx_a = _ctx(channel_id="c-multi-a", saga_session_id="saga-multi-a")
    ctx_b = _ctx(channel_id="c-multi-b", saga_session_id="saga-multi-b")
    tok_a = _context.set_current_turn(ctx_a)
    # set_current_turn replaces _current_turn; manually inject ctx_b
    # into the registry so both are "active" without disturbing the
    # contextvar (the test's whole point is that contextvar is one
    # turn while two are in _active_turns).
    _context._active_turns[ctx_b.turn_id] = ctx_b
    try:
        out = await dispatch_via_sdk_task_fork(
            saga_query.handler, {"query": "hi"}  # no session_id
        )
        # Handler still succeeds — the fix is about ctx resolution, not
        # blocking the call. Atom auto-credit silently no-ops; the
        # missing-resolution-path event tells operators this is happening.
        assert out.get("is_error") is not True
        assert ctx_a.saga_atom_ids == []  # not credited (resolution=missing)
        assert ctx_b.saga_atom_ids == []  # not credited
    finally:
        _context._active_turns.pop(ctx_b.turn_id, None)
        _context.reset_current_turn(tok_a)

    res_events = [f for k, f in captured if k == "saga_query_ctx_resolution"]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "missing"
    assert res_events[0]["turn_id"] is None


@pytest.mark.asyncio
async def test_saga_feedback_resolves_via_saga_session_id_under_sdk_fork(
    monkeypatch,
):
    """saga_feedback under SDK fork must scope outcome by the calling
    turn's saga_session_id (otherwise SAGA records the outcome with no
    session attribution). Same lookup chain as saga_query."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def _record(kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", _record)

    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_feedback = _by_name(tools, "saga_feedback")

    ctx = _ctx(channel_id="c-fb", saga_session_id="saga-fb-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await dispatch_via_sdk_task_fork(
            saga_feedback.handler,
            {"atom_id": "a-1", "signal": "useful", "session_id": "saga-fb-1"},
        )
        assert out.get("is_error") is not True
    finally:
        _context.reset_current_turn(token)

    payload = fake.last("outcome")
    assert payload["session_id"] == "saga-fb-1"

    res_events = [f for k, f in captured if k == "saga_feedback_ctx_resolution"]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "saga_session_id"


@pytest.mark.asyncio
async def test_saga_mark_contributions_resolves_via_saga_session_id_under_sdk_fork(
    monkeypatch,
):
    """saga_mark_contributions under SDK fork must scope by the calling
    turn's saga_session_id (otherwise the credit is recorded against
    no session). Same lookup chain as saga_query."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    captured: list[tuple[str, dict]] = []

    async def _record(kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", _record)

    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_mc = _by_name(tools, "saga_mark_contributions")

    ctx = _ctx(channel_id="c-mc", saga_session_id="saga-mc-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await dispatch_via_sdk_task_fork(
            saga_mc.handler,
            {
                "atom_ids": ["a", "b"],
                "response_text": "thanks",
                "session_id": "saga-mc-1",
            },
        )
        assert out.get("is_error") is not True
    finally:
        _context.reset_current_turn(token)

    payload = fake.last("feedback")
    assert payload["session_id"] == "saga-mc-1"

    res_events = [
        f for k, f in captured if k == "saga_mark_contributions_ctx_resolution"
    ]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "saga_session_id"


@pytest.mark.asyncio
async def test_saga_store_logs_resolution_path_for_observability_parity(
    monkeypatch,
):
    """saga_store doesn't currently scope storage by session (the saga
    client interface doesn't accept session_id), but the handler logs
    saga_store_ctx_resolution for observability parity with the other
    tools. This way the introspection-report's resolution-path
    histogram covers all four saga tools uniformly, and a future
    wire-up of session-scoped storage doesn't need a separate event
    rollout."""
    captured: list[tuple[str, dict]] = []

    async def _record(kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.sagatools.log_event", _record)

    fake = FakeSaga()
    tools = build_saga_tools(fake)  # type: ignore[arg-type]
    saga_store = _by_name(tools, "saga_store")

    ctx = _ctx(channel_id="c-st", saga_session_id="saga-st-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await saga_store.handler(
            {"content": "remember", "stream": "semantic", "session_id": "saga-st-1"}
        )
        assert out.get("is_error") is not True
    finally:
        _context.reset_current_turn(token)

    res_events = [f for k, f in captured if k == "saga_store_ctx_resolution"]
    assert len(res_events) == 1
    assert res_events[0]["resolution_path"] == "saga_session_id"


# Late import to keep the file's main imports compact.
import asyncio  # noqa: E402
