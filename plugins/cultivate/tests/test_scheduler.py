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
async def test_server_registers_provider_jobs_with_opt_in_reaper_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sched = Scheduler(tmp_path / "scheduler.yaml", _enqueue)
    events: list[tuple[str, dict]] = []

    async def record(kind: str, **fields: object) -> None:
        events.append((kind, fields))

    monkeypatch.delenv("MIMIR_WORKLINK_REAPER_CRON", raising=False)
    monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", raising=False)
    await server.configure_worklink(sched, tmp_path, "operator", object(), record)
    assert sched._scheduler.get_job("worklink-factory-park-alert") is not None
    assert sched._scheduler.get_job("worklink-reaper") is None
    assert "worklink-reaper" in sched.registered_callables()
    assert sched._scheduler.get_job("pr-checkout-lease-reaper") is None
    assert events == []

    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_WORKLINK_REAPER_CRON", "0 * * * *")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    await server.configure_worklink(sched, tmp_path, "operator", object(), record)
    assert sched._scheduler.get_job("worklink-reaper") is not None
    assert sched._scheduler.get_job("pr-checkout-lease-reaper") is not None
    assert events == []


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
