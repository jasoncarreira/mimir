"""Atomic, scope-bound pull-request checkout leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
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
PUBLISHED_HEAD_REF = "refs/mimir/pr-checkout-lease/published"
_LEASE_ROOT_ENV = "MIMIR_PR_CHECKOUT_LEASE_ROOT"
_CLEANUP_IDENTITY_FIELDS = (
    "canonical_repo",
    "canonical_origin",
    "head_sha",
    "destination_ref",
    "owner",
    "scope_id",
    "path",
    "lease_root",
    "recovery_id",
)


def _report_retained_candidates(
    event_type: str,
    scope: RepoPRActionScope,
    candidates: Sequence[tuple[Path, str]],
) -> None:
    """Best-effort structured evidence for retained work and reconciliation refusals."""
    try:
        from .event_logger import log_event_sync

        log_event_sync(
            event_type,
            repository=scope.canonical_repo,
            pull_request=scope.pr_number,
            scope_id=scope.scope_id,
            candidates=[
                {"commit": head, "path": str(path)} for path, head in candidates
            ],
        )
    except Exception:  # noqa: BLE001 - evidence failure must not change lease safety
        pass


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
    pr_number: int = 0

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
        "version": 3,
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
        "pr_number": lease.pr_number,
    }


def _safe_lease_path(root: Path, path: Path, *, must_exist: bool) -> Path:
    root = root.resolve(strict=True)
    if path.parent.resolve(strict=True) != root or path == root or path.is_symlink():
        raise RuntimeError(
            "PR checkout lease path escapes its configured root: "
            f"expected child of {str(root)!r}; actual {str(path)!r}"
        )
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
            ["git", "-C", str(staging), "fetch", "--no-tags", "origin",
             scope.checkout_ref or scope.destination_ref],
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
        base_ancestry = runner([
            "git", "-C", str(staging), "merge-base", "--is-ancestor",
            scoped_base, actual_base,
        ])
        if base_ancestry.returncode == 1:
            raise RuntimeError(
                "PR base history rewritten: scoped base "
                f"{scoped_base} is unreachable from fetched base {actual_base}"
            )
        if base_ancestry.returncode != 0:
            raise RuntimeError(
                (base_ancestry.stderr or base_ancestry.stdout).strip()
                or "PR base ancestry check failed"
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
            pr_number=scope.pr_number,
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
        _run(
            runner,
            ["git", "-C", str(staging), "update-ref", PUBLISHED_HEAD_REF, actual_head],
            "could not record the published PR head",
        )
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
    renew_expired: bool = False,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> PRCheckoutLease:
    """Recover exact-scope work after revalidating its original remote PR head."""
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
    if "pr_number" in raw and raw.get("pr_number") != scope.pr_number:
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
            pr_number=scope.pr_number,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expired = datetime.now(UTC) >= lease.expires_at
    if (lease.revoked or not path.is_dir() or expired) and not (expired and renew_expired):
        raise RuntimeError("PR checkout lease is expired")
    head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        "recovered PR checkout has no HEAD",
    ).lower()
    origin = _run(
        runner, ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        "recovered PR checkout has no origin",
    )
    branch = _run(
        runner, ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
        "recovered PR checkout is detached",
    )
    ancestor = runner([
        "git", "-C", str(path), "merge-base", "--is-ancestor", lease.head_sha.lower(), head,
    ])
    if (
        origin != lease.canonical_origin
        or branch != scope.head_ref
        or ancestor.returncode != 0
    ):
        raise RuntimeError("recovered PR checkout identity mismatch")
    _assert_self_contained_checkout(path, runner=runner)
    _run(
        runner,
        ["git", "-C", str(path), "fetch", "--no-tags", "origin",
         scope.checkout_ref or scope.destination_ref],
        "PR head fetch failed during lease recovery",
    )
    remote_head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        "fetched PR head is missing during lease recovery",
    ).lower()
    if remote_head != scope.observed_head_sha.lower():
        raise RuntimeError(
            f"PR head advanced: scoped head {scope.observed_head_sha.lower()} is stale; "
            f"fetched head is {remote_head}"
        )
    if expired:
        lease = replace_lease_expiry(lease, datetime.now(UTC) + ttl)
        (path / _METADATA).write_text(
            json.dumps(_metadata(lease), sort_keys=True) + "\n", encoding="utf-8",
        )
    if review_state is not None:
        review_state.attach_checkout_lease(lease)
        review_state.record_git_head(scope.scope_id, head)
    return lease


def replace_lease_expiry(lease: PRCheckoutLease, expires_at: datetime) -> PRCheckoutLease:
    """Renew a validated retained lease without changing any identity field."""
    values = {field: getattr(lease, field) for field in lease.__dataclass_fields__}
    values["expires_at"] = expires_at
    return PRCheckoutLease(**values)


def _retained_candidate_head(
    path: Path,
    scope: RepoPRActionScope,
    *,
    root: Path,
    owner: str,
    runner: Runner,
) -> str:
    """Validate retained metadata and local ancestry without renewing the lease."""
    path = _safe_lease_path(root, path, must_exist=True)
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease recovery metadata is invalid") from exc
    expected = {
        "canonical_repo": scope.canonical_repo,
        "canonical_origin": scope.canonical_origin,
        "owner": owner,
        "scope_id": scope.scope_id,
        "destination_ref": scope.destination_ref,
        "head_sha": scope.observed_head_sha,
        "path": str(path),
        "lease_root": str(root),
    }
    if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
    if "pr_number" in raw and raw.get("pr_number") != scope.pr_number:
        raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
    head = _run(
        runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        "retained PR checkout has no HEAD",
    ).lower()
    ancestor = runner([
        "git", "-C", str(path), "merge-base", "--is-ancestor",
        scope.observed_head_sha.lower(), head,
    ])
    if ancestor.returncode != 0:
        raise RuntimeError(f"retained PR checkout ancestry mismatch at {path}")
    return head


def acquire_pr_checkout_lease(
    scope: RepoPRActionScope,
    *,
    owner: str,
    lease_root: Path | None = None,
    ttl: timedelta = timedelta(hours=2),
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> tuple[PRCheckoutLease, tuple[str, ...]]:
    """Resume the sole exact-scope lease, refusing divergent retained fixes."""
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
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        prefix = f"{scope.scope_id[:16]}-"
        paths: list[Path] = []
        foreign_candidates: list[tuple[Path, str]] = []
        for path in sorted(root.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            try:
                raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                if path.name.startswith(prefix):
                    raise RuntimeError("PR checkout lease recovery metadata is invalid")
                continue
            if not isinstance(raw, dict):
                if path.name.startswith(prefix):
                    raise RuntimeError("PR checkout lease recovery metadata is invalid")
                continue
            if raw.get("scope_id") == scope.scope_id:
                paths.append(path)
                continue
            if path.name.startswith(prefix):
                raise RuntimeError(f"PR checkout lease recovery scope mismatch at {path}")
            same_pr = (
                raw.get("canonical_repo") == scope.canonical_repo
                and raw.get("destination_ref") == scope.destination_ref
                and raw.get("head_sha") == scope.observed_head_sha
                and raw.get("owner") == owner
                and ("pr_number" not in raw or raw.get("pr_number") == scope.pr_number)
            )
            if same_pr:
                head = _run(
                    runner, ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                    "retained PR checkout has no HEAD",
                ).lower()
                if head != scope.observed_head_sha.lower():
                    foreign_candidates.append((path, head))
        candidates = [
            (path, _retained_candidate_head(
                path, scope, root=root, owner=owner, runner=runner,
            ))
            for path in paths
        ]
        unpublished = [
            (path, head) for path, head in candidates
            if head != scope.observed_head_sha.lower()
        ]
        if foreign_candidates:
            _report_retained_candidates(
                "pr_checkout_lease_scope_conflict",
                scope,
                [*unpublished, *foreign_candidates],
            )
            named = ", ".join(
                f"{head} ({path}, scope mismatch)"
                for path, head in [*unpublished, *foreign_candidates]
            )
            raise RuntimeError(
                "unpublished PR checkout lease candidates include another scope; "
                f"refusing reuse: {named}"
            )
        distinct = sorted({head for _path, head in unpublished})
        if len(distinct) > 1:
            _report_retained_candidates(
                "pr_checkout_lease_candidates_diverged", scope, unpublished,
            )
            named = ", ".join(f"{head} ({path})" for path, head in unpublished)
            raise RuntimeError(
                "divergent unpublished PR checkout lease candidates; "
                f"refusing implicit selection: {named}"
            )
        if candidates:
            chosen_path, _head = unpublished[0] if unpublished else candidates[0]
            lease = recover_pr_checkout_lease(
                chosen_path,
                scope,
                owner=owner,
                lease_root=root,
                renew_expired=True,
                ttl=ttl,
                review_state=review_state,
                runner=runner,
            )
            if unpublished:
                _report_retained_candidates(
                    "pr_checkout_lease_resumed", scope, unpublished,
                )
            return lease, tuple(distinct)
        lease = create_pr_checkout_lease(
            scope,
            owner=owner,
            lease_root=root,
            ttl=ttl,
            review_state=review_state,
            runner=runner,
        )
        return lease, ()
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


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
    if not isinstance(raw, dict):
        raise RuntimeError(
            "PR checkout lease cleanup metadata type mismatch: "
            f"expected dict; actual {type(raw).__name__}"
        )
    for key in _CLEANUP_IDENTITY_FIELDS:
        if key not in raw:
            raise RuntimeError(
                f"PR checkout lease cleanup metadata {key} mismatch: "
                f"expected {expected[key]!r}; actual <missing>"
            )
    for key, value in expected.items():
        # Version describes metadata shape, not checkout identity. Older shapes
        # remain safe when all identity fields are present and match.
        if key != "version" and key in raw and raw[key] != value:
            raise RuntimeError(
                f"PR checkout lease cleanup metadata {key} mismatch: "
                f"expected {value!r}; actual {raw[key]!r}"
            )
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
    if origin != lease.canonical_origin:
        raise RuntimeError(
            "PR checkout lease cleanup origin mismatch: "
            f"expected {lease.canonical_origin!r}; actual {origin!r}"
        )
    expected_branch = lease.destination_ref.removeprefix("refs/heads/")
    branch_result = runner([
        "git", "-C", str(lease.path), "symbolic-ref", "--quiet", "--short", "HEAD",
    ])
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "<detached>"
    if branch != expected_branch:
        raise RuntimeError(
            "PR checkout lease cleanup branch mismatch: "
            f"expected {expected_branch!r}; actual {branch!r}"
        )
    published = runner([
        "git", "-C", str(lease.path), "rev-parse", "--verify",
        f"{PUBLISHED_HEAD_REF}^{{commit}}",
    ])
    if published.returncode == 0:
        published_head = published.stdout.strip().lower()
    else:
        # Successful push verification leaves this proof in older leases that
        # predate the private publication ref.
        published_head = _run(
            runner,
            ["git", "-C", str(lease.path), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            "PR checkout lease cleanup found no publication proof",
        ).lower()
    ancestor = runner([
        "git", "-C", str(lease.path), "merge-base", "--is-ancestor", head,
        published_head,
    ])
    if ancestor.returncode != 0:
        raise RuntimeError(
            "PR checkout lease cleanup publication mismatch: "
            f"HEAD {head!r} is not contained in published commit {published_head!r}"
        )
    shutil.rmtree(lease.path)
    return True
