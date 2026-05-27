"""Tests for mimir/ntfy.py — phone-push alarm helper.

Covers the helper's eight contractual behaviors:

1. No NTFY_TOPIC env → emit ``ntfy_skip_no_topic``, no HTTP.
2. 2xx happy path → emit ``ntfy_post_ok`` (chainlink #65 paired
   positive), dedup table stamped, headers + body match.
3. Re-fire within dedup window → no-op (no HTTP, no event).
4. Re-fire after dedup window → posts again.
5. Network failure → ``ntfy_post_failed`` with error repr; no
   exception propagated.
6. HTTP 5xx → ``ntfy_post_failed`` with ``status=503`` and
   ``error="http_5xx"``.
7. HTTP 4xx → ``ntfy_post_rejected`` with ``status=400`` and a
   ``body_excerpt`` containing the response body.
8. Header serialization: priority is a string, tags are comma-joined.

The aiohttp mocking pattern matches ``tests/test_oauth_usage_poller.py``
— a tiny canned ``ClientSession`` stand-in. No real network is hit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest

from mimir import ntfy


# ─── aiohttp mocks ────────────────────────────────────────────────────


class _MockResponse:
    def __init__(self, status: int, body: str = ""):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _MockSession:
    """Minimal ``aiohttp.ClientSession`` stand-in.

    Records each ``post(...)`` call and returns the configured response
    (or raises ``post_exc`` if set, to simulate network failures).
    """

    def __init__(
        self,
        post_resp: _MockResponse | None = None,
        post_exc: BaseException | None = None,
    ):
        self.post_resp = post_resp
        self.post_exc = post_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, *, data=None, headers=None, **_):
        self.calls.append((url, {"data": data, "headers": headers}))
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_resp


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _MockSession) -> None:
    """Patch ``aiohttp.ClientSession`` in the ntfy module so the helper
    picks up our mock instead of opening a real socket."""
    monkeypatch.setattr(
        ntfy.aiohttp,
        "ClientSession",
        lambda *a, **kw: session,
    )


# ─── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_dedup() -> None:
    """Each test starts with an empty dedup table."""
    ntfy._reset_dedup_for_tests()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace ``ntfy.log_event`` with a recorder that captures
    (event_type, payload) tuples without touching the real logger."""
    events: list[tuple[str, dict]] = []

    async def _fake_log_event(event_type: str, **payload: Any) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(ntfy, "log_event", _fake_log_event)
    return events


# ─── tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_topic_emits_skip_event(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    # If the helper ever tried to open a real session, this would blow
    # up — proves no HTTP path was taken.
    monkeypatch.setattr(
        ntfy.aiohttp,
        "ClientSession",
        AsyncMock(side_effect=AssertionError("must not construct session")),
    )

    await ntfy.post_algedonic_alarm(
        category="cost-runaway",
        title="t",
        body="b",
        dedupe_key="cost-runaway:daily",
    )

    assert captured_events == [(
        "ntfy_skip_no_topic",
        {"category": "cost-runaway", "dedupe_key": "cost-runaway:daily"},
    )]


@pytest.mark.asyncio
async def test_happy_path_2xx_emits_post_ok(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """chainlink #65: 2xx now emits ``ntfy_post_ok`` as the paired
    positive for the sticky ``ntfy_post_failed`` / ``ntfy_post_rejected``
    failure lines. First-occurrence-only dedup at the feedback layer
    keeps events.jsonl growth bounded in the algedonic block."""
    monkeypatch.setenv("NTFY_TOPIC", "mimir-alarms-xyz")
    session = _MockSession(post_resp=_MockResponse(200, "ok"))
    _install_session(monkeypatch, session)

    await ntfy.post_algedonic_alarm(
        category="discord-down",
        title="Discord outbound failing",
        body="3 consecutive sends failed",
        dedupe_key="discord-down:outbound",
        priority=4,
        tags=["warning"],
    )

    assert captured_events == [(
        "ntfy_post_ok",
        {"category": "discord-down", "dedupe_key": "discord-down:outbound"},
    )]
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "https://ntfy.sh/mimir-alarms-xyz"
    assert kwargs["data"] == "3 consecutive sends failed"
    assert kwargs["headers"]["Title"] == "Discord outbound failing"
    assert kwargs["headers"]["Priority"] == "4"
    assert kwargs["headers"]["Tags"] == "warning"
    # Dedup table updated.
    assert "discord-down:outbound" in ntfy._LAST_POST


@pytest.mark.asyncio
async def test_dedup_within_window_skips(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(post_resp=_MockResponse(200, ""))
    _install_session(monkeypatch, session)

    await ntfy.post_algedonic_alarm(
        category="x", title="t", body="b", dedupe_key="same-key",
    )
    # Second call should be a silent no-op.
    await ntfy.post_algedonic_alarm(
        category="x", title="t", body="b", dedupe_key="same-key",
    )

    # Only the first call hit the wire.
    assert len(session.calls) == 1
    # The first call emitted ``ntfy_post_ok`` (chainlink #65 paired
    # positive); the dedup-skipped second call emits nothing.
    assert [evt[0] for evt in captured_events] == ["ntfy_post_ok"]


@pytest.mark.asyncio
async def test_dedup_window_expiry_re_posts(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(post_resp=_MockResponse(200, ""))
    _install_session(monkeypatch, session)

    # First fire stamps the dedup table at "now".
    await ntfy.post_algedonic_alarm(
        category="x", title="t", body="b",
        dedupe_key="rotating-key",
        dedup_window_seconds=60,
    )
    assert len(session.calls) == 1

    # Advance "now" past the 60s window by patching _now_utc.
    real_now = ntfy._LAST_POST["rotating-key"]
    later = real_now + timedelta(seconds=120)
    monkeypatch.setattr(ntfy, "_now_utc", lambda: later)

    await ntfy.post_algedonic_alarm(
        category="x", title="t", body="b",
        dedupe_key="rotating-key",
        dedup_window_seconds=60,
    )

    # Both calls hit the wire.
    assert len(session.calls) == 2
    # Each successful post emits ``ntfy_post_ok`` (chainlink #65).
    assert [evt[0] for evt in captured_events] == ["ntfy_post_ok", "ntfy_post_ok"]


@pytest.mark.asyncio
async def test_network_failure_emits_post_failed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(
        post_exc=aiohttp.ClientConnectionError("boom"),
    )
    _install_session(monkeypatch, session)

    # Must not raise.
    await ntfy.post_algedonic_alarm(
        category="cost-runaway", title="t", body="b",
        dedupe_key="cost-runaway:hourly",
    )

    assert len(captured_events) == 1
    event_type, payload = captured_events[0]
    assert event_type == "ntfy_post_failed"
    assert payload["category"] == "cost-runaway"
    assert payload["dedupe_key"] == "cost-runaway:hourly"
    assert "boom" in payload["error"]
    assert "ClientConnectionError" in payload["error"]
    # Dedup table NOT stamped on failure — operator should be re-tried
    # on the next cycle.
    assert "cost-runaway:hourly" not in ntfy._LAST_POST


@pytest.mark.asyncio
async def test_5xx_emits_post_failed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(post_resp=_MockResponse(503, "service unavailable"))
    _install_session(monkeypatch, session)

    await ntfy.post_algedonic_alarm(
        category="oauth", title="t", body="b",
        dedupe_key="oauth:logged-out",
    )

    assert len(captured_events) == 1
    event_type, payload = captured_events[0]
    assert event_type == "ntfy_post_failed"
    assert payload["status"] == 503
    assert payload["error"] == "http_5xx"
    assert "service unavailable" in payload["body_excerpt"]
    # Not stamped — re-fire eligible.
    assert "oauth:logged-out" not in ntfy._LAST_POST


@pytest.mark.asyncio
async def test_4xx_emits_post_rejected(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(post_resp=_MockResponse(400, "bad request"))
    _install_session(monkeypatch, session)

    await ntfy.post_algedonic_alarm(
        category="x", title="t", body="b", dedupe_key="x:y",
    )

    assert len(captured_events) == 1
    event_type, payload = captured_events[0]
    assert event_type == "ntfy_post_rejected"
    assert payload["status"] == 400
    assert "bad request" in payload["body_excerpt"]
    assert payload["category"] == "x"
    assert payload["dedupe_key"] == "x:y"


@pytest.mark.asyncio
async def test_headers_serialization(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    session = _MockSession(post_resp=_MockResponse(200, ""))
    _install_session(monkeypatch, session)

    await ntfy.post_algedonic_alarm(
        category="x",
        title="Cost runaway: $$$",
        body="b",
        dedupe_key="cost:5h",
        priority=5,
        tags=["warning", "rotating_light"],
    )

    assert len(session.calls) == 1
    _, kwargs = session.calls[0]
    headers = kwargs["headers"]
    assert headers["Title"] == "Cost runaway: $$$"
    assert headers["Priority"] == "5"
    assert isinstance(headers["Priority"], str)
    assert headers["Tags"] == "warning,rotating_light"


# ─── cost-runaway dead-man alarm tests (chainlink #66) ────────────────


@pytest.mark.asyncio
async def test_cost_runaway_alarm_fires_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """fire_cost_runaway_alarm_if_warranted calls post_algedonic_alarm when
    the rate exceeds the threshold and NTFY_TOPIC is unset (emits skip event,
    not a real push — but the alarm path is exercised)."""
    # With no NTFY_TOPIC the helper emits ntfy_skip_no_topic, which is
    # enough to prove the alarm function was invoked.
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "cost_rate_alert",
        {"rate_now_usd_per_hour": 75.0},
        threshold_usd_per_hour=50.0,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds, (
        "Expected ntfy_skip_no_topic (alarm invoked but no topic configured); "
        f"got events: {kinds}"
    )
    # Confirm category is correct in the skip event.
    skip_evt = next(e for e in captured_events if e[0] == "ntfy_skip_no_topic")
    assert skip_evt[1]["category"] == "cost-runaway"


@pytest.mark.asyncio
async def test_cost_runaway_alarm_fires_for_advisory_kind(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """cost_rate_advisory (quota-billing mode) also triggers the alarm."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "cost_rate_advisory",
        {"rate_now_usd_per_hour": 60.0},
        threshold_usd_per_hour=50.0,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds


@pytest.mark.asyncio
async def test_cost_runaway_alarm_silent_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """No alarm when rate is below the threshold, even with NTFY_TOPIC set."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "cost_rate_alert",
        {"rate_now_usd_per_hour": 25.0},
        threshold_usd_per_hour=50.0,
    )

    # No events at all — short-circuited before calling post_algedonic_alarm.
    assert captured_events == []


@pytest.mark.asyncio
async def test_cost_runaway_alarm_silent_for_unrelated_event(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Non-cost events are ignored, even with a very high rate field."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "git_push_failed",
        {"rate_now_usd_per_hour": 999.0},
        threshold_usd_per_hour=50.0,
    )

    assert captured_events == []


@pytest.mark.asyncio
async def test_cost_runaway_alarm_fires_at_exact_threshold(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Rate exactly at threshold fires the alarm (>= semantics: rate < threshold
    is the short-circuit condition, so rate == threshold passes through)."""
    # No NTFY_TOPIC → alarm path is invoked but returns ntfy_skip_no_topic,
    # proving we reached post_algedonic_alarm.
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "cost_rate_alert",
        {"rate_now_usd_per_hour": 50.0},
        threshold_usd_per_hour=50.0,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds, (
        "Alarm should fire at exactly the threshold (>= semantics); "
        f"got events: {kinds}"
    )


@pytest.mark.asyncio
async def test_cost_runaway_alarm_silent_just_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Rate just below threshold is silent."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")

    await ntfy.fire_cost_runaway_alarm_if_warranted(
        "cost_rate_alert",
        {"rate_now_usd_per_hour": 49.99},
        threshold_usd_per_hour=50.0,
    )

    assert captured_events == []
