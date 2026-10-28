"""Head-bound draft PR CI observation for Worklink leaf publication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
import subprocess
import time
from typing import Callable, Mapping

from mimir.redaction import redact_text
from mimir_cultivate.review_checks import approval_blocker

Runner = Callable[..., subprocess.CompletedProcess[str]]
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class CIResult:
    outcome: str  # green, red, infrastructure, timeout, moved, stopped
    reason: str
    checks: tuple[tuple[str, str], ...] = ()
    excerpt: str = ""
    rerun_ids: tuple[int, ...] = ()


def _json(run: Runner, args: list[str]) -> object:
    result = run(args, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("CI forge read failed: " + redact_text((result.stderr or result.stdout)[:500]))
    return json.loads(result.stdout)


def _head(run: Runner, pr_url: str) -> str:
    data = _json(run, ["gh", "pr", "view", pr_url, "--json", "headRefOid,state,isDraft"])
    if not isinstance(data, dict) or data.get("state") != "OPEN" or data.get("isDraft") is not True:
        raise RuntimeError("draft PR is no longer open and draft")
    return str(data.get("headRefOid", ""))


def _snapshot(run: Runner, repo: str, pr_url: str, sha: str) -> CIResult:
    if not _SHA.fullmatch(sha) or not _SLUG.fullmatch(repo):
        raise RuntimeError("invalid CI repository or pushed head")
    if _head(run, pr_url) != sha:
        return CIResult("moved", "PR head moved by another actor")
    pages = _json(run, ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    status_pages = _json(run, ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/statuses?per_page=100"])
    if not isinstance(pages, list) or not pages or not isinstance(status_pages, list) or not status_pages:
        raise RuntimeError("CI checks could not be read completely")
    checks: list[object] = []
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("check_runs"), list) or type(page.get("total_count")) is not int:
            raise RuntimeError("CI check response is malformed")
        checks.extend(page["check_runs"])
        if page["total_count"] != pages[0]["total_count"]:
            raise RuntimeError("CI check count changed while reading")
    if len(checks) != pages[0]["total_count"]:
        raise RuntimeError("CI checks could not be read completely")
    statuses: list[object] = []
    seen: set[str] = set()
    for page in status_pages:
        if not isinstance(page, list):
            raise RuntimeError("CI status response is malformed")
        for row in page:
            if not isinstance(row, Mapping) or not isinstance(row.get("context"), str):
                raise RuntimeError("CI status response is malformed")
            if row["context"] not in seen:
                statuses.append(row)
                seen.add(row["context"])
    if _head(run, pr_url) != sha:
        return CIResult("moved", "PR head moved by another actor")
    blocker = approval_blocker({"check_runs": checks, "total_count": len(checks)}, {"statuses": statuses})
    if blocker and not blocker.startswith("CI checks not green:") and blocker != "no CI checks or commit statuses exist on this head":
        raise RuntimeError(blocker)
    names = tuple(
        (str(row["name"]), str(row.get("conclusion") if row.get("status") == "completed" else row.get("status")))
        for row in checks if isinstance(row, Mapping)
    ) + tuple((str(row["context"]), str(row.get("state"))) for row in statuses if isinstance(row, Mapping))
    pending = any(isinstance(row, Mapping) and row.get("status") != "completed" for row in checks) or any(
        isinstance(row, Mapping) and row.get("state") == "pending" for row in statuses
    )
    if not names or pending:
        return CIResult("pending", blocker or "CI checks pending", names)
    if blocker is None:
        return CIResult("green", "CI green", names)
    failed = [row for row in checks if isinstance(row, Mapping) and row.get("conclusion") not in ("success", "neutral", "skipped")]
    excerpt = redact_text("\n".join(
        f"{row.get('name', '')}: {str(row.get('output', {}).get('summary', ''))[:1000]} "
        f"{str(row.get('output', {}).get('text', ''))[:1000]}"
        for row in failed if isinstance(row.get("output"), Mapping)
    ))[:4000]
    infra = [row for row in failed if row.get("conclusion") == "startup_failure" or re.search(
        r"(?:registry.*429|toomanyrequests)", str(row.get("output", {})), re.I
    )]
    ids = tuple(row["id"] for row in infra if type(row.get("id")) is int and row["id"] > 0)
    return CIResult("infrastructure" if infra else "red", blocker, names, excerpt, ids)


async def wait_for_ci(
    run: Runner, repo: str, pr_url: str, sha: str, *, timeout_s: int,
    stopped: Callable[[], bool], poll_s: float = 10,
) -> CIResult:
    deadline = time.monotonic() + timeout_s
    latest = CIResult("pending", "CI checks have not appeared")
    while True:
        if stopped():
            return CIResult("stopped", "stopped by operator", latest.checks)
        try:
            latest = await asyncio.to_thread(_snapshot, run, repo, pr_url, sha)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
            return CIResult("error", redact_text(str(exc))[:500], latest.checks)
        if latest.outcome != "pending":
            return latest
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return CIResult("timeout", "CI wait timed out: " + latest.reason, latest.checks)
        await asyncio.sleep(min(poll_s, remaining))


async def rerun_infrastructure(run: Runner, repo: str, pr_url: str, sha: str, ids: tuple[int, ...]) -> bool:
    if not ids or not _SLUG.fullmatch(repo) or not _SHA.fullmatch(sha):
        return False
    for run_id in ids:
        if await asyncio.to_thread(_head, run, pr_url) != sha:
            return False
        result = await asyncio.to_thread(run, ["gh", "api", "-X", "POST", f"repos/{repo}/check-runs/{run_id}/rerequest"], timeout=30)
        if result.returncode != 0:
            return False
    return True


async def ready_pr(run: Runner, pr_url: str, sha: str) -> None:
    if await asyncio.to_thread(_head, run, pr_url) != sha:
        raise RuntimeError("PR head moved before ready for review")
    result = await asyncio.to_thread(run, ["gh", "pr", "ready", pr_url], timeout=30)
    if result.returncode != 0:
        raise RuntimeError("could not mark PR ready for review: " + redact_text((result.stderr or result.stdout)[:500]))
    live = await asyncio.to_thread(_json, run, ["gh", "pr", "view", pr_url, "--json", "headRefOid,state,isDraft"])
    if (not isinstance(live, Mapping) or live.get("headRefOid") != sha
            or live.get("state") != "OPEN" or live.get("isDraft") is not False):
        await asyncio.to_thread(run, ["gh", "pr", "ready", "--undo", pr_url], timeout=30)
        raise RuntimeError("PR head moved while marking ready for review")
