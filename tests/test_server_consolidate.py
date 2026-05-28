"""Tests for POST /api/memory/consolidate guards (chainlink #233).

Pins three behaviors of the consolidate endpoint:
1. Concurrent calls — second one gets 429 while the first is inflight.
2. ``max_clusters`` and ``extra_canonical_subjects`` from the request
   body actually flow through to ``SagaClient.consolidate`` (they were
   silently dropped before).
3. ``max_clusters`` out of bounds (negative, zero, > 100, non-int,
   bool) returns 400.

The full ``build_app`` wires a real saga client + scheduler + dispatcher.
For these tests we exercise just the route handler with a stub
``saga_client`` whose ``consolidate`` we control.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.server import _ConsolidateGuard, _handle_consolidate


class _StubSagaClient:
    """Records the kwargs ``consolidate`` was called with, and optionally
    blocks on an event so a test can prove a second caller hits 429 while
    the first is still running.
    """

    def __init__(self, *, result: dict[str, Any] | None = None,
                 gate: asyncio.Event | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result if result is not None else {"ok": True}
        self._gate = gate

    async def consolidate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._gate is not None:
            await self._gate.wait()
        return self._result


def _build_app(saga_client: _StubSagaClient) -> web.Application:
    app = web.Application()
    app["saga_client"] = saga_client
    app["consolidate_guard"] = _ConsolidateGuard()
    app.router.add_post("/api/memory/consolidate", _handle_consolidate)
    return app


@pytest.mark.asyncio
async def test_consolidate_passes_dry_run_through() -> None:
    """Baseline: dry_run still works after the refactor."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"dry_run": True},
        )
    assert resp.status == 200
    assert len(stub.calls) == 1
    assert stub.calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_consolidate_plumbs_max_clusters() -> None:
    """max_clusters from the request body reaches SagaClient.consolidate
    (was silently dropped before chainlink #233)."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"max_clusters": 5},
        )
    assert resp.status == 200
    assert stub.calls[0]["max_clusters"] == 5


@pytest.mark.asyncio
async def test_consolidate_plumbs_extra_canonical_subjects() -> None:
    """extra_canonical_subjects flows through as a list of strings."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"extra_canonical_subjects": ["foo", "bar"]},
        )
    assert resp.status == 200
    assert stub.calls[0]["extra_canonical_subjects"] == ["foo", "bar"]


@pytest.mark.asyncio
async def test_consolidate_omitted_params_become_none() -> None:
    """When the body omits max_clusters / extra_canonical_subjects, we
    pass None — letting SagaClient's defaults apply."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post("/api/memory/consolidate", json={})
    assert resp.status == 200
    assert stub.calls[0]["max_clusters"] is None
    assert stub.calls[0]["extra_canonical_subjects"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0, -1, 101, 1000])
async def test_consolidate_max_clusters_out_of_bounds_returns_400(
    bad_value: int,
) -> None:
    """max_clusters must be 1..100. Anything outside is a 400, not silent
    pass-through that the worker would have to reject later."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"max_clusters": bad_value},
        )
    assert resp.status == 400
    assert stub.calls == []  # never reached the saga client


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [1.5, "5", True, [5], {"n": 5}])
async def test_consolidate_max_clusters_non_int_returns_400(
    bad_value: Any,
) -> None:
    """JSON's loose number/bool coercion mustn't slip through — we want a
    real int (bool is rejected even though ``isinstance(True, int)``)."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"max_clusters": bad_value},
        )
    assert resp.status == 400
    assert stub.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["not-a-list", [1, 2], [{"x": 1}], 42])
async def test_consolidate_extra_canonical_subjects_bad_shape_returns_400(
    bad_value: Any,
) -> None:
    """extra_canonical_subjects must be a list[str] when present."""
    stub = _StubSagaClient()
    async with TestClient(TestServer(_build_app(stub))) as client:
        resp = await client.post(
            "/api/memory/consolidate",
            json={"extra_canonical_subjects": bad_value},
        )
    assert resp.status == 400
    assert stub.calls == []


@pytest.mark.asyncio
async def test_consolidate_concurrent_call_returns_429() -> None:
    """First call holds the inflight flag; second concurrent call gets
    429 instead of starting a parallel LLM fan-out."""
    gate = asyncio.Event()
    stub = _StubSagaClient(gate=gate)

    async with TestClient(TestServer(_build_app(stub))) as client:
        # Fire first call; it will block on the gate.
        first = asyncio.create_task(
            client.post("/api/memory/consolidate", json={})
        )
        # Give the handler a chance to set _consolidate_inflight before
        # the second request lands.
        await asyncio.sleep(0.05)

        second = await client.post("/api/memory/consolidate", json={})
        assert second.status == 429
        body = await second.json()
        assert body["error"] == "consolidate already running"

        # Let the first call finish so the inflight flag clears.
        gate.set()
        first_resp = await first
        assert first_resp.status == 200

    # Only one consolidate call ever reached the saga client.
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_consolidate_inflight_clears_after_exception() -> None:
    """If the saga call raises, the inflight flag must still clear —
    otherwise the endpoint wedges permanently."""

    class _RaisingStub:
        calls: list[dict[str, Any]] = []

        async def consolidate(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            raise RuntimeError("boom")

    stub = _RaisingStub()
    app = _build_app(stub)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        first = await client.post("/api/memory/consolidate", json={})
        assert first.status == 500
        # Inflight must have cleared so the second call works.
        assert app["consolidate_guard"].inflight is False
        second = await client.post("/api/memory/consolidate", json={})
        assert second.status == 500
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_consolidate_inflight_clears_after_validation_400() -> None:
    """A 400 validation rejection must not leave inflight set — the
    guard only protects the expensive call path, not the body-parse
    pre-check."""
    stub = _StubSagaClient()
    app = _build_app(stub)
    async with TestClient(TestServer(app)) as client:
        bad = await client.post(
            "/api/memory/consolidate", json={"max_clusters": -1}
        )
        assert bad.status == 400
        # Validation rejected pre-guard → inflight was never set
        assert app["consolidate_guard"].inflight is False
        # And a real call still works.
        ok = await client.post("/api/memory/consolidate", json={})
        assert ok.status == 200
    assert len(stub.calls) == 1
