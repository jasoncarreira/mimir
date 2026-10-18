"""Trusted hook profiles declared by poller manifests."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Collection

from .background_io import run_in_pool
from .models import AgentEvent

if TYPE_CHECKING:
    from .poller_recovery import RelevanceFn
    from .pollers import PollerConfig
    from .plugins.registry import PolicyRegistry


RecoveryRelevanceFactory = Callable[["PollerConfig"], "RelevanceFn"]
PruneStaleReceipts = Callable[[Collection[str]], None]
ReceiptLivenessHook = Callable[[Path, Path | None, PruneStaleReceipts], None]


@dataclass(frozen=True)
class PollerHooks:
    """Resolved, server-trusted hooks for one poller declaration."""

    recovery_relevance: RecoveryRelevanceFactory | None = None
    receipt_liveness: ReceiptLivenessHook | None = None


@dataclass(frozen=True)
class PollerHookProfile:
    """A manifest-selectable hook profile and its reserved identity binding."""

    skill: str
    reserved_name: str
    hooks: PollerHooks


def _github_recovery(poller: "PollerConfig") -> "RelevanceFn":
    token = poller.env.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    return github_recovery_relevance_check(token)


def _worklink_recovery(poller: "PollerConfig") -> "RelevanceFn":
    return worklink_recovery_relevance_check(poller.resolved_persist_dir())


def github_recovery_relevance_check(token: str) -> "RelevanceFn":
    """Compatibility entry point for the GitHub skill's recovery hook."""
    from . import pollers

    cache: dict[tuple[str, int], bool | None] = {}

    async def check(event: AgentEvent) -> bool | None:
        items = event.extra.get("items") if isinstance(event.extra, dict) else None
        if not isinstance(items, list) or not items:
            return None
        saw_pr = False
        saw_unknown = False
        for item in items:
            if not isinstance(item, dict):
                saw_unknown = True
                continue
            event_type = item.get("event_type")
            url = item.get("url")
            is_pr = (
                item.get("subject_type") == "pull_request"
                or event_type in pollers._GITHUB_PR_EVENT_TYPES
                or (event_type == "issue_comment" and isinstance(url, str)
                    and "/pull/" in urllib.parse.urlsplit(url).path)
            )
            if not is_pr:
                saw_unknown = True
                continue
            repo = item.get("repo")
            number = item.get("number")
            if isinstance(number, str) and number.isdigit():
                number = int(number)
            parts = repo.split("/") if isinstance(repo, str) else []
            if (len(parts) != 2 or not all(parts) or not isinstance(number, int)
                    or isinstance(number, bool) or number < 1):
                saw_unknown = True
                continue
            saw_pr = True
            key = (repo, number)
            if key not in cache:
                escaped_repo = "/".join(urllib.parse.quote(value, safe="") for value in parts)
                attestation = await run_in_pool(
                    pollers._ATTESTATION_POOL, pollers._github_api_attestation,
                    f"repos/{escaped_repo}/pulls/{number}", token,
                )
                if (attestation is None or attestation[0] != 200
                        or not isinstance(attestation[1], dict)
                        or attestation[1].get("state") not in {"open", "closed"}):
                    cache[key] = None
                else:
                    cache[key] = attestation[1]["state"] == "open"
            verdict = cache[key]
            if verdict is True:
                return True
            if verdict is None:
                saw_unknown = True
        if saw_unknown or not saw_pr:
            return None
        return False

    return check


def worklink_recovery_relevance_check(persist_dir: Path) -> "RelevanceFn":
    """Compatibility entry point for Worklink's recovery hook."""
    from .worklink.poller_hooks import recovery_relevance_check

    return recovery_relevance_check(persist_dir)


def _worklink_receipt_liveness(
    persist_dir: Path,
    home: Path | None,
    prune_stale: PruneStaleReceipts,
) -> None:
    from .worklink.poller_hooks import report_live_delivery_receipts

    report_live_delivery_receipts(persist_dir, home, prune_stale)


_CORE_HOOK_PROFILES = {
    "github": PollerHookProfile(
        skill="github-poller",
        reserved_name="github-activity",
        hooks=PollerHooks(recovery_relevance=_github_recovery),
    ),
    "worklink": PollerHookProfile(
        skill="chainlink-orchestrator",
        reserved_name="worklink-ready-queue",
        hooks=PollerHooks(
            recovery_relevance=_worklink_recovery,
            receipt_liveness=_worklink_receipt_liveness,
        ),
    ),
}


def reserved_hook_profile_for_name(name: str) -> str | None:
    """Return the hook profile expected by a reserved poller name."""
    for profile_name, profile in _CORE_HOOK_PROFILES.items():
        if profile.reserved_name == name:
            return profile_name
    return None


def reserved_skill_for_name(name: str) -> str | None:
    """Return the only skill allowed to claim a reserved poller name."""
    if name == "github-ci-watch":
        return "github-ci-watch"
    profile_name = reserved_hook_profile_for_name(name)
    if profile_name is None:
        return None
    return _CORE_HOOK_PROFILES[profile_name].skill


def resolve_poller_hooks(
    raw: object, manifest_path: Path, *, policy: "PolicyRegistry | None" = None,
    plugin_owner: str | None = None,
) -> PollerHooks | None:
    """Resolve selected provider hooks without importing skill-controlled code."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"unknown poller hooks profile: {raw!r}")
    if raw == "worklink" and plugin_owner is not None:
        registered = policy.poller_hooks.get(raw) if policy is not None else None
        if (plugin_owner != "cultivate" or manifest_path.parent.name != "chainlink-orchestrator"
                or registered is None or registered[0] != plugin_owner):
            raise ValueError(f"reserved or foreign poller hooks profile: {raw!r}")
        hooks = registered[1]()
        if not isinstance(hooks, PollerHooks):
            raise ValueError(f"invalid poller hooks profile: {raw!r}")
        return hooks
    if raw not in _CORE_HOOK_PROFILES:
        registered = policy.poller_hooks.get(raw) if policy is not None else None
        if registered is None or plugin_owner is None or registered[0] != plugin_owner:
            raise ValueError(f"unknown or foreign poller hooks profile: {raw!r}")
        hooks = registered[1]()
        if not isinstance(hooks, PollerHooks):
            raise ValueError(f"invalid poller hooks profile: {raw!r}")
        return hooks
    profile = _CORE_HOOK_PROFILES[raw]
    if plugin_owner is not None:
        raise ValueError(f"core poller hooks profile {raw!r} is reserved")
    if manifest_path.parent.name != profile.skill:
        raise ValueError(
            f"poller hooks profile {raw!r} may only be declared by "
            f"the {profile.skill!r} skill"
        )
    return profile.hooks
