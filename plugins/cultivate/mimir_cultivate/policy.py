"""Cultivate's owner-bound repo and pull-request action vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


class RepoPRAction(StrEnum):
    INSPECT = "repo.inspect"
    CHECKOUT = "repo.checkout"
    TEST = "repo.test"
    WRITE = "repo.write"
    COMMIT = "repo.commit"
    PUSH = "repo.push"
    PR_COMMENT = "pr.comment"
    PR_EDIT = "pr.edit"
    PR_REREQUEST = "pr.rerequest"
    PR_REVIEW = "pr.review"


def register_action_protocol(registrar: PolicyRegistrar, validate_scope: Callable[..., bool]) -> None:
    """Bind the vocabulary and grant validator to this selected registrar."""
    registrar.register_scoped_action(
        RepoPRAction, validate_scope, checkout_action=RepoPRAction.CHECKOUT,
        live_lease=_live_scoped_pr_lease,
    )


def _live_scoped_pr_lease(scope: object, review_state: object) -> object | None:
    attached = getattr(review_state, "checkout_lease", None)
    if attached is None or not getattr(attached, "is_active", False):
        return None
    try:
        root = Path(os.environ["MIMIR_PR_CHECKOUT_LEASE_ROOT"])
        if not root.is_absolute() or root.is_symlink():
            return None
        root = root.resolve(strict=True)
        path = Path(attached.path)
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            return None
        metadata = path / ".git/mimir-pr-checkout-lease.json"
        if (metadata.is_symlink() or metadata.parent.is_symlink()
                or (path / ".git/mimir-pr-checkout-lease-reclamation.json").exists()):
            return None
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if any(raw.get(field) != str(getattr(attached, field)) for field in ("path", "lease_root")):
            return None
        if any(raw.get(field) != getattr(attached, field) for field in (
            "recovery_id", "scope_id", "owner", "head_sha", "canonical_origin",
            "canonical_repo", "pr_number", "destination_ref",
        )):
            return None
        expires = datetime.fromisoformat(raw["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(UTC) or expires != attached.expires_at:
            return None
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None
    if (attached.scope_id != scope.scope_id or attached.owner != scope.principal
            or attached.head_sha.lower() != scope.observed_head_sha.lower()
            or attached.canonical_repo.lower() != scope.canonical_repo.lower()
            or getattr(review_state, "action_scope", None) is not scope):
        return None
    if _observed_checkout_state(path) != (
        scope.head_ref, getattr(review_state, "git_expected_head", None)
    ):
        return None
    return attached


def _observed_checkout_state(path: Path) -> tuple[str, str] | None:
    """Inspect the current checkout without inheriting user Git configuration."""
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
    }
    output = bytearray()
    try:
        process = subprocess.Popen(
            ("/usr/bin/git", "-C", str(path), "-c", "core.fsmonitor=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
             "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
             "--no-pager", "--no-optional-locks", "status", "--porcelain=v2",
             "--branch", "--untracked-files=no"),
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        )
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                remaining_bytes = 1_048_576
                while selector.get_map():
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        return None
                    for key, _ in selector.select(min(remaining_time, 0.1)):
                        stream = key.fileobj
                        chunk = os.read(stream.fileno(), min(65_536, remaining_bytes + 1))
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if len(chunk) > remaining_bytes:
                            return None
                        remaining_bytes -= len(chunk)
                        if stream is process.stdout:
                            output.extend(chunk)
                if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    return None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
    except (OSError, subprocess.SubprocessError):
        return None
    fields = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.startswith("# branch."):
            key, separator, value = line[2:].partition(" ")
            if separator:
                fields[key] = value
    branch, head = fields.get("branch.head"), fields.get("branch.oid", "").lower()
    if (not branch or branch == "(detached)" or len(head) != 40
            or any(char not in "0123456789abcdef" for char in head)):
        return None
    return branch, head


def validate_scoped_pr_action(action: RepoPRAction, scope: object, grant: object) -> bool:
    """Validate the selected owner's action against host-observed authority."""
    return (
        type(action) is RepoPRAction
        and action.value in getattr(scope, "allowed_operations", ())
        and getattr(grant, "scope_id", None) == getattr(scope, "scope_id", None)
        and getattr(grant, "owner", None) == "cultivate"
        and getattr(grant, "action", None) == action.value
    )
