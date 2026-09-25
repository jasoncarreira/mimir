"""Trusted hook profiles declared by poller manifests."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Collection

if TYPE_CHECKING:
    from .poller_recovery import RelevanceFn
    from .pollers import PollerConfig


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


@lru_cache(maxsize=1)
def _github_hooks_module():
    path = (
        Path(__file__).parent
        / "optional-skills" / "github-poller" / "scripts" / "hooks.py"
    )
    spec = importlib.util.spec_from_file_location("mimir._github_poller_hooks", path)
    if spec is None or spec.loader is None:  # pragma: no cover - fixed package path
        raise RuntimeError(f"could not load GitHub poller hooks from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _github_recovery(poller: "PollerConfig") -> "RelevanceFn":
    token = poller.env.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    return github_recovery_relevance_check(token)


def _worklink_recovery(poller: "PollerConfig") -> "RelevanceFn":
    return worklink_recovery_relevance_check(poller.resolved_persist_dir())


def github_recovery_relevance_check(token: str) -> "RelevanceFn":
    """Compatibility entry point for the GitHub skill's recovery hook."""
    return _github_hooks_module().recovery_relevance_check(token)


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


POLLER_HOOK_PROFILES = {
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
    for profile_name, profile in POLLER_HOOK_PROFILES.items():
        if profile.reserved_name == name:
            return profile_name
    return None


def reserved_skill_for_name(name: str) -> str | None:
    """Return the only skill allowed to claim a reserved poller name."""
    profile_name = reserved_hook_profile_for_name(name)
    if profile_name is None:
        return None
    return POLLER_HOOK_PROFILES[profile_name].skill


def resolve_poller_hooks(raw: object, manifest_path: Path) -> PollerHooks | None:
    """Resolve a manifest hook profile without importing skill-controlled code."""
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in POLLER_HOOK_PROFILES:
        raise ValueError(f"unknown poller hooks profile: {raw!r}")
    profile = POLLER_HOOK_PROFILES[raw]
    if manifest_path.parent.name != profile.skill:
        raise ValueError(
            f"poller hooks profile {raw!r} may only be declared by "
            f"the {profile.skill!r} skill"
        )
    return profile.hooks
