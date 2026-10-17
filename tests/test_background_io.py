"""Background jobs must not depend on spare default-executor capacity."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.timing import HANG_GUARD_SECONDS


@pytest.mark.parametrize("operation", ["scratch", "worklink", "attestation", "event"])
def test_jobs_complete_with_saturated_default_pool(tmp_path, monkeypatch, operation):
    from mimir import event_logger, pollers, scheduler, scratch_janitor
    from mimir.models import AgentEvent

    if operation == "worklink" and os.environ.get("MIMIR_PLUGINS") not in {"cultivate", "all"}:
        pytest.skip("Worklink background work requires selected Cultivate")

    marker = contextvars.ContextVar("background-test-marker", default=None)
    logger = event_logger.EventLogger(tmp_path / "events.jsonl", "pool-test")
    monkeypatch.setattr(scheduler, "log_event", logger.log)
    sched = scheduler.Scheduler(tmp_path / "scheduler.yaml", AsyncMock())
    observed = []

    def sweep(*args, **kwargs):
        observed.append(marker.get())
        return SimpleNamespace(removed=[], errors=[])

    monkeypatch.setattr(scratch_janitor, "sweep_scratch_roots", sweep)
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    monkeypatch.setenv("MIMIR_SCRATCH_TTL_DAYS", "1")
    monkeypatch.setattr(pollers, "_github_api_attestation", lambda *a: (200, {"state": "open"}))
    sched.add_scratch_janitor_job(tmp_path)
    if operation == "worklink":
        from mimir.plugins.manager import load_plugins
        from mimir_cultivate.worklink import autonomy as provider_autonomy

        called: list[str] = []

        def provider_reap(*_args):
            called.append("provider")
            return SimpleNamespace(reaped=[], examined=0, skipped=0, skipped_issue_ids=[])

        assert not hasattr(sched, "add_worklink_reaper_job")
        monkeypatch.setattr(provider_autonomy, "reap_stale_claims_for_home", provider_reap)
        monkeypatch.setattr(provider_autonomy, "prune_stale_attempt_checkouts_for_home", lambda *a: [])
        monkeypatch.setattr(provider_autonomy, "close_merged_chainlinks_for_home", lambda *a: [])
        monkeypatch.setenv("MIMIR_WORKLINK_REAPER_CRON", "* * * * *")
        monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", raising=False)
        hook = load_plugins(enable="cultivate").policy.server_hooks["cultivate", "configure_jobs"]

    async def exercise():
        if operation == "worklink":
            async def unexpected_event(*args, **kwargs):
                pytest.fail(f"unexpected scheduler error: {args}, {kwargs}")

            await hook(sched, tmp_path, "operator", object(), unexpected_event)
        loop = asyncio.get_running_loop()
        # This loop and its executor belong solely to this test.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = asyncio.Event()
        release = threading.Event()

        def occupy():
            loop.call_soon_threadsafe(entered.set)
            release.wait(10)

        blocker = loop.run_in_executor(None, occupy)
        token = marker.set("caller-context")
        try:
            await asyncio.wait_for(entered.wait(), HANG_GUARD_SECONDS)
            if operation == "scratch":
                await asyncio.wait_for(sched._callables["scratch-janitor"].fn(), HANG_GUARD_SECONDS)
                assert observed == ["caller-context"]
            elif operation == "worklink":
                await asyncio.wait_for(sched._callables["worklink-reaper"].fn(), HANG_GUARD_SECONDS)
                assert called == ["provider"]
                assert not hasattr(sched, "add_worklink_reaper_job")
            elif operation == "attestation":
                check = pollers._github_recovery_relevance_check("token")
                event = AgentEvent(trigger="poller", channel_id="test", content="PR", extra={
                    "items": [{"repo": "owner/repo", "number": 1, "subject_type": "pull_request"}],
                })
                assert await asyncio.wait_for(check(event), HANG_GUARD_SECONDS) is True
            else:
                await asyncio.wait_for(logger.log("isolated"), HANG_GUARD_SECONDS)
                assert json.loads((tmp_path / "events.jsonl").read_text())["type"] == "isolated"
            assert not blocker.done(), "the default worker must still be saturated"
        finally:
            marker.reset(token)
            release.set()
            await blocker

    asyncio.run(exercise())
