"""Atomic, scope-bound pull-request checkout leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence
import uuid

from .models import RepoPRAction, RepoPRActionScope, RepoReviewState
from .worklink.checkout import _assert_self_contained_checkout, _clone_attempt_checkout


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_METADATA = ".git/mimir-pr-checkout-lease.json"
_LEASE_ROOT_ENV = "MIMIR_PR_CHECKOUT_LEASE_ROOT"


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class PRCheckoutLease:
    canonical_repo: str
    canonical_origin: str
    source_root: Path
    scope_base_sha: str
    base_sha: str
    head_sha: str
    destination_ref: str
    owner: str
    scope_id: str
    path: Path
    lease_root: Path
    created_at: datetime
    expires_at: datetime
    recovery_id: str
    recovered: bool = False
    revoked: bool = False

    @property
    def write_root(self) -> Path:
        return self.path

    @property
    def is_active(self) -> bool:
        return not self.revoked and datetime.now(UTC) < self.expires_at and self.path.is_dir()

    def revoke(self) -> None:
        object.__setattr__(self, "revoked", True)


def configured_pr_checkout_lease_root() -> Path:
    raw = os.environ.get(_LEASE_ROOT_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{_LEASE_ROOT_ENV} is not configured")
    root = Path(raw)
    if not root.is_absolute():
        raise RuntimeError(f"{_LEASE_ROOT_ENV} must be an absolute path")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    return root.resolve(strict=True)


def _run(runner: Runner, args: list[str], failure: str) -> str:
    result = runner(args)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or failure)
    return result.stdout.strip()


def _metadata(lease: PRCheckoutLease) -> dict[str, object]:
    return {
        "version": 2,
        "canonical_repo": lease.canonical_repo,
        "canonical_origin": lease.canonical_origin,
        "source_root": str(lease.source_root),
        "scope_base_sha": lease.scope_base_sha,
        "base_sha": lease.base_sha,
        "head_sha": lease.head_sha,
        "destination_ref": lease.destination_ref,
        "owner": lease.owner,
        "scope_id": lease.scope_id,
        "path": str(lease.path),
        "lease_root": str(lease.lease_root),
        "created_at": lease.created_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "recovery_id": lease.recovery_id,
    }


def _safe_lease_path(root: Path, path: Path, *, must_exist: bool) -> Path:
    root = root.resolve(strict=True)
    if path.parent.resolve(strict=True) != root or path == root or path.is_symlink():
        raise RuntimeError("PR checkout lease path escapes its configured root")
    if must_exist and not path.is_dir():
        raise RuntimeError("PR checkout lease path is missing")
    return path


def create_pr_checkout_lease(
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Publish a self-contained checkout only after exact scope verification."""
    if RepoPRAction.CHECKOUT.value not in scope.allowed_operations:
        raise RuntimeError("scope does not grant PR checkout")
    if owner != scope.principal:
        raise RuntimeError("PR checkout lease owner does not match scope")
    if ttl <= timedelta(0):
        raise ValueError("PR checkout lease ttl must be positive")

    if lease_root is not None and lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = (lease_root.resolve(strict=True) if lease_root is not None
            else configured_pr_checkout_lease_root())
    source = Path(scope.canonical_root).resolve(strict=True)
    observed_origin = _run(
        runner, ["git", "-C", str(source), "config", "--get", "remote.origin.url"],
        "source checkout has no origin",
    )
    if observed_origin != scope.canonical_origin:
        raise RuntimeError("source checkout origin does not match PR scope")

    recovery_id = uuid.uuid4().hex
    name = f"{scope.scope_id[:16]}-{recovery_id}"
    path = root / name
    staging = root / f".{name}.staging"
    _safe_lease_path(root, staging, must_exist=False)
    if path.exists() or path.is_symlink() or staging.exists() or staging.is_symlink():
        raise RuntimeError("PR checkout lease collision")

    now = datetime.now(UTC)
    try:
        _clone_attempt_checkout(source, staging, runner=runner, event_logger=None)
        _run(
            runner,
            ["git", "-C", str(staging), "remote", "set-url", "origin", scope.canonical_origin],
            "git remote set-url failed",
        )
        _run(
            runner,
            ["git", "-C", str(staging), "fetch", "--no-tags", "origin", scope.destination_ref],
            "PR head fetch failed",
        )
        actual_head = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "fetched PR head is missing",
        ).lower()
        if actual_head != scope.observed_head_sha.lower():
            raise RuntimeError(
                f"PR head advanced: scoped head {scope.observed_head_sha.lower()} is stale; "
                f"fetched head is {actual_head}"
            )
        _run(
            runner,
            ["git", "-C", str(staging), "fetch", "--no-tags", "origin", scope.base_ref],
            "PR base fetch failed",
        )
        actual_base = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "fetched PR base is missing",
        ).lower()
        merge_base_result = runner([
            "git", "-C", str(staging), "merge-base", actual_head, actual_base,
        ])
        merge_base = (
            merge_base_result.stdout.strip().lower()
            if merge_base_result.returncode == 0 else "none"
        )
        scoped_base = scope.observed_base_sha.lower()
        if merge_base == actual_head:
            raise RuntimeError(
                "PR base already contains the authorized head: fetched base "
                f"{actual_base}; authorized head {actual_head} is stale as an open PR"
            )
        if actual_base != scoped_base and merge_base != scoped_base:
            raise RuntimeError(
                "PR base history rewritten: scoped base "
                f"{scoped_base} is stale; fetched base {actual_base} has merge-base "
                f"{merge_base} with the authorized PR head"
            )
        lease = PRCheckoutLease(
            canonical_repo=scope.canonical_repo,
            canonical_origin=scope.canonical_origin,
            source_root=source,
            scope_base_sha=scoped_base,
            base_sha=actual_base,
            head_sha=scope.observed_head_sha,
            destination_ref=scope.destination_ref,
            owner=owner,
            scope_id=scope.scope_id,
            path=path,
            lease_root=root,
            created_at=now,
            expires_at=now + ttl,
            recovery_id=recovery_id,
        )
        _run(
            runner,
            ["git", "-C", str(staging), "checkout", "-B", scope.head_ref, actual_head],
            "PR head checkout failed",
        )
        checked_out = _run(
            runner, ["git", "-C", str(staging), "rev-parse", "--verify", "HEAD"],
            "checked-out PR head is missing",
        ).lower()
        if checked_out != scope.observed_head_sha.lower():
            raise RuntimeError("checked-out HEAD does not match immutable scope")
        _assert_self_contained_checkout(staging, runner=runner)
        (staging / _METADATA).write_text(
            json.dumps(_metadata(lease), sort_keys=True) + "\n", encoding="utf-8",
        )
        os.replace(staging, path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if review_state is not None:
        review_state.attach_checkout_lease(lease)
    return lease


def recover_pr_checkout_lease(
    path: Path,
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Recover an unexpired lease only when disk metadata still matches scope."""
    if lease_root is not None and lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = (lease_root.resolve(strict=True) if lease_root is not None
            else configured_pr_checkout_lease_root())
    path = _safe_lease_path(root, path, must_exist=True)
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expected = {
        "canonical_repo": scope.canonical_repo,
        "canonical_origin": scope.canonical_origin,
        "source_root": str(Path(scope.canonical_root).resolve(strict=True)),
        "scope_base_sha": scope.observed_base_sha,
        "head_sha": scope.observed_head_sha,
        "destination_ref": scope.destination_ref,
        "owner": owner,
        "scope_id": scope.scope_id,
        "path": str(path),
        "lease_root": str(root),
    }
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError("PR checkout lease recovery scope mismatch")
    base_sha = raw.get("base_sha")
    if not isinstance(base_sha, str) or len(base_sha) != 40 or any(
        character not in "0123456789abcdef" for character in base_sha.lower()
    ):
        raise RuntimeError("PR checkout lease recovery metadata is invalid")
    try:
        lease = PRCheckoutLease(
            **{
                **expected,
                "source_root": Path(str(expected["source_root"])),
                "base_sha": base_sha.lower(),
                "path": path,
                "lease_root": root,
            },
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            recovery_id=str(raw["recovery_id"]),
            recovered=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    if not lease.is_active:
        raise RuntimeError("PR checkout lease is expired")
    head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        "recovered PR checkout has no HEAD",
    ).lower()
    origin = _run(
        runner, ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        "recovered PR checkout has no origin",
    )
    if head != lease.head_sha.lower() or origin != lease.canonical_origin:
        raise RuntimeError("recovered PR checkout identity mismatch")
    _assert_self_contained_checkout(path, runner=runner)
    return lease


def cleanup_pr_checkout_lease(
    lease: PRCheckoutLease,
    *,
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> bool:
    """Revoke and remove an exact lease; repeated cleanup is a safe no-op."""
    if review_state is not None:
        review_state.revoke_checkout_lease(lease)
    lease.revoke()
    if not lease.path.exists() and not lease.path.is_symlink():
        return False
    _safe_lease_path(lease.lease_root, lease.path, must_exist=True)
    try:
        raw = json.loads((lease.path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease cleanup metadata is invalid") from exc
    expected = _metadata(lease)
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError("PR checkout lease cleanup identity mismatch")
    head = _run(
        runner,
        ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
        "PR checkout lease cleanup found no HEAD",
    ).lower()
    origin = _run(
        runner,
        ["git", "-C", str(lease.path), "config", "--get", "remote.origin.url"],
        "PR checkout lease cleanup found no origin",
    )
    if head != lease.head_sha.lower() or origin != lease.canonical_origin:
        raise RuntimeError("PR checkout lease cleanup identity mismatch")
    shutil.rmtree(lease.path)
    return True
