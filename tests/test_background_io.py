"""Background jobs must not depend on spare default-executor capacity."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("operation", ["scratch", "worklink", "attestation", "event"])
def test_jobs_complete_with_saturated_default_pool(tmp_path, monkeypatch, operation):
    from mimir import event_logger, pollers, scheduler, scratch_janitor
    from mimir.models import AgentEvent
    from mimir.worklink import autonomy

    marker = contextvars.ContextVar("background-test-marker", default=None)
    logger = event_logger.EventLogger(tmp_path / "events.jsonl", "pool-test")
    monkeypatch.setattr(scheduler, "log_event", logger.log)
    sched = scheduler.Scheduler(tmp_path / "scheduler.yaml", AsyncMock())
    observed = []

    def sweep(*args, **kwargs):
        observed.append(marker.get())
        return SimpleNamespace(removed=[], errors=[])

    monkeypatch.setattr(scratch_janitor, "sweep_scratch_roots", sweep)
    monkeypatch.setattr(autonomy, "reap_stale_claims_for_home", lambda *a: SimpleNamespace(
        reaped=[], examined=0, skipped=0, skipped_issue_ids=[],
    ))
    monkeypatch.setattr(autonomy, "prune_stale_attempt_checkouts_for_home", lambda *a: [])
    monkeypatch.setattr(autonomy, "close_merged_chainlinks_for_home", lambda *a: [])
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    monkeypatch.setenv("MIMIR_SCRATCH_TTL_DAYS", "1")
    monkeypatch.setattr(pollers, "_github_api_attestation", lambda *a: (200, {"state": "open"}))
    sched.add_scratch_janitor_job(tmp_path)
    sched.add_worklink_reaper_job(tmp_path, "* * * * *")

    async def exercise():
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
            await asyncio.wait_for(entered.wait(), 2)
            if operation == "scratch":
                await asyncio.wait_for(sched._callables["scratch-janitor"].fn(), 2)
                assert observed == ["caller-context"]
            elif operation == "worklink":
                await asyncio.wait_for(sched._callables["worklink-reaper"].fn(), 2)
            elif operation == "attestation":
                check = pollers._github_recovery_relevance_check("token")
                event = AgentEvent(trigger="poller", channel_id="test", content="PR", extra={
                    "items": [{"repo": "owner/repo", "number": 1, "subject_type": "pull_request"}],
                })
                assert await asyncio.wait_for(check(event), 2) is True
            else:
                await asyncio.wait_for(logger.log("isolated"), 2)
                assert json.loads((tmp_path / "events.jsonl").read_text())["type"] == "isolated"
            assert not blocker.done(), "the default worker must still be saturated"
        finally:
            marker.reset(token)
            release.set()
            await blocker

    asyncio.run(exercise())
