"""Cultivate maintenance registration and selected poller recovery hooks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugins.manager import load_plugins
from mimir.poller_hooks import PollerHooks, resolve_poller_hooks
from mimir.pollers import _validate_poller_identity
from mimir.scheduler import Scheduler
from mimir_cultivate import scheduler as jobs
from mimir_cultivate import server


async def _enqueue(_event: object) -> bool:
    return True


def test_park_alert_is_provider_owned_and_fires_every_five_minutes(tmp_path: Path) -> None:
    sched = Scheduler(tmp_path / "scheduler.yaml", _enqueue)
    assert not hasattr(sched, "add_worklink_factory_park_alert_job")
    assert jobs.add_factory_park_alert_job(sched, tmp_path, "operator", object())
    job = sched._scheduler.get_job("worklink-factory-park-alert")
    assert job is not None
    assert "minute='*/5'" in str(job.trigger)


@pytest.mark.asyncio
async def test_fresh_worklink_home_registers_reaper_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sched = Scheduler(tmp_path / "scheduler.yaml", _enqueue)
    events: list[tuple[str, dict]] = []

    async def record(kind: str, **fields: object) -> None:
        events.append((kind, fields))

    # Cultivate setup provisions worklink.yaml, but does not write the cron env.
    (tmp_path / "worklink.yaml").write_text("repository: org/repo\n")
    monkeypatch.delenv("MIMIR_WORKLINK_REAPER_CRON", raising=False)
    monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", raising=False)
    await server.configure_worklink(sched, tmp_path, "operator", object(), record)
    assert sched._scheduler.get_job("worklink-factory-park-alert") is not None
    reaper = sched._scheduler.get_job("worklink-reaper")
    assert reaper is not None
    assert "minute='*/30'" in str(reaper.trigger)
    assert "worklink-reaper" in sched.registered_callables()
    assert sched._scheduler.get_job("pr-checkout-lease-reaper") is None
    assert events == []

    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_WORKLINK_REAPER_CRON", "0 * * * *")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    await server.configure_worklink(sched, tmp_path, "operator", object(), record)
    reaper = sched._scheduler.get_job("worklink-reaper")
    assert reaper is not None
    assert "minute='0'" in str(reaper.trigger)
    assert sched._scheduler.get_job("pr-checkout-lease-reaper") is not None
    assert events == []


@pytest.mark.asyncio
async def test_explicit_empty_reaper_cron_disables_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "worklink.yaml").write_text("repository: org/repo\n")
    monkeypatch.setenv("MIMIR_WORKLINK_REAPER_CRON", "")
    sched = Scheduler(tmp_path / "scheduler.yaml", _enqueue)

    async def record(kind: str, **fields: object) -> None:
        pytest.fail(f"unexpected event: {kind} {fields}")

    await server.configure_worklink(sched, tmp_path, None, object(), record)
    assert sched._scheduler.get_job("worklink-reaper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", [None, "*/7 * * * *"])
async def test_non_worklink_home_has_no_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cron: str | None,
) -> None:
    if cron is None:
        monkeypatch.delenv("MIMIR_WORKLINK_REAPER_CRON", raising=False)
    else:
        monkeypatch.setenv("MIMIR_WORKLINK_REAPER_CRON", cron)
    sched = Scheduler(tmp_path / "scheduler.yaml", _enqueue)

    async def record(kind: str, **fields: object) -> None:
        pytest.fail(f"unexpected event: {kind} {fields}")

    await server.configure_worklink(sched, tmp_path, None, object(), record)
    assert sched._scheduler.get_job("worklink-reaper") is None
    assert "worklink-reaper" not in sched.registered_callables()


def test_selected_worklink_hooks_are_provider_hooks(tmp_path: Path) -> None:
    policy = load_plugins(enable="cultivate").policy
    assert policy.poller_hooks["worklink"][0] == "cultivate"
    manifest = tmp_path / "chainlink-orchestrator" / "pollers.json"
    hooks = resolve_poller_hooks(
        "worklink", manifest, policy=policy, plugin_owner="cultivate",
    )
    assert isinstance(hooks, PollerHooks)
    assert hooks.recovery_relevance is not None
    assert hooks.receipt_liveness is not None
    poller = SimpleNamespace(resolved_persist_dir=lambda: tmp_path / "state")
    assert callable(hooks.recovery_relevance(poller))
    _validate_poller_identity("worklink-ready-queue", manifest, plugin_owner="cultivate")

    with pytest.raises(ValueError, match="reserved"):
        _validate_poller_identity("worklink-ready-queue", manifest, plugin_owner="foreign")
    with pytest.raises(ValueError, match="foreign"):
        resolve_poller_hooks("worklink", manifest, policy=policy, plugin_owner="foreign")
    with pytest.raises(ValueError, match="foreign"):
        resolve_poller_hooks(
            "worklink", tmp_path / "other-skill" / "pollers.json",
            policy=policy, plugin_owner="cultivate",
        )


def test_core_only_policy_exposes_no_provider_poller_hook() -> None:
    assert "worklink" not in load_plugins(enable="").policy.poller_hooks
