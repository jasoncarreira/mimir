"""Closed, scope-bound repository and pull-request tools."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from ..forge import ForgeClient, ForgeError, ReviewVerdict
from ..models import AuthContext, RepoPRAction, RepoPRActionScope

_BODY_MAX_BYTES = 65_536
_PATH_MAX_BYTES = 4_096
_OPERATION = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_clients: dict[str, ForgeClient] = {}
_default_client: ForgeClient | None = None
_escalation_lock = threading.Lock()


def register_forge_client(client: ForgeClient, *, repositories: tuple[str, ...]) -> None:
    """Register an adapter for exact repositories without changing the tools."""
    for repository in repositories:
        normalized = repository.strip().lower()
        if not normalized or "/" not in normalized:
            raise ValueError("invalid repository registration")
        _clients[normalized] = client


def set_forge_client(client: ForgeClient | None) -> None:
    """Set the fallback adapter used when no repository registration exists."""
    global _default_client
    _default_client = client


def _scope(
    runtime: ToolRuntime[AuthContext] | None,
    action: RepoPRAction | None,
) -> RepoPRActionScope:
    context = getattr(runtime, "context", None)
    scope = getattr(context, "repo_pr_action_scope", None)
    if not isinstance(scope, RepoPRActionScope):
        raise ToolException("pull-request operation rejected: no immutable scope")
    if action is not None and action.value not in scope.allowed_operations:
        raise ToolException(f"pull-request operation rejected: {action.value} not granted")
    return scope


def _client(scope: RepoPRActionScope) -> ForgeClient:
    client = _clients.get(scope.canonical_repo.lower()) or _default_client
    if client is None:
        from ..forge.github import GitHubForgeClient

        client = GitHubForgeClient()
    return client


def _body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolException("body must be non-empty text")
    if len(value.encode("utf-8")) > _BODY_MAX_BYTES:
        raise ToolException("body exceeds 65536-byte limit")
    if "\x00" in value:
        raise ToolException("body contains a prohibited null byte")
    return value


def _path(value: str) -> str:
    path = Path(value)
    if (
        not value
        or len(value.encode("utf-8")) > _PATH_MAX_BYTES
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ToolException("path must be a bounded relative repository path")
    return value


def _call(operation: Any) -> Any:
    try:
        return operation()
    except ForgeError as exc:
        raise ToolException(str(exc)) from exc


@tool
def pr_metadata(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read bounded metadata for the pull request bound to this turn."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return asdict(_call(lambda: _client(scope).get_pull_request(scope)))


@tool
def pr_files(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded file projections for the pull request bound to this turn."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return [asdict(item) for item in _call(lambda: _client(scope).list_files(scope))]


@tool
def pr_diff(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read the bounded unified diff for the pull request bound to this turn."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return _call(lambda: _client(scope).get_diff(scope))


@tool
def pr_checks(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded check projections for the bound pull request head."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return [asdict(item) for item in _call(lambda: _client(scope).list_checks(scope))]


@tool
def pr_reviews(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded submitted-review projections for the bound pull request."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return [asdict(item) for item in _call(lambda: _client(scope).list_reviews(scope))]


@tool
def pr_comments(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded conversation and inline comments for the bound pull request."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return [asdict(item) for item in _call(lambda: _client(scope).list_comments(scope))]


@tool
def pr_review_requests(
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded pending review requests for the bound pull request."""
    scope = _scope(runtime, RepoPRAction.INSPECT)
    return [
        asdict(item)
        for item in _call(lambda: _client(scope).list_review_requests(scope))
    ]


@tool
def pr_submit_review(
    verdict: ReviewVerdict,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Submit one approve, comment, or request-changes review on the bound PR."""
    scope = _scope(runtime, RepoPRAction.PR_REVIEW)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).submit_review(scope, verdict, safe_body)))


@tool
def pr_inline_review_comment(
    path: str,
    line: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one inline review comment to a right-side line on the bound PR head."""
    scope = _scope(runtime, RepoPRAction.PR_REVIEW)
    if isinstance(line, bool) or not isinstance(line, int) or line < 1 or line > 10_000_000:
        raise ToolException("line must be a positive bounded integer")
    safe_path = _path(path)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).add_inline_review_comment(
        scope, path=safe_path, line=line, body=safe_body,
    )))


@tool
def pr_comment(
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one conversation comment to the pull request bound to this turn."""
    scope = _scope(runtime, RepoPRAction.PR_COMMENT)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).add_pull_request_comment(scope, safe_body)))


@tool
def pr_rerequest_review(
    reviewer: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Re-request one reviewer on the pull request bound to this turn."""
    scope = _scope(runtime, RepoPRAction.PR_REREQUEST)
    if _REVIEWER.fullmatch(reviewer) is None:
        raise ToolException("reviewer is invalid")
    _call(lambda: _client(scope).rerequest_review(scope, reviewer))
    return {"status": "review_rerequested", "reviewer": reviewer}


def _escalation_state_path() -> Path:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ToolException("unsupported operation escalation requires MIMIR_HOME")
    return Path(home).resolve() / "state" / "unsupported_operations.json"


def _emit_unsupported(scope: RepoPRActionScope, operation: str) -> bool:
    key = f"{scope.scope_id}:{operation}"
    state_path = _escalation_state_path()
    with _escalation_lock:
        try:
            known = set(json.loads(state_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            known = set()
        except (OSError, ValueError, TypeError) as exc:
            raise ToolException("unsupported operation escalation state is unreadable") from exc
        if key in known:
            return False

        from ..event_logger import log_durable_event_sync

        try:
            log_durable_event_sync(
                "unsupported_operation",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
                operation=operation,
                scope_id=scope.scope_id,
                operator_visible=True,
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(sorted(known | {key}), separators=(",", ":")),
                encoding="utf-8",
            )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(state_path)
            directory_fd = os.open(state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException("unsupported operation escalation could not be persisted") from exc
    return True


@tool
def unsupported_operation(
    operation: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Escalate a real bound-PR need not represented by the closed typed API."""
    scope = _scope(runtime, None)
    if _OPERATION.fullmatch(operation) is None:
        raise ToolException("operation must be a short stable operation name")
    emitted = _emit_unsupported(scope, operation)
    return {
        "status": "unsupported_operation",
        "escalated": emitted,
        "repository": scope.canonical_repo,
        "pull_request": scope.pr_number,
        "operation": operation,
    }


def _bind_injected_runtime(forge_tool: StructuredTool) -> StructuredTool:
    """Bind runtime before LangChain caches its raw callable annotation."""
    if forge_tool.func is None:
        raise RuntimeError(f"forge tool {forge_tool.name!r} has no sync callable")
    forge_tool.func.__annotations__["runtime"] = ToolRuntime
    forge_tool.args_schema = create_schema_from_function(
        forge_tool.name,
        forge_tool.func,
        filter_args=(),
        include_injected=True,
    )
    forge_tool.__dict__.pop("_injected_args_keys", None)
    return forge_tool


FORGE_TOOLS = tuple(_bind_injected_runtime(forge_tool) for forge_tool in (
    pr_metadata,
    pr_files,
    pr_diff,
    pr_checks,
    pr_reviews,
    pr_comments,
    pr_review_requests,
    pr_submit_review,
    pr_inline_review_comment,
    pr_comment,
    pr_rerequest_review,
    unsupported_operation,
))
