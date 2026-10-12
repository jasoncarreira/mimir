from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.worklink.backends.feature_factory import FactoryStatus
from mimir.worklink.factory_state import FactoryRunRecord, save_factory_record
from mimir.worklink import park_alerts


def _record(home: Path, *, phase: str = "parked", attempt: int = 1,
            reason: str = "SECRET_MARKER @everyone") -> FactoryRunRecord:
    run_id = "chainlink-1947"
    sandbox = home / ".factory-sandboxes" / run_id
    return FactoryRunRecord(
        run_id=run_id, issue_id=1947, attempt=attempt, repository="owner/repo",
        base_ref="main", branch="epic/1947", launcher="/usr/bin/node",
        sandbox=str(sandbox), session=None, handle=None,
        status=FactoryStatus(run_id=run_id, valid=True, sandbox_path=str(sandbox),
                             status="needs-human", terminal_result={"reason": reason}),
        observed_at=None, controller_phase=phase,
    )


class Channels:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, channel: str, body: str, *, final: bool) -> SimpleNamespace:
        assert channel == "operator" and final
        self.messages.append(body)
        return SimpleNamespace(sent=True)


@pytest.mark.asyncio
async def test_park_alerts_once_per_identity_across_restart_and_both_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = Channels()
    pushes: list[dict] = []
    events: list[tuple[str, dict]] = []

    async def push(**kwargs):
        pushes.append(kwargs)

    async def event(kind, **kwargs):
        events.append((kind, kwargs))

    monkeypatch.setenv("NTFY_TOPIC", "operator")
    monkeypatch.setattr(park_alerts, "post_algedonic_alarm", push)
    monkeypatch.setattr(park_alerts, "log_event", event)
    monkeypatch.setattr(park_alerts, "_issue_title", lambda home, issue: "Plugin core")
    first = _record(tmp_path)
    save_factory_record(tmp_path, first)
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    # A fresh invocation reloads the persisted state rather than a process cache.
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    assert len(channels.messages) == len(pushes) == 1
    body = channels.messages[0]
    assert pushes[0]["body"] == body
    assert pushes[0]["title"] == "Factory epic #1947: Plugin core"
    assert pushes[0]["category"] == "worklink-factory-parked"
    assert pushes[0]["priority"] == 4
    assert pushes[0]["dedupe_key"] == park_alerts.park_dedupe_key(first)
    assert all(piece in body for piece in ("#1947", "Plugin core", "chainlink-1947",
                                           "attempt: 1", "needs-human", "WORKLINK_BLOCKED",
                                           "mimir worklink status 1947"))
    assert "SECRET_MARKER" not in body and "@everyone" not in body
    assert "SECRET_MARKER" not in pushes[0]["title"] and "@everyone" not in pushes[0]["title"]
    assert [(kind, data["sinks"]) for kind, data in events if kind.endswith("_alerted")] == [
        ("worklink_factory_park_alerted", ["channel", "ntfy"])
    ]
    state = json.loads(park_alerts._state_path(tmp_path).read_text())
    assert state == {park_alerts.park_dedupe_key(first): ["channel", "ntfy"]}

    second = replace(first, attempt=2)
    save_factory_record(tmp_path, second)
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    third = replace(second, status=replace(second.status, terminal_result={"reason": "different"}))
    save_factory_record(tmp_path, third)
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    assert len(channels.messages) == len(pushes) == 3
    assert len({push["dedupe_key"] for push in pushes}) == 3


@pytest.mark.asyncio
async def test_non_parked_and_archived_records_never_alert(tmp_path, monkeypatch) -> None:
    channels = Channels()
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(park_alerts, "_issue_title", lambda *_: pytest.fail("title looked up"))
    for phase in ("running", "completed", "terminal"):
        record = _record(tmp_path, phase=phase)
        save_factory_record(tmp_path, record)
        await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    save_factory_record(tmp_path, replace(_record(tmp_path), status=None))
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    parked = _record(tmp_path)
    assert parked.status is not None
    save_factory_record(tmp_path, replace(parked, status=replace(parked.status, status="running")))
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    (tmp_path / "state/worklink/factory-runs/chainlink-1947.json").unlink()
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    assert channels.messages == []


@pytest.mark.asyncio
async def test_unrouted_is_durable_and_bad_record_is_skipped(tmp_path, monkeypatch) -> None:
    events: list[str] = []

    async def event(kind, **kwargs):
        events.append(kind)

    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(park_alerts, "log_event", event)
    save_factory_record(tmp_path, _record(tmp_path))
    (tmp_path / "state/worklink/factory-runs/chainlink-1999.json").write_text("{bad")
    await park_alerts.alert_parked_factory_runs(tmp_path, "", Channels())
    await park_alerts.alert_parked_factory_runs(tmp_path, "", Channels())
    assert events.count("worklink_factory_park_unrouted") == 1
    assert events.count("worklink_factory_park_record_skipped") == 2
    assert json.loads(park_alerts._state_path(tmp_path).read_text()) == {
        park_alerts.park_dedupe_key(_record(tmp_path)): ["unrouted"]
    }


@pytest.mark.asyncio
async def test_failed_channel_does_not_duplicate_ntfy(tmp_path, monkeypatch) -> None:
    pushes = []
    events = []
    monkeypatch.setenv("NTFY_TOPIC", "operator")
    monkeypatch.setattr(park_alerts, "_issue_title", lambda *_: "Plugin core")

    async def push(**kwargs):
        pushes.append(kwargs)

    async def event(kind, **kwargs):
        events.append(kind)

    monkeypatch.setattr(park_alerts, "post_algedonic_alarm", push)
    monkeypatch.setattr(park_alerts, "log_event", event)
    save_factory_record(tmp_path, _record(tmp_path))

    class FailingChannels:
        async def send(self, *args, **kwargs):
            raise RuntimeError("offline")

    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", FailingChannels())
    channels = Channels()
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", channels)
    assert len(pushes) == len(channels.messages) == 1
    assert events.count("worklink_factory_park_alerted") == 1


def test_title_lookup_requires_matching_issue_and_neutralizes_mentions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(park_alerts.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"id": 1948, "title": "@everyone Unsafe"})))
    with pytest.raises(ValueError, match="identity"):
        park_alerts._issue_title(tmp_path, 1947)
    monkeypatch.setattr(park_alerts.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"id": 1947, "title": "@everyone Unsafe"})))
    assert "@everyone" not in park_alerts._issue_title(tmp_path, 1947)


@pytest.mark.asyncio
async def test_corrupt_dedupe_state_fails_closed(tmp_path, monkeypatch) -> None:
    events = []

    async def event(kind, **kwargs):
        events.append(kind)

    monkeypatch.setattr(park_alerts, "log_event", event)
    monkeypatch.setattr(park_alerts, "_issue_title", lambda *_: pytest.fail("title looked up"))
    save_factory_record(tmp_path, _record(tmp_path))
    park_alerts._state_path(tmp_path).write_text("{bad")
    await park_alerts.alert_parked_factory_runs(tmp_path, "operator", Channels())
    assert events == ["worklink_factory_park_state_unavailable"]


@pytest.mark.asyncio
async def test_registered_job_scans_without_enqueuing_turns(tmp_path, monkeypatch) -> None:
    from mimir.scheduler import Scheduler

    calls = []

    async def enqueue(event):
        pytest.fail("model turn enqueued")

    async def scan(home, channel, channels):
        calls.append((home, channel, channels))

    monkeypatch.setattr(park_alerts, "alert_parked_factory_runs", scan)
    scheduler = Scheduler(tmp_path / "scheduler.yaml", enqueue, home=tmp_path)
    channels = Channels()
    scheduler.add_worklink_factory_park_alert_job(tmp_path, "operator", channels)
    job = scheduler._callables["worklink-factory-park-alert"]
    assert job.default_cron == "*/5 * * * *"
    await job.fn()
    assert calls == [(tmp_path, "operator", channels)]
