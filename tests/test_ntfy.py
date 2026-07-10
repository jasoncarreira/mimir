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

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
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


# ─── scheduler-wedge dead-man alarm tests (chainlink #66) ─────────────


def _write_events(path, events: list[dict]) -> None:
    """Write a list of event dicts to ``path`` as JSONL."""
    import json as _json
    path.write_text("\n".join(_json.dumps(e) for e in events) + "\n")


def _heartbeat_event(ts: str) -> dict:
    return {
        "timestamp": ts,
        "type": "scheduled_tick",
        "channel_id": "scheduler:heartbeat",
    }


def _write_scheduler_yaml(tmp_path, cron: str = "*/45 * * * *"):
    """Write a minimal scheduler.yaml with the given heartbeat cron to ``tmp_path``.

    Default cron ``*/45 * * * *`` (45 min) × safety_factor 2.0 = 90 min
    threshold — matching the intent of the original hardcoded constant.
    """
    import yaml as _yaml
    content = [{"name": "heartbeat", "cron": cron, "prompt_file": "heartbeat.md"}]
    sched_path = tmp_path / "scheduler.yaml"
    sched_path.write_text(_yaml.safe_dump(content), encoding="utf-8")
    return sched_path


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_offloads_log_assessment_from_event_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """chainlink #838: scheduler-health log scans must not run on the loop.

    The wedge check reads scheduler.yaml and tail-scans events.jsonl.  Those
    synchronous file operations belong behind ``asyncio.to_thread`` so the
    health check itself does not create a ``scheduler_loop_lag`` sample.
    """
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=130)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    loop_thread = threading.get_ident()
    observed_threads: list[int] = []
    real_assess = ntfy._assess_scheduler_wedge

    def wrapped_assess(*args: Any, **kwargs: Any):
        observed_threads.append(threading.get_ident())
        return real_assess(*args, **kwargs)

    monkeypatch.setattr(ntfy, "_assess_scheduler_wedge", wrapped_assess)

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    assert observed_threads, "wedge assessment did not run"
    assert all(thread_id != loop_thread for thread_id in observed_threads), (
        "scheduler wedge assessment ran on the event-loop thread"
    )
    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds


@pytest.mark.asyncio
async def test_size_scaled_events_log_scan_does_not_lag_event_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """chainlink #843: a large events.jsonl scan must not monopolize the loop."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=48)).isoformat()
    rows = [
        {
            "timestamp": old_ts,
            "type": "other_event",
            "channel_id": "other",
            "payload": "x" * 200,
        }
        for _ in range(20_000)
    ]
    events_file.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    try:
        await asyncio.wait_for(
            ntfy.fire_scheduler_wedge_alarm_if_warranted(
                events_file,
                scheduler_yaml_path=sched_yaml,
                now=now,
            ),
            timeout=5,
        )
    finally:
        done.set()
        await ticker_task

    assert ticks > 0, "event loop did not advance while events.jsonl was scanned"
    assert captured_events == []


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_fires_when_stale(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Alarm fires when heartbeat hasn't appeared for >threshold minutes."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    # Last tick was 100 minutes ago; threshold is 90.
    from datetime import timedelta

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=100)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds, (
        "Expected ntfy_skip_no_topic (alarm invoked but no topic set); "
        f"got events: {kinds}"
    )
    skip_evt = next(e for e in captured_events if e[0] == "ntfy_skip_no_topic")
    assert skip_evt[1]["category"] == "scheduler-wedge"


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_silent_when_recent(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """No alarm when the last heartbeat is well within the threshold."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    from datetime import timedelta

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=30)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    assert captured_events == []


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_silent_no_events_file(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """No alarm (no crash) when events.jsonl does not exist yet."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    missing = tmp_path / "nonexistent.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(missing, scheduler_yaml_path=sched_yaml)

    assert captured_events == []


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_silent_no_heartbeat_in_log(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """No alarm when the log exists but contains no heartbeat tick events."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)
    # Only non-heartbeat events.
    _write_events(events_file, [
        {"timestamp": "2026-05-27T03:00:00+00:00", "type": "oauth_ok", "channel_id": "other"},
    ])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
    )

    assert captured_events == []


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_picks_last_heartbeat(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Uses the MOST RECENT heartbeat timestamp (not the oldest one)."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    from datetime import timedelta

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    old_tick_ts = (now - timedelta(minutes=200)).isoformat()
    recent_tick_ts = (now - timedelta(minutes=20)).isoformat()

    # Old tick first, then recent one — scanner should use the recent one.
    _write_events(events_file, [
        _heartbeat_event(old_tick_ts),
        _heartbeat_event(recent_tick_ts),
    ])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    # Recent tick is within threshold → no alarm.
    assert captured_events == []


@pytest.mark.asyncio
async def test_scheduler_wedge_alarm_exact_threshold_fires(
    tmp_path: "pytest.fixture",
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Elapsed == threshold does NOT fire (strictly-less-than guard)."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    from datetime import timedelta

    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    # Exactly at threshold — elapsed == 90 min.
    last_tick_ts = (now - timedelta(minutes=90)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    # elapsed (90.0) is NOT < threshold (90) → alarm fires.
    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds, (
        "Alarm should fire at exactly the threshold (>= semantics); "
        f"got events: {kinds}"
    )


@pytest.mark.asyncio
async def test_threshold_derives_from_30min_cron(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """*/30 cron × safety_factor 2.0 → 60 min threshold; 70 min stale fires."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path, cron="*/30 * * * *")

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=70)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds, (
        f"Expected alarm: 70 min > threshold 60 min (*/30 × 2.0); got: {kinds}"
    )


@pytest.mark.asyncio
async def test_threshold_derives_from_2h_cron(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """0 */2 cron × safety_factor 2.0 → 240 min threshold; 90 min stale silent."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path, cron="0 */2 * * *")

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=90)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    assert captured_events == [], (
        f"No alarm expected: 90 min < threshold 240 min (0 */2 × 2.0); got: {captured_events}"
    )


@pytest.mark.asyncio
async def test_no_heartbeat_in_scheduler_yaml_no_alarm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """If scheduler.yaml has no heartbeat job, no alarm even with stale events."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"

    import yaml as _yaml
    sched_yaml = tmp_path / "scheduler.yaml"
    sched_yaml.write_text(
        _yaml.safe_dump([{"name": "reflect", "cron": "0 6 * * 0", "prompt_file": "reflect.md"}]),
        encoding="utf-8",
    )

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=300)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    assert captured_events == [], (
        f"No alarm when heartbeat absent from scheduler.yaml; got: {captured_events}"
    )


@pytest.mark.asyncio
async def test_disabled_heartbeat_with_stale_events_no_alarm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Operator removed heartbeat cron: stale last_tick must NOT alarm.

    Regression guard: before this fix, disabling the heartbeat would still
    fire alarms for ~30 days until the old tick rotated out of events.jsonl.
    """
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    events_file = tmp_path / "events.jsonl"

    import yaml as _yaml
    sched_yaml = tmp_path / "scheduler.yaml"
    sched_yaml.write_text(
        _yaml.safe_dump([{"name": "heartbeat", "cron": "", "prompt_file": "heartbeat.md"}]),
        encoding="utf-8",
    )

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(days=5)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    assert captured_events == [], (
        f"No alarm when heartbeat cron is empty (disabled); got: {captured_events}"
    )


# ─── chainlink #221: classify silence as quota-blocked vs genuine wedge ───


def _suppress_event(ts: str, reason: str = "quota_saturated:anthropic:seven_day@1.00") -> dict:
    """One ``scheduled_tick_suppressed`` event on the heartbeat channel."""
    return {
        "timestamp": ts,
        "type": "scheduled_tick_suppressed",
        "channel_id": "scheduler:heartbeat",
        "schedule_name": "heartbeat",
        "reason": reason,
    }


@pytest.mark.asyncio
async def test_wedge_alarm_silent_when_silence_is_suppression(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Today's mimirbot incident: heartbeat last completed 130 min ago,
    threshold is 90 min, AND ``scheduled_tick_suppressed`` events
    landed in the window. The scheduler is alive and intentionally
    not firing; operator action is upstream (fix quota state, switch
    spec, wait it out) — none of those warrant a phone push.

    Skip the ntfy entirely. Log a
    ``scheduler_suppressed_window_observed`` event for ops-dashboard
    surfacing — same observability without waking the operator.
    """
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)  # */45 * * * *, threshold 90 min

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=130)).isoformat()
    suppress_1_ts = (now - timedelta(minutes=85)).isoformat()
    suppress_2_ts = (now - timedelta(minutes=40)).isoformat()

    _write_events(events_file, [
        _heartbeat_event(last_tick_ts),
        _suppress_event(suppress_1_ts),
        _suppress_event(suppress_2_ts),
    ])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    kinds = [e[0] for e in captured_events]
    # No ntfy events of any kind (no skip-no-topic, no post-ok, no post-failed).
    ntfy_events = [k for k in kinds if k.startswith("ntfy_")]
    assert ntfy_events == [], (
        f"Expected NO ntfy events when silence is suppression; got: {ntfy_events}"
    )
    # But the observability event landed for ops-dashboard surfacing.
    observed = [e for e in captured_events
                if e[0] == "scheduler_suppressed_window_observed"]
    assert len(observed) == 1
    payload = observed[0][1]
    assert payload["channel_id"] == "scheduler:heartbeat"
    assert payload["suppress_reason"] == "quota_saturated:anthropic:seven_day@1.00"
    assert payload["elapsed_minutes"] == pytest.approx(130.0, abs=0.1)
    assert payload["threshold_minutes"] == pytest.approx(90.0, abs=0.1)


@pytest.mark.asyncio
async def test_wedge_alarm_classifies_as_wedge_when_no_suppress_events(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """No ``scheduled_tick_suppressed`` events in the window → existing
    ``scheduler-wedge`` alarm fires (behavior preserved). This is the
    genuine wedge case: APScheduler is actually stuck.
    """
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick_ts = (now - timedelta(minutes=130)).isoformat()
    _write_events(events_file, [_heartbeat_event(last_tick_ts)])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    kinds = [e[0] for e in captured_events]
    assert "ntfy_skip_no_topic" in kinds
    skip_evt = next(e for e in captured_events if e[0] == "ntfy_skip_no_topic")
    assert skip_evt[1]["category"] == "scheduler-wedge"
    assert skip_evt[1]["dedupe_key"] == "scheduler-wedge:heartbeat"


@pytest.mark.asyncio
async def test_wedge_alarm_ignores_suppress_events_outside_window(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """A ``scheduled_tick_suppressed`` event from BEFORE the last
    completed heartbeat shouldn't classify the silence as suppressed —
    it belongs to a prior incident. Fire the genuine-wedge alarm.
    """
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    events_file = tmp_path / "events.jsonl"
    sched_yaml = _write_scheduler_yaml(tmp_path)

    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    # Old suppress event (4h before now), then the heartbeat ran fine
    # (2h before now), then silence. The 4h-ago suppress shouldn't
    # poison the classification of the current silence window.
    old_suppress_ts = (now - timedelta(hours=4)).isoformat()
    last_tick_ts = (now - timedelta(hours=2)).isoformat()
    _write_events(events_file, [
        _suppress_event(old_suppress_ts),
        _heartbeat_event(last_tick_ts),
    ])

    await ntfy.fire_scheduler_wedge_alarm_if_warranted(
        events_file,
        scheduler_yaml_path=sched_yaml,
        now=now,
    )

    skip_evt = next(e for e in captured_events if e[0] == "ntfy_skip_no_topic")
    # Old suppress is outside the window → classifies as wedge.
    assert skip_evt[1]["category"] == "scheduler-wedge"


def test_classify_silence_picks_freshest_suppress_reason(
    tmp_path,
) -> None:
    """When multiple suppress events fall in the window, the helper
    returns the most-recent reason — that's the one the operator most
    likely needs to act on.
    """
    events_file = tmp_path / "events.jsonl"
    from datetime import timedelta
    now = datetime(2026, 5, 27, 4, 0, 0, tzinfo=timezone.utc)
    last_tick = now - timedelta(minutes=200)
    earlier_suppress_ts = (now - timedelta(minutes=120)).isoformat()
    later_suppress_ts = (now - timedelta(minutes=30)).isoformat()

    _write_events(events_file, [
        _heartbeat_event(last_tick.isoformat()),
        _suppress_event(earlier_suppress_ts, reason="quota_saturated:anthropic:seven_day@0.92"),
        _suppress_event(later_suppress_ts, reason="quota_saturated:anthropic:seven_day@1.00"),
    ])

    classification, reason = ntfy._classify_silence(
        events_file,
        channel_id="scheduler:heartbeat",
        window_start=last_tick,
        window_end=now,
    )
    assert classification == "suppressed"
    # Freshest reason (the 1.00 one) wins.
    assert reason == "quota_saturated:anthropic:seven_day@1.00"


class TestParseEventTs:
    """chainlink #259: event-ts parsing is Z-normalized + naive-coerced so
    the downstream tz-aware comparison can't raise a TypeError that escapes
    _classify_silence's 'never raises' contract."""

    def test_naive_coerced_to_utc(self):
        from datetime import timezone
        from mimir.ntfy import _parse_event_ts
        ts = _parse_event_ts("2026-05-27T04:00:00")  # no tz
        assert ts is not None and ts.utcoffset() == timezone.utc.utcoffset(None)

    def test_z_suffix_parses(self):
        from mimir.ntfy import _parse_event_ts
        ts = _parse_event_ts("2026-05-27T04:00:00Z")
        assert ts is not None and ts.utcoffset().total_seconds() == 0

    def test_offset_preserved(self):
        from mimir.ntfy import _parse_event_ts
        assert _parse_event_ts("2026-05-27T04:00:00+00:00") is not None

    def test_bad_inputs_return_none(self):
        from mimir.ntfy import _parse_event_ts
        assert _parse_event_ts("garbage") is None
        assert _parse_event_ts("") is None
        assert _parse_event_ts(None) is None
