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
import tarfile
from typing import Callable, Sequence
import uuid

from ._rmtree import rmtree_missing_ok
from .models import (
    NormalizedPullRequestSnapshot,
    RepoPRAction,
    RepoPRActionScope,
    RepoReviewState,
)
from .worklink.checkout import _assert_self_contained_checkout, _clone_attempt_checkout


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_METADATA = ".git/mimir-pr-checkout-lease.json"
_RECLAMATION_METADATA = ".git/mimir-pr-checkout-lease-reclamation.json"
_RECOVERY_DIRECTORY = ".recovery"
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


def _report_lease_acquired(
    lease: PRCheckoutLease,
    *,
    acquisition: str,
    runner: Runner,
) -> None:
    """Best-effort evidence of the checkout head bound by an established lease."""
    try:
        from .event_logger import log_event_sync

        head_sha = _run(
            runner,
            ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
            "acquired PR checkout lease has no HEAD",
        ).lower()
        log_event_sync(
            "pr_checkout_lease_acquired",
            repository=lease.canonical_repo,
            pull_request=lease.pr_number,
            scope_id=lease.scope_id,
            head_sha=head_sha,
            path=str(lease.path),
            owner=lease.owner,
            acquisition=acquisition,
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


def _observe_current_pr_head(scope: RepoPRActionScope) -> str:
    """Read the current head from the server-configured forge adapter."""
    from .tools.forge import _client_for_repository

    snapshot = _client_for_repository(scope.canonical_repo).get_pull_request_snapshot(
        scope.canonical_repo.lower(), scope.pr_number,
    )
    if (
        not isinstance(snapshot, NormalizedPullRequestSnapshot)
        or snapshot.number != scope.pr_number
        or snapshot.state != "open"
        or snapshot.head_ref != scope.head_ref
        or len(snapshot.head_sha) != 40
        or any(character not in "0123456789abcdef" for character in snapshot.head_sha.lower())
    ):
        raise RuntimeError("forge returned ambiguous pull-request head metadata")
    return snapshot.head_sha.lower()


def _report_superseded_lease(
    scope: RepoPRActionScope,
    lease: PRCheckoutLease,
    observed_head: str,
) -> None:
    try:
        from .event_logger import log_event_sync

        log_event_sync(
            "pr_checkout_lease_superseded",
            repository=scope.canonical_repo,
            pull_request=scope.pr_number,
            scope_id=lease.scope_id,
            superseded_head=lease.head_sha,
            observed_head=observed_head,
            path=str(lease.path),
        )
    except Exception:  # noqa: BLE001 - evidence failure must not change lease safety
        pass


@dataclass(frozen=True)
class PRCheckoutLeaseReclamation:
    lease: str
    path: Path
    expired_by_s: float
    size_bytes: int
    publication_proof_present: bool
    reclaimed: bool
    error: str | None = None
    recovery_bundle: Path | None = None
    canonical_origin_contains_head: bool | None = None


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
        (path / _RECLAMATION_METADATA).unlink(missing_ok=True)
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


def _lease_from_recorded_metadata(path: Path, root: Path) -> PRCheckoutLease:
    try:
        raw = json.loads((path / _METADATA).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("PR checkout lease reclamation metadata is invalid")
    try:
        created_at = datetime.fromisoformat(str(raw["created_at"]))
        expires_at = datetime.fromisoformat(str(raw["expires_at"]))
        lease = PRCheckoutLease(
            canonical_repo=str(raw["canonical_repo"]),
            canonical_origin=str(raw["canonical_origin"]),
            source_root=Path(str(raw["source_root"])),
            scope_base_sha=str(raw.get("scope_base_sha", raw["base_sha"])),
            base_sha=str(raw["base_sha"]),
            head_sha=str(raw["head_sha"]),
            destination_ref=str(raw["destination_ref"]),
            owner=str(raw["owner"]),
            scope_id=str(raw["scope_id"]),
            path=path,
            lease_root=root,
            created_at=created_at,
            expires_at=expires_at,
            recovery_id=str(raw["recovery_id"]),
            pr_number=int(raw.get("pr_number", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid") from exc
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        raise RuntimeError("PR checkout lease reclamation metadata is invalid")
    return lease


def _checkout_size_bytes(path: Path) -> int:
    total = 0
    for current_root, directories, files in os.walk(path, followlinks=False):
        entries = [*(Path(current_root) / name for name in directories),
                   *(Path(current_root) / name for name in files)]
        for entry in entries:
            try:
                total += entry.lstat().st_blocks * 512
            except OSError:
                pass
    return total


def _origin_contains_head(lease: PRCheckoutLease, head: str, runner: Runner) -> bool | None:
    """Classify whether the current HEAD is reachable from a fetched origin ref."""
    fetched = runner([
        "git", "-C", str(lease.path), "fetch", "--no-tags", "origin",
    ])
    if fetched.returncode != 0:
        return None
    refs = runner([
        "git", "-C", str(lease.path), "for-each-ref", f"--contains={head}",
        "--format=%(refname)", "refs/remotes/origin",
    ])
    if refs.returncode != 0:
        return None
    return bool(refs.stdout.strip())


def _tree_blob(
    path: Path,
    revision: str,
    name: str,
    runner: Runner,
) -> str | None:
    """Return one path's blob object id, with absence represented explicitly."""
    result = runner([
        "git", "-C", str(path), "ls-tree", "-z", revision, "--", f":(literal){name}",
    ])
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip()
            or f"PR checkout lease blob inspection failed for {name!r}"
        )
    if not result.stdout:
        return None
    entries = [entry for entry in result.stdout.split("\0") if entry]
    if len(entries) != 1 or "\t" not in entries[0]:
        raise RuntimeError(f"PR checkout lease blob inspection was ambiguous for {name!r}")
    metadata, observed_name = entries[0].split("\t", 1)
    fields = metadata.split()
    if observed_name != name or len(fields) != 3 or fields[1] != "blob":
        raise RuntimeError(f"PR checkout lease blob inspection was ambiguous for {name!r}")
    return fields[2].lower()


def _unexplained_publication_paths(
    lease: PRCheckoutLease,
    *,
    head: str,
    published_head: str,
    base_ref: str,
    runner: Runner,
) -> tuple[str, ...]:
    """Name HEAD paths whose content is in neither publication nor tracked base."""
    base_name = base_ref.removeprefix("refs/heads/")
    valid_base = runner(["git", "check-ref-format", "--branch", base_name])
    if valid_base.returncode != 0:
        raise RuntimeError("PR checkout lease tracked base ref is invalid")
    tracked_base = _run(
        runner,
        [
            "git", "-C", str(lease.path), "rev-parse", "--verify",
            f"refs/remotes/origin/{base_name}^{{commit}}",
        ],
        "PR checkout lease tracked base is unavailable",
    ).lower()
    bases = _run(
        runner,
        ["git", "-C", str(lease.path), "merge-base", "--all", head, tracked_base],
        "PR checkout lease HEAD has no verifiable tracked base",
    ).splitlines()
    if len(bases) != 1:
        raise RuntimeError("PR checkout lease HEAD has no unique tracked base")
    # A merge base computed against tracked_base is its ancestor by construction;
    # _run above and the uniqueness check are the fail-closed verification gates.
    base = bases[0].lower()
    changed = runner([
        "git", "-C", str(lease.path), "diff", "--name-only", "-z",
        published_head, head, "--",
    ])
    if changed.returncode != 0:
        raise RuntimeError(
            (changed.stderr or changed.stdout).strip()
            or "PR checkout lease changed-path inspection failed"
        )
    names = tuple(name for name in changed.stdout.split("\0") if name)
    unexplained = []
    for name in names:
        head_blob = _tree_blob(lease.path, head, name, runner)
        published_blob = _tree_blob(lease.path, published_head, name, runner)
        base_blob = _tree_blob(lease.path, base, name, runner)
        if head_blob not in {published_blob, base_blob}:
            unexplained.append(name)
    return tuple(unexplained)


def _preserve_checkout_head(lease: PRCheckoutLease, head: str, runner: Runner) -> Path:
    """Atomically preserve the complete history reachable from a lease HEAD."""
    recovery_root = lease.lease_root / _RECOVERY_DIRECTORY
    if recovery_root.is_symlink():
        raise RuntimeError("PR checkout lease recovery directory may not be a symlink")
    recovery_root.mkdir(mode=0o700, exist_ok=True)
    recovery_root = recovery_root.resolve(strict=True)
    if recovery_root.parent != lease.lease_root.resolve(strict=True):
        raise RuntimeError("PR checkout lease recovery directory escapes its lease root")
    bundle = recovery_root / f"{lease.path.name}-{lease.recovery_id}.bundle"
    if bundle.exists():
        verified = runner([
            "git", "-C", str(lease.path), "bundle", "verify", str(bundle),
        ])
        if verified.returncode != 0 or bundle.stat().st_size == 0:
            raise RuntimeError("existing PR checkout lease recovery bundle is invalid")
        heads = _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "list-heads", str(bundle)],
            "existing PR checkout lease recovery bundle has no heads",
        )
        if head not in {line.split()[0].lower() for line in heads.splitlines() if line}:
            raise RuntimeError("existing PR checkout lease recovery bundle has another HEAD")
        return bundle
    staging = recovery_root / f".{bundle.name}.{uuid.uuid4().hex}.tmp"
    try:
        _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "create", str(staging), "HEAD"],
            "PR checkout lease recovery bundle creation failed",
        )
        _run(
            runner,
            ["git", "-C", str(lease.path), "bundle", "verify", str(staging)],
            "PR checkout lease recovery bundle verification failed",
        )
        if staging.stat().st_size == 0:
            raise RuntimeError("PR checkout lease recovery bundle is empty")
        os.replace(staging, bundle)
    finally:
        staging.unlink(missing_ok=True)
    return bundle


def _preserve_dirty_worktree(lease: PRCheckoutLease, bundle: Path) -> Path:
    """Atomically archive dirty tracked and untracked files without changing them."""
    archive = bundle.with_suffix(".worktree.tar.gz")
    if archive.exists():
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError("existing dirty-worktree recovery archive is invalid")
        with tarfile.open(archive, "r:gz") as retained:
            if not retained.getmembers():
                raise RuntimeError("existing dirty-worktree recovery archive is empty")
        return archive

    staging = archive.with_name(f".{archive.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tarfile.open(staging, "w:gz") as retained:
            for entry in lease.path.iterdir():
                if entry.name != ".git":
                    retained.add(entry, arcname=str(Path("worktree") / entry.name))
        with tarfile.open(staging, "r:gz") as retained:
            if not retained.getmembers():
                raise RuntimeError("dirty-worktree recovery archive is empty")
        if staging.stat().st_size == 0:
            raise RuntimeError("dirty-worktree recovery archive is empty")
        os.replace(staging, archive)
    finally:
        staging.unlink(missing_ok=True)
    return archive


def _recover_retained_checkout(
    lease: PRCheckoutLease,
    *,
    head: str,
    dirty: bool,
    runner: Runner,
) -> Path:
    """Preserve all retained state before releasing its checkout directory."""
    try:
        bundle = _preserve_checkout_head(lease, head, runner)
        if not bundle.is_file() or bundle.stat().st_size == 0:
            raise RuntimeError("recovery bundle was not confirmed present and non-empty")
        if dirty:
            archive = _preserve_dirty_worktree(lease, bundle)
            if not archive.is_file() or archive.stat().st_size == 0:
                raise RuntimeError("dirty-worktree recovery archive was not confirmed")
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        raise RuntimeError(
            "PR checkout lease recovery failed; lease retained at "
            f"{lease.path} with HEAD {head}. Operator must preserve the checkout "
            f"and recover or publish its work manually: {exc}"
        ) from exc
    return bundle


def _mark_lease_unreclaimable(
    lease: PRCheckoutLease,
    *,
    observed_at: datetime,
    error: str,
) -> None:
    marker = lease.path / _RECLAMATION_METADATA
    staging = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    record = {
        "version": 1,
        "classification": "unreclaimable",
        "lease": lease.path.name,
        "path": str(lease.path),
        "recovery_id": lease.recovery_id,
        "decided_at": observed_at.isoformat(),
        "error": error,
    }
    try:
        staging.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, marker)
    finally:
        staging.unlink(missing_ok=True)


def reclaim_expired_pr_checkout_leases(
    lease_root: Path,
    *,
    active_paths: set[Path] | Callable[[], set[Path]] | None = None,
    now: datetime | None = None,
    runner: Runner = _default_runner,
    event_logger: Callable[..., None] | None = None,
) -> list[PRCheckoutLeaseReclamation]:
    """Safely reclaim expired leases not attached to a live turn."""
    if lease_root.is_symlink():
        raise RuntimeError("PR checkout lease root may not be a symlink")
    root = lease_root.resolve(strict=True)
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("PR checkout lease reclamation time must be timezone-aware")
    if event_logger is None:
        from .event_logger import log_event_sync

        event_logger = log_event_sync
    results: list[PRCheckoutLeaseReclamation] = []
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        current_active_paths = active_paths() if callable(active_paths) else active_paths
        active = {
            path.resolve(strict=False) for path in (current_active_paths or set())
        }
        for path in sorted(root.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            if (path / _RECLAMATION_METADATA).is_file():
                continue
            try:
                lease = _lease_from_recorded_metadata(path, root)
            except RuntimeError:
                continue
            if observed_at < lease.expires_at or path.resolve(strict=False) in active:
                continue
            size_bytes = _checkout_size_bytes(path)
            proof = runner([
                "git", "-C", str(path), "rev-parse", "--verify",
                f"{PUBLISHED_HEAD_REF}^{{commit}}",
            ]).returncode == 0
            error: str | None = None
            reclaimed = False
            recovery_bundle: Path | None = None
            canonical_origin_contains_head: bool | None = None
            try:
                head = _run(
                    runner,
                    ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                    "PR checkout lease reclamation found no HEAD",
                ).lower()
                canonical_origin_contains_head = _origin_contains_head(lease, head, runner)
                reclaimed = cleanup_pr_checkout_lease(lease, runner=runner)
                candidate = (
                    root / _RECOVERY_DIRECTORY
                    / f"{lease.path.name}-{lease.recovery_id}.bundle"
                )
                if candidate.is_file():
                    recovery_bundle = candidate
            except RuntimeError as exc:
                error = str(exc)
                if error in {
                    "PR checkout lease cleanup found no publication proof",
                } or error.startswith("PR checkout lease cleanup publication mismatch:"):
                    try:
                        head = _run(
                            runner,
                            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                            "PR checkout lease reclamation found no HEAD",
                        ).lower()
                        canonical_origin_contains_head = _origin_contains_head(
                            lease, head, runner,
                        )
                        recovery_bundle = _preserve_checkout_head(lease, head, runner)
                        rmtree_missing_ok(path)
                        reclaimed = True
                        error = None
                    except (OSError, RuntimeError) as preservation_exc:
                        error = (
                            f"{error}; preservation failed: {preservation_exc}"
                        )
                if not reclaimed:
                    try:
                        _mark_lease_unreclaimable(
                            lease, observed_at=observed_at, error=error,
                        )
                    except OSError as marker_exc:
                        error = f"{error}; terminal marker failed: {marker_exc}"
            result = PRCheckoutLeaseReclamation(
                lease=path.name,
                path=path,
                expired_by_s=max(0.0, (observed_at - lease.expires_at).total_seconds()),
                size_bytes=size_bytes,
                publication_proof_present=proof,
                reclaimed=reclaimed,
                error=error,
                recovery_bundle=recovery_bundle,
                canonical_origin_contains_head=canonical_origin_contains_head,
            )
            results.append(result)
            event_logger(
                "pr_checkout_lease_reclaimed" if reclaimed else "pr_checkout_lease_unreclaimable",
                lease=result.lease,
                path=str(result.path),
                expired_by_s=round(result.expired_by_s, 3),
                size_bytes=result.size_bytes,
                publication_proof_present=result.publication_proof_present,
                error=result.error,
                recovery_bundle=(
                    str(result.recovery_bundle) if result.recovery_bundle else None
                ),
                canonical_origin_contains_head=result.canonical_origin_contains_head,
            )
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)
    event_logger(
        "pr_checkout_lease_reaper_sweep",
        expired_count=len(results),
        reclaimed_count=sum(result.reclaimed for result in results),
        retained_count=sum(not result.reclaimed for result in results),
    )
    return results


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
        foreign_leases: list[PRCheckoutLease] = []
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
                and raw.get("owner") == owner
                and (
                    raw.get("pr_number") == scope.pr_number
                    or (
                        "pr_number" not in raw
                        and raw.get("destination_ref") == scope.destination_ref
                    )
                )
            )
            if same_pr:
                foreign_leases.append(_lease_from_recorded_metadata(path, root))
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
        foreign_candidates: list[tuple[Path, str]] = []
        if foreign_leases:
            try:
                observed_head = _observe_current_pr_head(scope)
            except Exception:  # noqa: BLE001 - observation failures must fail closed
                observed_head = None

            stale = [
                lease for lease in foreign_leases
                if observed_head is not None and lease.head_sha.lower() != observed_head
            ]
            retained: list[tuple[Path, str]] = []
            for lease in stale:
                head = _run(
                    runner,
                    ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
                    "retained PR checkout has no HEAD",
                ).lower()
                status = _run(
                    runner,
                    ["git", "-C", str(lease.path), "status", "--porcelain=v1",
                     "--untracked-files=all"],
                    "retained PR checkout status inspection failed",
                )
                if status:
                    retained.append((lease.path, head))
                    continue
                if head != lease.head_sha.lower():
                    published_head = _run(
                        runner,
                        [
                            "git", "-C", str(lease.path), "rev-parse", "--verify",
                            f"{PUBLISHED_HEAD_REF}^{{commit}}",
                        ],
                        "PR checkout lease cleanup found no publication proof",
                    ).lower()
                    ancestor = runner([
                        "git", "-C", str(lease.path), "merge-base", "--is-ancestor",
                        head, published_head,
                    ])
                    if ancestor.returncode not in {0, 1}:
                        raise RuntimeError(
                            (ancestor.stderr or ancestor.stdout).strip()
                            or "PR checkout lease publication ancestry inspection failed"
                        )
                    unexplained = () if ancestor.returncode == 0 else (
                        _unexplained_publication_paths(
                            lease,
                            head=head,
                            published_head=published_head,
                            base_ref=scope.base_ref,
                            runner=runner,
                        )
                    )
                    if unexplained and _origin_contains_head(lease, head, runner) is not True:
                        retained.append((lease.path, head))

            if retained:
                _report_retained_candidates("pr_checkout_lease_retained", scope, retained)

            for lease in stale:
                cleanup_pr_checkout_lease(
                    lease, base_ref=scope.base_ref, runner=runner,
                )
                _report_superseded_lease(scope, lease, observed_head)
            foreign_leases = [lease for lease in foreign_leases if lease not in stale]
            for lease in foreign_leases:
                head = _run(
                    runner,
                    ["git", "-C", str(lease.path), "rev-parse", "--verify", "HEAD"],
                    "retained PR checkout has no HEAD",
                ).lower()
                foreign_candidates.append((lease.path, head))

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
            _report_lease_acquired(lease, acquisition="resume", runner=runner)
            return lease, tuple(distinct)
        lease = create_pr_checkout_lease(
            scope,
            owner=owner,
            lease_root=root,
            ttl=ttl,
            review_state=review_state,
            runner=runner,
        )
        _report_lease_acquired(lease, acquisition="fresh", runner=runner)
        return lease, ()
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def cleanup_pr_checkout_lease(
    lease: PRCheckoutLease,
    *,
    base_ref: str | None = None,
    review_state: RepoReviewState | None = None,
    runner: Runner = _default_runner,
) -> bool:
    """Remove an exact lease, preserving any work not proven published."""
    if not lease.path.exists() and not lease.path.is_symlink():
        if review_state is not None:
            review_state.revoke_checkout_lease(lease)
        lease.revoke()
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
    status = _run(
        runner,
        ["git", "-C", str(lease.path), "status", "--porcelain=v1",
         "--untracked-files=all"],
        "PR checkout lease cleanup status inspection failed",
    )
    published = runner([
        "git", "-C", str(lease.path), "rev-parse", "--verify",
        f"{PUBLISHED_HEAD_REF}^{{commit}}",
    ])
    ancestor_returncode = 1
    published_head = "<missing>"
    if published.returncode == 0:
        published_head = published.stdout.strip().lower()
        ancestor = runner([
            "git", "-C", str(lease.path), "merge-base", "--is-ancestor", head,
            published_head,
        ])
        if ancestor.returncode not in {0, 1}:
            raise RuntimeError(
                (ancestor.stderr or ancestor.stdout).strip()
                or "PR checkout lease publication ancestry inspection failed"
            )
        ancestor_returncode = ancestor.returncode
    unexplained: tuple[str, ...] = ()
    if published.returncode == 0 and ancestor_returncode == 1 and base_ref is not None:
        unexplained = _unexplained_publication_paths(
            lease,
            head=head,
            published_head=published_head,
            base_ref=base_ref,
            runner=runner,
        )
    retained_head = (
        published.returncode != 0
        or (ancestor_returncode == 1 and (base_ref is None or bool(unexplained)))
    )
    if retained_head and not status and _origin_contains_head(lease, head, runner) is True:
        retained_head = False
    if status or retained_head:
        _recover_retained_checkout(
            lease, head=head, dirty=bool(status), runner=runner,
        )
    rmtree_missing_ok(lease.path)
    if review_state is not None:
        review_state.revoke_checkout_lease(lease)
    lease.revoke()
    return True
