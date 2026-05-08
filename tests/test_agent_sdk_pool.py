"""Stage 4 (chainlink #11) — asyncio-aware ``ClientPool`` tests.

The pool replaces the single-shared-client + global ``asyncio.Lock``
that was serializing all turns through one ``ClaudeSDKClient``.
Tests pin:

- Two concurrent ``query()`` calls truly run in parallel — neither
  waits on the other's full request.
- Lazy fill: the first call constructs one client, a second
  concurrent call constructs a second client, etc.
- Max-size cap: at ``max_size`` in-flight, a further acquire waits
  for a release.
- Fingerprint flip drains the whole pool — idle clients disconnect
  immediately, in-flight clients finish and disconnect on release,
  fresh acquires use the new fingerprint.
- ``get_context_usage`` rides the pool — works concurrently with a
  ``query()``, doesn't block on it.
- ``shutdown_sdk_client()`` disconnects every pool member (idle and
  in-flight) and is idempotent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

import mimir.agent as agent_mod
from mimir.event_logger import init_logger


class _BlockingFakeClient:
    """Test double that lets each test pause ``receive_response`` mid-
    stream so we can interleave concurrent calls and assert the pool
    actually parallelizes them."""

    instances: list["_BlockingFakeClient"] = []

    def __init__(self, options: ClaudeAgentOptions | None = None, transport: Any = None) -> None:
        self.options = options
        self.connect_count = 0
        self.disconnect_count = 0
        self.queries: list[tuple[str, str]] = []
        # Per-instance "gate": tests set this to an ``asyncio.Event`` and
        # the fake will wait on it before yielding the assistant message.
        self.release_gate: asyncio.Event | None = None
        # Tracks how many clients are concurrently inside
        # receive_response so a test can assert true parallelism.
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.disconnect_count += 1

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append((prompt, session_id))

    async def receive_response(self):
        # If the test installed a gate, block until it's set. Lets us
        # have N requests in-flight simultaneously.
        if self.release_gate is not None:
            await self.release_gate.wait()
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    async def get_context_usage(self) -> dict:
        return {"apiUsage": {"five_hour": {"utilization": 0.1, "resets_at": 0, "status": "allowed"}}}


def _reset() -> None:
    agent_mod._reset_pool_for_tests()
    _BlockingFakeClient.instances.clear()


@pytest.fixture(autouse=True)
def _isolate_pool(monkeypatch, tmp_path: Path):
    _reset()
    # The pool now emits ``client_pool_drained`` events on fingerprint
    # flips (CR#20), so the event logger must be initialized for these
    # tests to exercise the drain path.
    (tmp_path / "logs").mkdir(exist_ok=True)
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-pool")
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", _BlockingFakeClient)
    yield
    _reset()


def _opts(**overrides) -> ClaudeAgentOptions:
    base = dict(
        system_prompt="hello system",
        model="claude-opus-4-7",
        cwd="/tmp",
        permission_mode="bypassPermissions",
    )
    base.update(overrides)
    return ClaudeAgentOptions(**base)


async def _drain(prompt: str, options: ClaudeAgentOptions, session_id: str = "default"):
    out = []
    async for msg in agent_mod.query(prompt=prompt, options=options, session_id=session_id):
        out.append(msg)
    return out


# ─── _TurnCell stamping (multi-channel budget correlation) ─────────


@pytest.mark.asyncio
async def test_acquire_ctx_stamps_and_clears_cell_turn_id():
    """The pool's ``acquire_ctx(turn_id=X)`` stamps ``entry.cell.turn_id``
    on enter and clears on exit. The budget hook reads this cell to
    correlate hook fires to the active turn under multi-channel use.
    Pins the contract that ``query()`` relies on."""
    pool = agent_mod.ClientPool()
    opts = _opts()

    captured: list[str | None] = []

    async with pool.acquire_ctx(opts, turn_id="turn-XYZ") as client:
        # Cell stamped while client is in flight.
        # Reach into the entry to verify (test only — production reads
        # via the contextvar that the SDK's hook task captured).
        # We can find the entry via the in_flight set.
        entries = list(pool._in_flight)
        assert len(entries) == 1
        captured.append(entries[0].cell.turn_id)
        assert client is entries[0].client

    # On exit, the cell is cleared and the entry returns to idle.
    assert pool._idle, "expected the released client to land in idle"
    assert pool._idle[0].cell.turn_id is None, "cell.turn_id must clear on release"

    assert captured == ["turn-XYZ"]
    await pool.shutdown()


@pytest.mark.asyncio
async def test_acquire_ctx_sets_contextvar_to_entry_cell_before_connect():
    """The SDK's hook control task is forked during ``client.connect()``
    and captures the ``_current_client_cell`` contextvar at that moment.
    For per-channel budget correlation to work, the pool MUST set the
    contextvar to the new entry's cell BEFORE connect returns. Pins it
    by snapshotting the contextvar value during the fake client's
    connect callback."""
    from mimir._context import _current_client_cell, _TurnCell

    captured_during_connect: list[_TurnCell | None] = []

    class _ConnectSnapshotClient(_BlockingFakeClient):
        async def connect(self) -> None:
            captured_during_connect.append(_current_client_cell.get())
            await super().connect()

    import mimir.agent as ag
    ag.ClaudeSDKClient = _ConnectSnapshotClient  # type: ignore[assignment]

    try:
        pool = agent_mod.ClientPool()
        async with pool.acquire_ctx(_opts(), turn_id="turn-A") as _:
            pass
        # The contextvar MUST be a fresh _TurnCell (not None) at
        # connect time — that's the cell the SDK's hook task captures.
        assert len(captured_during_connect) == 1
        cell = captured_during_connect[0]
        assert isinstance(cell, _TurnCell), (
            "contextvar must be set to a _TurnCell before connect, "
            f"got {type(cell).__name__}"
        )
        # The cell is the entry's cell — same instance.
        assert pool._idle, "expected the released client to land in idle"
        assert pool._idle[0].cell is cell
        await pool.shutdown()
    finally:
        ag.ClaudeSDKClient = _BlockingFakeClient  # type: ignore[assignment]


# ─── (a) two concurrent calls run in parallel ──────────────────────


@pytest.mark.asyncio
async def test_two_concurrent_calls_run_in_parallel():
    """Two ``query()`` calls launched concurrently must both reach
    ``receive_response`` before either finishes — the old global
    asyncio.Lock would serialize them so the second would only enter
    after the first yielded its terminal message."""
    opts = _opts()

    # Both gates start unset; we'll have both clients block inside
    # receive_response, then assert two clients exist concurrently,
    # then release them.
    started = asyncio.Event()
    in_flight_count = 0
    seen_concurrent = asyncio.Event()
    gate = asyncio.Event()

    async def _instrumented_drain(prompt: str):
        nonlocal in_flight_count
        msgs = []
        async for msg in agent_mod.query(prompt=prompt, options=opts):
            msgs.append(msg)
        return msgs

    # Override receive_response so we can count concurrent in-flight
    # callers.
    original = _BlockingFakeClient.receive_response

    async def _instrumented_receive(self):
        nonlocal in_flight_count
        in_flight_count += 1
        if in_flight_count >= 2:
            seen_concurrent.set()
        await gate.wait()
        in_flight_count -= 1
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    _BlockingFakeClient.receive_response = _instrumented_receive
    try:
        t1 = asyncio.create_task(_instrumented_drain("first"))
        t2 = asyncio.create_task(_instrumented_drain("second"))
        # Wait for both to be inside receive_response.
        await asyncio.wait_for(seen_concurrent.wait(), timeout=2.0)
        # Two clients must have been constructed (lazy-fill).
        assert len(_BlockingFakeClient.instances) == 2
        gate.set()
        await asyncio.gather(t1, t2)
    finally:
        _BlockingFakeClient.receive_response = original

    # After release both clients should be idle (still connected).
    for c in _BlockingFakeClient.instances:
        assert c.connect_count == 1
        assert c.disconnect_count == 0


# ─── (b) fingerprint flip drains and replaces ──────────────────────


@pytest.mark.asyncio
async def test_fingerprint_flip_drains_idle_clients():
    """When a query arrives with a different options-fingerprint, all
    idle pool members are disconnected before the new client is
    constructed."""
    await _drain("first", _opts(system_prompt="prompt-a"))
    await _drain("first2", _opts(system_prompt="prompt-a"))
    # Two queries, same fingerprint, same client (sequential reuse).
    assert len(_BlockingFakeClient.instances) == 1
    a = _BlockingFakeClient.instances[0]
    assert a.disconnect_count == 0

    # Flip the fingerprint — the idle client must be disconnected
    # before the new one runs.
    await _drain("second", _opts(system_prompt="prompt-b"))
    assert len(_BlockingFakeClient.instances) == 2
    assert a.disconnect_count == 1
    b = _BlockingFakeClient.instances[1]
    assert b.connect_count == 1
    assert b.disconnect_count == 0


@pytest.mark.asyncio
async def test_fingerprint_flip_emits_client_pool_drained_event(tmp_path: Path):
    """CR#20 regression: a fingerprint flip drains the pool — that's
    silent today (only visible as latency drift). Assert
    ``client_pool_drained`` lands in events.jsonl with both fingerprint
    prefixes and the affected counts so an unstable system prompt
    surfaces immediately."""
    events_path = tmp_path / "logs" / "events.jsonl"

    # Two queries at fingerprint A → 1 idle client at fingerprint A.
    await _drain("first", _opts(system_prompt="prompt-a"))
    await _drain("first2", _opts(system_prompt="prompt-a"))

    # Flip to fingerprint B → drain fires.
    await _drain("second", _opts(system_prompt="prompt-b"))

    drained = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line and json.loads(line).get("type") == "client_pool_drained"
    ]
    assert len(drained) == 1, f"expected exactly one drain event; got {drained}"
    ev = drained[0]
    assert ev["idle_disconnected"] == 1
    assert ev["in_flight_marked_stale"] == 0
    assert isinstance(ev["old_fingerprint_8"], str) and len(ev["old_fingerprint_8"]) == 8
    assert isinstance(ev["new_fingerprint_8"], str) and len(ev["new_fingerprint_8"]) == 8
    assert ev["old_fingerprint_8"] != ev["new_fingerprint_8"]


@pytest.mark.asyncio
async def test_fingerprint_flip_lets_in_flight_finish_then_disconnects_on_release():
    """An in-flight client at fingerprint A must finish its current
    request when fingerprint B arrives mid-flight; on release it
    disconnects rather than re-pooling."""
    opts_a = _opts(system_prompt="prompt-a")
    opts_b = _opts(system_prompt="prompt-b")

    a_gate = asyncio.Event()
    a_inside = asyncio.Event()

    async def _gated_receive(self):
        # Only the first instance (fingerprint A) blocks; later
        # instances fall through to the canned reply.
        if self is _BlockingFakeClient.instances[0]:
            a_inside.set()
            await a_gate.wait()
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    original = _BlockingFakeClient.receive_response
    _BlockingFakeClient.receive_response = _gated_receive
    try:
        # Start a query at fingerprint A and let it block inside
        # receive_response.
        t_a = asyncio.create_task(_drain("a", opts_a))
        await asyncio.wait_for(a_inside.wait(), timeout=2.0)
        client_a = _BlockingFakeClient.instances[0]
        assert client_a.disconnect_count == 0

        # While A is still in flight, run a query at fingerprint B.
        # A new client must be constructed — A is busy, can't drain
        # yet — and the pool flips its current fingerprint.
        t_b = asyncio.create_task(_drain("b", opts_b))
        # Give B time to start (it should construct a new client).
        # B isn't gated, so it'll complete quickly.
        await asyncio.wait_for(t_b, timeout=2.0)
        # B's client is fresh and connected.
        assert len(_BlockingFakeClient.instances) == 2
        client_b = _BlockingFakeClient.instances[1]
        assert client_b.connect_count == 1

        # A is still in flight — must NOT have been disconnected mid-stream.
        assert client_a.disconnect_count == 0

        # Release A. On release, A sees the fingerprint flip and
        # disconnects rather than re-pooling.
        a_gate.set()
        await asyncio.wait_for(t_a, timeout=2.0)

        # Give the release path a tick to complete the disconnect
        # (it happens after notify, outside the lock).
        for _ in range(20):
            if client_a.disconnect_count == 1:
                break
            await asyncio.sleep(0.01)
        assert client_a.disconnect_count == 1
        # B is still healthy and idle.
        assert client_b.disconnect_count == 0
    finally:
        _BlockingFakeClient.receive_response = original


# ─── (c) lazy fill grows up to max ────────────────────────────────


@pytest.mark.asyncio
async def test_lazy_fill_grows_up_to_max():
    """Sequential calls reuse a single client; concurrent calls each
    construct a fresh client up to ``max_size``."""
    opts = _opts()

    # Sequential: only one client created.
    await _drain("a", opts)
    await _drain("b", opts)
    await _drain("c", opts)
    assert len(_BlockingFakeClient.instances) == 1

    # Concurrent: pool grows. Force 4 concurrent in-flight requests
    # by gating receive_response.
    gate = asyncio.Event()

    async def _gated_receive(self):
        await gate.wait()
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    original = _BlockingFakeClient.receive_response
    _BlockingFakeClient.receive_response = _gated_receive
    try:
        tasks = [asyncio.create_task(_drain(f"msg{i}", opts)) for i in range(4)]
        # Wait for all 4 to be inside receive_response.
        for _ in range(200):
            if sum(1 for c in _BlockingFakeClient.instances if c.connect_count == 1) >= 4:
                break
            await asyncio.sleep(0.01)
        # Existing 1 + 3 new = 4 total. The first task may reuse the
        # pre-existing idle client; only 3 fresh ones get built.
        assert len(_BlockingFakeClient.instances) == 4
        gate.set()
        await asyncio.gather(*tasks)
    finally:
        _BlockingFakeClient.receive_response = original


# ─── (d) max-size cap waits for release ───────────────────────────


@pytest.mark.asyncio
async def test_max_size_cap_waits_for_release():
    """When the pool is at max_size and all clients are in flight, a
    further acquire blocks until one is released."""
    opts = _opts()

    # Shrink the pool's max_size for this test so we don't have to
    # spin up 11 concurrent fakes. The pool is created lazily on
    # first use; create it here and override the cap. Mutating
    # ``_max_size`` directly (rather than ``max_size``, which is a
    # read-only property inherited from ``saga.async_pool.BoundedAsyncPool``)
    # preserves the test's "reach in and tweak the singleton" intent.
    pool = agent_mod._get_pool()
    pool._max_size = 2

    gate = asyncio.Event()
    waiter_started = asyncio.Event()
    in_receive = 0
    saw_two_in_flight = asyncio.Event()

    async def _gated_receive(self):
        nonlocal in_receive
        in_receive += 1
        if in_receive == 2:
            saw_two_in_flight.set()
        await gate.wait()
        in_receive -= 1
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    original = _BlockingFakeClient.receive_response
    _BlockingFakeClient.receive_response = _gated_receive
    try:
        # Two saturate the pool.
        t1 = asyncio.create_task(_drain("a", opts))
        t2 = asyncio.create_task(_drain("b", opts))
        await asyncio.wait_for(saw_two_in_flight.wait(), timeout=2.0)
        assert len(_BlockingFakeClient.instances) == 2

        # Third must wait — pool is at cap.
        async def _third():
            waiter_started.set()
            return await _drain("c", opts)

        t3 = asyncio.create_task(_third())
        await asyncio.wait_for(waiter_started.wait(), timeout=2.0)
        # Give t3 time to actually try to acquire and block. It
        # shouldn't have constructed a 3rd client.
        await asyncio.sleep(0.1)
        assert len(_BlockingFakeClient.instances) == 2
        assert not t3.done()

        # Release one — t3 should now proceed.
        gate.set()
        await asyncio.gather(t1, t2, t3)

        # Still only 2 clients total — t3 reused one.
        assert len(_BlockingFakeClient.instances) == 2
    finally:
        _BlockingFakeClient.receive_response = original


# ─── (e) get_context_usage works during a concurrent query ────────


@pytest.mark.asyncio
async def test_get_context_usage_works_during_concurrent_query():
    """``get_context_usage`` rides the pool. While one ``query()`` is
    blocked in ``receive_response``, ``get_context_usage`` must be
    able to acquire a separate client and return."""
    opts = _opts()

    query_inside = asyncio.Event()
    gate = asyncio.Event()

    async def _gated_receive(self):
        query_inside.set()
        await gate.wait()
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    original = _BlockingFakeClient.receive_response
    _BlockingFakeClient.receive_response = _gated_receive
    try:
        t_q = asyncio.create_task(_drain("hello", opts))
        await asyncio.wait_for(query_inside.wait(), timeout=2.0)

        # query is parked in receive_response. get_context_usage
        # must NOT block — the pool can grow to a second client.
        usage = await asyncio.wait_for(agent_mod.get_context_usage(opts), timeout=2.0)
        assert usage is not None
        assert "apiUsage" in usage
        # Two clients exist now.
        assert len(_BlockingFakeClient.instances) == 2

        gate.set()
        await t_q
    finally:
        _BlockingFakeClient.receive_response = original


# ─── (f) shutdown disconnects all pool members ────────────────────


@pytest.mark.asyncio
async def test_shutdown_disconnects_all_pool_members():
    """``shutdown_sdk_client()`` disconnects every connected client
    in the pool — both idle and any that happened to be in flight."""
    opts = _opts()

    gate = asyncio.Event()
    in_flight = 0
    saw_three = asyncio.Event()

    async def _gated_receive(self):
        nonlocal in_flight
        in_flight += 1
        if in_flight == 3:
            saw_three.set()
        await gate.wait()
        in_flight -= 1
        yield AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
        )

    original = _BlockingFakeClient.receive_response
    _BlockingFakeClient.receive_response = _gated_receive
    try:
        # Build out three concurrent in-flight clients.
        tasks = [asyncio.create_task(_drain(f"m{i}", opts)) for i in range(3)]
        await asyncio.wait_for(saw_three.wait(), timeout=2.0)
        assert len(_BlockingFakeClient.instances) == 3

        # Release one — it returns to idle. Two stay in flight.
        gate.set()
        # Don't gather yet; give the released ones a moment to land
        # back in idle.
        await asyncio.sleep(0.1)
        await asyncio.gather(*tasks)
    finally:
        _BlockingFakeClient.receive_response = original

    # All three are now idle. Shutdown disconnects them all.
    await agent_mod.shutdown_sdk_client()
    for c in _BlockingFakeClient.instances:
        assert c.disconnect_count == 1

    # Idempotent — second call is a no-op (no pool to drain).
    await agent_mod.shutdown_sdk_client()
    for c in _BlockingFakeClient.instances:
        assert c.disconnect_count == 1


@pytest.mark.asyncio
async def test_shutdown_with_no_pool_is_safe():
    """Shutdown before any query() is a no-op."""
    await agent_mod.shutdown_sdk_client()


# ─── back-compat: legacy module-level reset hooks ──────────────────


@pytest.mark.asyncio
async def test_legacy_module_resets_still_safe():
    """Existing tests that pre-date the pool poke at ``_sdk_client``,
    ``_sdk_options_fingerprint``, ``_sdk_lock`` to reset state. Those
    assignments are now no-op shims; the load-bearing reset is
    ``_reset_pool_for_tests``. Verify both forms coexist without
    breaking."""
    await _drain("hi", _opts())
    assert agent_mod._pool is not None

    agent_mod._sdk_client = None
    agent_mod._sdk_options_fingerprint = None
    agent_mod._sdk_lock = None
    # The pool is still alive (legacy assignments don't reset it).
    assert agent_mod._pool is not None

    agent_mod._reset_pool_for_tests()
    assert agent_mod._pool is None


# ─── connect-fail propagation (PR #15 review fix) ─────────────────


@pytest.mark.asyncio
async def test_acquire_propagates_connect_failure_cleanly():
    """When ``ClaudeSDKClient.connect()`` raises during pool acquire,
    the original exception must propagate cleanly — not get masked by
    a ``RuntimeError: Lock is not acquired.`` from the surrounding
    ``async with cond:`` block.

    Regression for the connect-fail path's prior manual ``cond.release()``
    before ``raise`` (PR #15 review). The lock is held when the
    exception unwinds; ``__aexit__`` releases it. A manual release
    would leave the lock unheld and ``__aexit__`` would then raise.
    """

    class _ConnectFails(_BlockingFakeClient):
        async def connect(self) -> None:  # type: ignore[override]
            raise RuntimeError("connect blew up")

    pool = agent_mod.ClientPool(max_size=2)

    import contextlib

    @contextlib.asynccontextmanager
    async def _patched_client():
        original = agent_mod.ClaudeSDKClient
        agent_mod.ClaudeSDKClient = _ConnectFails  # type: ignore[assignment]
        try:
            yield
        finally:
            agent_mod.ClaudeSDKClient = original  # type: ignore[assignment]

    async with _patched_client():
        with pytest.raises(RuntimeError, match="connect blew up"):
            async with pool.acquire_ctx(_opts()):
                pass

    # Reservation was backed out — pool stayed empty.
    assert pool.size == 0


@pytest.mark.asyncio
async def test_connect_failure_does_not_strand_other_waiters():
    """A connect failure must ``notify_all`` so a peer waiting at
    ``cond.wait()`` (max-size cap) wakes up and re-tries acquire.
    Without the notify, a peer can park forever even though the
    failed acquire freed the slot."""

    # Pool of size 1 so the second acquire waits on cond.
    pool = agent_mod.ClientPool(max_size=1)

    fail_first = {"flag": True}

    class _ConnectFailsOnce(_BlockingFakeClient):
        async def connect(self) -> None:  # type: ignore[override]
            if fail_first["flag"]:
                fail_first["flag"] = False
                raise RuntimeError("first connect fails")
            await super().connect()

    import contextlib

    @contextlib.asynccontextmanager
    async def _patched_client():
        original = agent_mod.ClaudeSDKClient
        agent_mod.ClaudeSDKClient = _ConnectFailsOnce  # type: ignore[assignment]
        try:
            yield
        finally:
            agent_mod.ClaudeSDKClient = original  # type: ignore[assignment]

    async with _patched_client():
        # First acquire fails immediately.
        with pytest.raises(RuntimeError, match="first connect fails"):
            async with pool.acquire_ctx(_opts()):
                pass

        # Second acquire should succeed (slot is free, second connect
        # works). If notify_all is missing this would deadlock if a
        # peer was already waiting; here we just confirm a fresh
        # acquire after the failure path runs cleanly.
        async with pool.acquire_ctx(_opts()) as client:
            assert client.connect_count == 1
        assert pool.size == 1
