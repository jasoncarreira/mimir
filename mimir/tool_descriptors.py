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


_D = ToolDescriptor
_E = ResultOriginKind.EXTERNAL
_R = ResultOriginKind.REPOSITORY
_N = ResultOriginKind.NON_INGESTING

TOOL_DESCRIPTORS: Mapping[str, ToolDescriptor] = MappingProxyType({
    "Bash": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "Edit": _D(SinkCategory.FILE, _generic_target, "filesystem"),
    "Write": _D(SinkCategory.FILE, _generic_target, "filesystem"),
    "abandon_proposal": _D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=_N),
    "activity_panel_edit": _D(SinkCategory.HARNESS_DISPLAY, _generic_target, "message"),
    "activity_panel_post": _D(SinkCategory.HARNESS_DISPLAY, _generic_target, "message"),
    "add_schedule": _D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=_N),
    "adownload_files": _D(SinkCategory.FILE, _generic_target, "filesystem"),
    "aexecute": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "approve_declassification": _D(result_origin=_N),
    "bash": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "bash_async": _D(SinkCategory.SHELL_PROCESS, _command_target, "shell_process", result_origin=_N, ifc_delegation=True),
    "bash_exec": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "clear_ingest_taint": _D(result_origin=_N),
    "commitment_complete": _D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=_N),
    "commitment_dismiss": _D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=_N),
    "commitment_snooze": _D(SinkCategory.SAGA, _commitment_target, "commitments", result_origin=_N),
    "defer_injected_message": _D(SinkCategory.SAGA, _injected_message_target, "injected_messages", result_origin=_N),
    "download_files": _D(SinkCategory.FILE, _generic_target, "filesystem"),
    "edit_file": _D(SinkCategory.FILE, _file_target, "filesystem", result_origin=_N),
    "execute": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "fetch_channel_history": _D(sink_target_extractor=_channel_target),
    "fetch_url": _D(SinkCategory.NETWORK, _url_target, "network", FetchAuthorizationKind.FETCH_URL, _N),
    "hands_edit": _D(SinkCategory.EXTERNAL_MCP, _generic_target, "client_provider"),
    "hands_python": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "hands_request_scope": _D(result_origin=_N),
    "hands_shell": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "harness_auto_deliver": _D(SinkCategory.SAME_CHANNEL, _generic_target, "message"),
    "harness_resend_nudge": _D(SinkCategory.SAME_CHANNEL, _generic_target, "message"),
    "http_request": _D(SinkCategory.HTTP_WEBHOOK, _url_target, "network"),
    "issue_comment": _D(SinkCategory.FORGE, _generic_target, "configured_repository_issue"),
    "memory_store": _D(SinkCategory.SAGA, _generic_target, "saga", result_origin=_N),
    "ntfy_send": _D(SinkCategory.NOTIFICATION, _generic_target, "notification"),
    "open_proposal": _D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=_N),
    "operator_alert": _D(SinkCategory.NOTIFICATION, _operator_alert_target, "notification", result_origin=_N),
    "post_message": _D(SinkCategory.CROSS_CHANNEL, _generic_target, "message"),
    "pr_checks": _D(result_origin=_E | _R),
    "pr_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "pr_comments": _D(result_origin=_E | _R),
    "pr_diff": _D(result_origin=_E | _R),
    "pr_edit_body": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "pr_files": _D(result_origin=_E | _R),
    "pr_inline_review_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "pr_job_log": _D(result_origin=_E | _R),
    "pr_metadata": _D(result_origin=_E | _R),
    "pr_rerequest_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N),
    "pr_review_requests": _D(result_origin=_E | _R),
    "pr_reviews": _D(result_origin=_E | _R),
    "pr_submit_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "react": _D(SinkCategory.SAME_CHANNEL, _channel_target, "message", result_origin=_N, budget_exempt=True),
    "rebuild_index": _D(SinkCategory.FILE, _index_target, "filesystem", result_origin=_N),
    "reload_pollers": _D(SinkCategory.SCHEDULER, _fixed_target("scheduler:pollers"), "scheduler", result_origin=_N),
    "remove_schedule": _D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=_N),
    "repo_checkout": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "repo_cleanup": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N),
    "repo_commit": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_diff": _D(result_origin=_R, git_operation_result=True),
    "repo_fetch": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_merge": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_merge_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_push": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_rebase": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_rebase_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_revert": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_revert_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True),
    "repo_stage": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, git_operation_result=True),
    "repo_status": _D(result_origin=_R, git_operation_result=True),
    "repo_test": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R),
    "repo_unmerged": _D(result_origin=_R, git_operation_result=True),
    "request_mimir_update": _D(SinkCategory.FILE, _update_target, "filesystem", result_origin=_N),
    "request_operator_approval": _D(result_origin=_N),
    "saga_end_session": _D(SinkCategory.SAGA, _generic_target, "session_boundary", result_origin=_N),
    "saga_feedback": _D(SinkCategory.SAGA, _generic_target, "saga", result_origin=_N),
    "saga_forget": _D(SinkCategory.SAGA, _generic_target, "saga"),
    "saga_mark_contributions": _D(SinkCategory.SAGA, _generic_target, "saga", result_origin=_N),
    "saga_record_skill_learning": _D(SinkCategory.SAGA, _generic_target, "saga", result_origin=_N),
    "send_message": _D(SinkCategory.SAME_CHANNEL, _channel_target, "message", result_origin=_N, budget_exempt=True),
    "set_poller_overrides": _D(SinkCategory.SCHEDULER, _poller_overrides_target, "scheduler", result_origin=_N),
    "set_schedule_priority": _D(SinkCategory.SCHEDULER, _schedule_target, "scheduler", result_origin=_N),
    "shell": _D(SinkCategory.SHELL_PROCESS, _generic_target, "shell_process"),
    "shell_exec": _D(SinkCategory.SHELL_PROCESS, _command_target, "shell_process"),
    "spawn_open_code": _D(SinkCategory.SPAWN, _spawn_target, "spawn_process", ifc_delegation=True),
    "submit_proposal": _D(SinkCategory.PROPOSAL, _generic_target, "proposal", result_origin=_N),
    "task": _D(result_origin=_N, ifc_delegation=True),
    "unsupported_operation": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N),
    "web_search": _D(
        SinkCategory.NETWORK,
        _web_search_target,
        "network",
        FetchAuthorizationKind.WEB_SEARCH,
        _E | ResultOriginKind.WEB_SEARCH_FORMAT,
    ),
    "web_turn_events": _D(SinkCategory.SAME_CHANNEL, _generic_target),
    "webhook": _D(SinkCategory.HTTP_WEBHOOK, _url_target, "network"),
    "worklink_resume": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "worklink_run": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "write_file": _D(SinkCategory.FILE, _file_target, "filesystem", result_origin=_N),
    "write_todos": _D(result_origin=_N),
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
