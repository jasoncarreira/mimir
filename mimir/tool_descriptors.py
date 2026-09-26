"""Immutable authorization and information-flow facts for tools."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntFlag, StrEnum, auto
from pathlib import Path
from types import MappingProxyType
from typing import Any


class SinkCategory(StrEnum):
    """Information-flow sink categories."""

    SAME_CHANNEL = "same_channel"
    CROSS_CHANNEL = "cross_channel"
    PUBLIC = "public"
    EXTERNAL_MCP = "external_mcp"
    HTTP_WEBHOOK = "http_webhook"
    SHELL_PROCESS = "shell_process"
    NETWORK = "network"
    SPAWN = "spawn"
    NOTIFICATION = "notification"
    FILE = "file"
    DIRECT_MESSAGE = "direct_message"
    SAGA = "saga"
    SCHEDULER = "scheduler"
    PROPOSAL = "proposal"
    FORGE = "forge"
    HARNESS_DISPLAY = "harness_display"
    UNKNOWN = "unknown"


class FetchAuthorizationKind(StrEnum):
    NONE = "none"
    FETCH_URL = "fetch_url"
    WEB_SEARCH = "web_search"


class ResultOriginKind(IntFlag):
    NONE = 0
    EXTERNAL = auto()
    REPOSITORY = auto()
    NON_INGESTING = auto()
    WEB_SEARCH_FORMAT = auto()


SinkTargetExtractor = Callable[
    [str, Mapping[str, Any], Any | None], tuple[str | None, ...]
]
_TARGET_EXTRACTOR_UNDECLARED = object()


@dataclass(frozen=True)
class ToolDescriptor:
    sink_category: SinkCategory | None = None
    sink_target_extractor: SinkTargetExtractor | None | object = (
        _TARGET_EXTRACTOR_UNDECLARED
    )
    sink_destination: str | None = None
    fetch_authorization: FetchAuthorizationKind = FetchAuthorizationKind.NONE
    result_origin: ResultOriginKind = ResultOriginKind.NONE
    git_operation_result: bool = False
    ifc_delegation: bool = False
    budget_exempt: bool = False


def _value(value: Any) -> str | None:
    return str(value) if value else None


def _generic_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (_value(arguments.get("target") or arguments.get("destination")),)


def _repo_pr_target(
    tool_name: str, arguments: Mapping[str, Any], auth_context: Any | None,
) -> tuple[str | None, ...]:
    discovered = getattr(auth_context, "server_discovered_pr_states", None)
    repository = arguments.get("repository")
    pull_request = arguments.get("pull_request")
    state = (
        discovered.resolve_for_tool(tool_name, repository, pull_request)
        if discovered is not None
        and isinstance(repository, str)
        and isinstance(pull_request, int)
        else None
    )
    if state is None:
        registry = getattr(auth_context, "repo_pr_scope_registry", None)
        state = (
            registry.resolve(repository, pull_request)
            if registry is not None and hasattr(registry, "resolve")
            else None
        )
    if state is None:
        return (None,)
    scope = state.action_scope
    return (
        f"{scope.canonical_repo}#pull/{scope.pr_number}"
        f"@{scope.observed_head_sha}:{scope.scope_id}",
    )


def _operator_alert_target(
    _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    from .channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel

    return (
        resolve_deliver_channel(
            OPERATOR_CHANNEL_SENTINEL,
            os.environ.get("MIMIR_OPERATOR_ALERT_CHANNEL", ""),
        ),
    )


def _channel_target(
    _tool_name: str, arguments: Mapping[str, Any], auth_context: Any | None,
) -> tuple[str | None, ...]:
    explicit = arguments.get("channel_id")
    return (_value(explicit) if explicit else getattr(auth_context, "channel_id", None),)


def _file_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (_value(arguments.get("file_path") or arguments.get("path")),)


def _command_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (_value(arguments.get("command")),)


def _spawn_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    target = _value(arguments.get("cwd") or os.environ.get("MIMIR_HOME"))
    artifact_root = _value(arguments.get("artifact_root"))
    return (target, artifact_root) if artifact_root else (target,)


def _worklink_target(
    _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (_value(os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")),)


def _url_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (_value(arguments.get("url")),)


def _web_search_target(
    _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    from .tools.web_search_destination import web_search_url

    return (web_search_url(),)


def _schedule_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    name = str(arguments.get("name") or "").strip()
    return (f"scheduler:job:{name}" if name else "scheduler:jobs",)


def _poller_overrides_target(
    _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    return (str(Path(home) / "pollers-overrides.yaml") if home else "scheduler:poller-overrides",)


def _fixed_target(value: str) -> SinkTargetExtractor:
    def extract(
        _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
    ) -> tuple[str | None, ...]:
        return (value,)

    return extract


def _commitment_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    commitment_id = str(arguments.get("commitment_id") or "").strip()
    return (f"commitment:{commitment_id}" if commitment_id else "commitments",)


def _injected_message_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    message_id = str(arguments.get("message_id") or "").strip()
    return (f"injected-message:{message_id}" if message_id else "injected_messages",)


def _update_target(
    _tool_name: str, _arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    return (str(Path(home) / ".mimir" / "pending-update.flag") if home else "pending-update.flag",)


def _index_target(
    _tool_name: str, arguments: Mapping[str, Any], _auth_context: Any | None,
) -> tuple[str | None, ...]:
    return (f"index:{str(arguments.get('scope') or 'all').strip().lower()}",)


D = ToolDescriptor
E = ResultOriginKind.EXTERNAL
R = ResultOriginKind.REPOSITORY
N = ResultOriginKind.NON_INGESTING

TOOL_DESCRIPTORS: Mapping[str, ToolDescriptor] = MappingProxyType({
    "Bash": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "Edit": D(SinkCategory.FILE, _generic_target, "filesystem"),
    "Write": D(SinkCategory.FILE, _generic_target, "filesystem"),
    "abandon_proposal": D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=N),
    "activity_panel_edit": D(SinkCategory.HARNESS_DISPLAY, _generic_target, "message"),
    "activity_panel_post": D(SinkCategory.HARNESS_DISPLAY, _generic_target, "message"),
    "add_schedule": D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=N),
    "adownload_files": D(SinkCategory.FILE, _generic_target, "filesystem"),
    "aexecute": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "approve_declassification": D(result_origin=N),
    "bash": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "bash_async": D(SinkCategory.SHELL_PROCESS, _command_target, "shell_process", result_origin=N, ifc_delegation=True),
    "bash_exec": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "clear_ingest_taint": D(result_origin=N),
    "commitment_complete": D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=N),
    "commitment_dismiss": D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=N),
    "commitment_snooze": D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=N),
    "defer_injected_message": D(SinkCategory.SAGA, _injected_message_target, "injected_messages", result_origin=N),
    "download_files": D(SinkCategory.FILE, _generic_target, "filesystem"),
    "edit_file": D(SinkCategory.FILE, _file_target, "filesystem", result_origin=N),
    "execute": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "fetch_channel_history": D(sink_target_extractor=_channel_target),
    "fetch_url": D(SinkCategory.NETWORK, _url_target, "network", FetchAuthorizationKind.FETCH_URL, N),
    "hands_edit": D(SinkCategory.EXTERNAL_MCP, _generic_target, "client_provider"),
    "hands_python": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "hands_request_scope": D(result_origin=N),
    "hands_shell": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "harness_auto_deliver": D(SinkCategory.SAME_CHANNEL, _generic_target, "message"),
    "harness_resend_nudge": D(SinkCategory.SAME_CHANNEL, _generic_target, "message"),
    "http_request": D(SinkCategory.HTTP_WEBHOOK, _url_target, "network"),
    "issue_comment": D(SinkCategory.FORGE, _generic_target, "configured_repository_issue"),
    "memory_store": D(SinkCategory.SAGA, _generic_target, "saga", result_origin=N),
    "ntfy_send": D(SinkCategory.NOTIFICATION, _generic_target, "notification"),
    "open_proposal": D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=N),
    "operator_alert": D(SinkCategory.NOTIFICATION, _operator_alert_target, "notification", result_origin=N),
    "post_message": D(SinkCategory.CROSS_CHANNEL, _generic_target, "message"),
    "pr_checks": D(result_origin=E | R),
    "pr_comment": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "pr_comments": D(result_origin=E | R),
    "pr_diff": D(result_origin=E | R),
    "pr_edit_body": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "pr_files": D(result_origin=E | R),
    "pr_inline_review_comment": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "pr_job_log": D(result_origin=E | R),
    "pr_metadata": D(result_origin=E | R),
    "pr_rerequest_review": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=N),
    "pr_review_requests": D(result_origin=E | R),
    "pr_reviews": D(result_origin=E | R),
    "pr_submit_review": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "react": D(SinkCategory.SAME_CHANNEL, _channel_target, "message", result_origin=N, budget_exempt=True),
    "rebuild_index": D(SinkCategory.FILE, _index_target, "filesystem", result_origin=N),
    "reload_pollers": D(SinkCategory.SCHEDULER, _fixed_target("scheduler:pollers"), "scheduler", result_origin=N),
    "remove_schedule": D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=N),
    "repo_checkout": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "repo_cleanup": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=N),
    "repo_commit": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_diff": D(result_origin=R, git_operation_result=True),
    "repo_fetch": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_merge": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_merge_abort": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_push": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_rebase": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_rebase_abort": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_revert": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_revert_abort": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R, git_operation_result=True),
    "repo_stage": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=N, git_operation_result=True),
    "repo_status": D(result_origin=R, git_operation_result=True),
    "repo_test": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=R),
    "repo_unmerged": D(result_origin=R, git_operation_result=True),
    "request_mimir_update": D(SinkCategory.FILE, _update_target, "filesystem", result_origin=N),
    "request_operator_approval": D(result_origin=N),
    "saga_end_session": D(SinkCategory.SAGA, _generic_target, "session_boundary", result_origin=N),
    "saga_feedback": D(SinkCategory.SAGA, _generic_target, "saga", result_origin=N),
    "saga_forget": D(SinkCategory.SAGA, _generic_target, "saga"),
    "saga_mark_contributions": D(SinkCategory.SAGA, _generic_target, "saga", result_origin=N),
    "saga_record_skill_learning": D(SinkCategory.SAGA, _generic_target, "saga", result_origin=N),
    "send_message": D(SinkCategory.SAME_CHANNEL, _channel_target, "message", result_origin=N, budget_exempt=True),
    "set_poller_overrides": D(SinkCategory.SCHEDULER, _poller_overrides_target, "scheduler", result_origin=N),
    "set_schedule_priority": D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=N),
    "shell": D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "shell_exec": D(SinkCategory.SHELL_PROCESS, _command_target, "shell_process"),
    "spawn_open_code": D(SinkCategory.SPAWN, _spawn_target, "spawn_process", ifc_delegation=True),
    "submit_proposal": D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=N),
    "task": D(result_origin=N, ifc_delegation=True),
    "unsupported_operation": D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=N),
    "web_search": D(
        SinkCategory.NETWORK,
        _web_search_target,
        "network",
        FetchAuthorizationKind.WEB_SEARCH,
        E | ResultOriginKind.WEB_SEARCH_FORMAT,
    ),
    "web_turn_events": D(SinkCategory.SAME_CHANNEL, _generic_target),
    "webhook": D(SinkCategory.HTTP_WEBHOOK, _url_target, "network"),
    "worklink_resume": D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "worklink_run": D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "write_file": D(SinkCategory.FILE, _file_target, "filesystem", result_origin=N),
    "write_todos": D(result_origin=N),
})


def get_tool_descriptor(tool_name: str) -> ToolDescriptor | None:
    return TOOL_DESCRIPTORS.get(tool_name)


def validate_tool_descriptors(
    descriptors: Mapping[str, ToolDescriptor] = TOOL_DESCRIPTORS,
) -> None:
    """Reject categorized sinks whose target extraction was not considered."""
    missing = sorted(
        name
        for name, descriptor in descriptors.items()
        if descriptor.sink_category is not None
        and descriptor.sink_target_extractor is _TARGET_EXTRACTOR_UNDECLARED
    )
    if missing:
        raise ValueError(
            "sink categories without declared target extractors: " + ", ".join(missing)
        )


validate_tool_descriptors()
