"""Cultivate-owned maintenance jobs for Worklink and PR checkout leases."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from mimir._context import active_pr_checkout_lease_paths
from mimir.background_io import run_in_pool
from mimir.event_logger import log_event
from mimir.poller_hooks import PollerHooks

_REAP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worklink-reap")


def worklink_poller_hooks() -> PollerHooks:
    """Resolve the ready queue's recovery and durable receipt hooks."""
    from .worklink.poller_hooks import recovery_relevance_check, report_live_delivery_receipts

    def recovery(poller: Any) -> Any:
        return recovery_relevance_check(poller.resolved_persist_dir())

    return PollerHooks(
        recovery_relevance=recovery,
        receipt_liveness=report_live_delivery_receipts,
    )


def add_factory_park_alert_job(scheduler: Any, home: Path, channel: str | None, channels: Any) -> bool:
    from .worklink.park_alerts import alert_parked_factory_runs

    async def fire() -> None:
        await alert_parked_factory_runs(home, channel, channels)

    return scheduler.register_callable(
        name="worklink-factory-park-alert", fn=fire,
        default_cron="*/5 * * * *", job_id="worklink-factory-park-alert",
        max_instances=1, coalesce=True,
    )


def add_reaper_job(
    scheduler: Any, home: Path, cron_expr: str = "", *, job_id: str = "worklink-reaper",
) -> bool:
    from .worklink.autonomy import (
        close_merged_chainlinks_for_home,
        prune_stale_attempt_checkouts_for_home,
        reap_stale_claims_for_home,
        report_foreign_owned_git_objects_for_home,
    )
    from .worklink.identities import get_identities

    async def fire() -> None:
        def reap() -> tuple[Any, list[Path], list[Path], list[Any]]:
            return (
                reap_stale_claims_for_home(home),
                report_foreign_owned_git_objects_for_home(
                    home, expected_uid=get_identities().mimir_uid,
                ) if os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO") else [],
                prune_stale_attempt_checkouts_for_home(home),
                close_merged_chainlinks_for_home(home),
            )

        result, _, pruned_paths, closed_chainlinks = await run_in_pool(_REAP_POOL, reap)
        await log_event(
            "worklink_claims_reaped", count=len(result.reaped),
            issue_ids=[record.issue_id for record in result.reaped],
            examined=result.examined, skipped=result.skipped,
            skipped_issue_ids=result.skipped_issue_ids,
        )
        if pruned_paths:
            await log_event(
                "worklink_attempt_checkouts_pruned", count=len(pruned_paths),
                paths=[str(path) for path in pruned_paths],
            )
        if closed_chainlinks:
            await log_event(
                "worklink_merged_chainlinks_closed", count=len(closed_chainlinks),
                issue_ids=[record.issue_id for record in closed_chainlinks],
            )

    return scheduler.register_callable(
        name=job_id, fn=fire, default_cron=cron_expr, job_id=job_id,
        misfire_grace_time=3600, max_instances=1, coalesce=True,
    )


def add_pr_checkout_lease_reaper_job(
    scheduler: Any, lease_root: Path, cron_expr: str = "*/15 * * * *",
    *, job_id: str = "pr-checkout-lease-reaper",
) -> bool:
    from .repository import reclaim_expired_pr_checkout_leases

    async def fire() -> None:
        await asyncio.to_thread(
            reclaim_expired_pr_checkout_leases, lease_root,
            active_paths=active_pr_checkout_lease_paths,
        )

    return scheduler.register_callable(
        name=job_id, fn=fire, default_cron=cron_expr, job_id=job_id,
        misfire_grace_time=900, max_instances=1, coalesce=True,
    )
