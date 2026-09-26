from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mimir.tool_descriptors import (
    FetchAuthorizationKind,
    ResultOriginKind,
    SinkCategory,
    TOOL_DESCRIPTORS,
    ToolDescriptor,
    validate_tool_descriptors,
)


_OLD_SINK_CATEGORIES = {
    SinkCategory.SAME_CHANNEL: {
        "send_message", "react", "harness_auto_deliver", "harness_resend_nudge",
        "web_turn_events",
    },
    SinkCategory.CROSS_CHANNEL: {"post_message"},
    SinkCategory.HARNESS_DISPLAY: {"activity_panel_post", "activity_panel_edit"},
    SinkCategory.HTTP_WEBHOOK: {"webhook", "http_request"},
    SinkCategory.NETWORK: {"fetch_url", "web_search"},
    SinkCategory.SHELL_PROCESS: {
        "shell_exec", "bash_async", "Bash", "bash", "bash_exec", "execute",
        "aexecute", "shell", "hands_shell", "hands_python",
    },
    SinkCategory.EXTERNAL_MCP: {"hands_edit"},
    SinkCategory.SPAWN: {"spawn_open_code", "worklink_run", "worklink_resume"},
    SinkCategory.NOTIFICATION: {"operator_alert", "ntfy_send"},
    SinkCategory.FILE: {
        "write_file", "edit_file", "Write", "Edit", "download_files",
        "adownload_files", "rebuild_index", "request_mimir_update",
    },
    SinkCategory.SAGA: {
        "memory_store", "saga_record_skill_learning", "saga_feedback",
        "saga_mark_contributions", "saga_forget", "saga_end_session",
        "commitment_complete", "commitment_snooze", "commitment_dismiss",
        "defer_injected_message",
    },
    SinkCategory.SCHEDULER: {
        "add_schedule", "set_schedule_priority", "remove_schedule",
        "set_poller_overrides", "reload_pollers",
    },
    SinkCategory.PROPOSAL: {"open_proposal", "submit_proposal", "abandon_proposal"},
    SinkCategory.FORGE: {
        "pr_submit_review", "pr_inline_review_comment", "pr_comment",
        "pr_edit_body", "issue_comment", "pr_rerequest_review",
        "unsupported_operation", "repo_checkout", "repo_cleanup", "repo_fetch",
        "repo_test", "repo_stage", "repo_commit", "repo_merge",
        "repo_merge_abort", "repo_rebase", "repo_rebase_abort", "repo_revert",
        "repo_revert_abort", "repo_push",
    },
}

_OLD_SINK_DESTINATIONS = {
    "filesystem": {
        "write_file", "edit_file", "rebuild_index", "request_mimir_update",
        "download_files", "adownload_files", "Write", "Edit",
    },
    "shell_process": {
        "shell_exec", "bash_async", "Bash", "bash", "bash_exec", "execute",
        "aexecute", "shell", "hands_shell", "hands_python",
    },
    "spawn_process": {"spawn_open_code"},
    "proposal": {"open_proposal", "submit_proposal", "abandon_proposal"},
    "scheduler": {
        "add_schedule", "set_schedule_priority", "remove_schedule",
        "set_poller_overrides", "reload_pollers",
    },
    "commitments": {"commitment_complete", "commitment_snooze", "commitment_dismiss"},
    "injected_messages": {"defer_injected_message"},
    "saga": {
        "saga_feedback", "saga_mark_contributions", "saga_record_skill_learning",
        "saga_forget", "memory_store",
    },
    "session_boundary": {"saga_end_session"},
    "message": {
        "send_message", "react", "post_message", "harness_auto_deliver",
        "harness_resend_nudge", "activity_panel_post", "activity_panel_edit",
    },
    "notification": {"operator_alert", "ntfy_send"},
    "worklink": {"worklink_run", "worklink_resume"},
    "network": {"web_search", "fetch_url", "webhook", "http_request"},
    "client_provider": {"hands_edit"},
    "bound_pull_request": {
        "pr_submit_review", "pr_inline_review_comment", "pr_comment",
        "pr_edit_body", "pr_rerequest_review", "unsupported_operation",
        "repo_checkout", "repo_cleanup", "repo_fetch", "repo_test", "repo_stage",
        "repo_commit", "repo_merge", "repo_merge_abort", "repo_rebase",
        "repo_rebase_abort", "repo_revert", "repo_revert_abort", "repo_push",
    },
    "configured_repository_issue": {"issue_comment"},
}

_OLD_NON_INGESTING = {
    "hands_request_scope", "approve_declassification", "clear_ingest_taint",
    "request_operator_approval", "memory_store", "open_proposal",
    "submit_proposal", "abandon_proposal", "saga_feedback",
    "saga_mark_contributions", "saga_end_session", "saga_record_skill_learning",
    "rebuild_index", "bash_async", "fetch_url", "operator_alert", "send_message",
    "react", "defer_injected_message", "add_schedule", "set_schedule_priority",
    "remove_schedule", "set_poller_overrides", "reload_pollers",
    "commitment_complete", "commitment_snooze", "commitment_dismiss",
    "request_mimir_update", "pr_rerequest_review", "unsupported_operation",
    "repo_cleanup", "repo_stage", "write_todos", "write_file", "edit_file", "task",
}
_OLD_REPOSITORY_RESULTS = {
    "pr_job_log", "pr_metadata", "pr_files", "pr_diff", "pr_checks", "pr_reviews",
    "pr_comments", "pr_review_requests", "repo_checkout", "repo_fetch",
    "repo_status", "repo_test", "repo_diff", "repo_unmerged", "pr_submit_review",
    "pr_inline_review_comment", "pr_comment", "pr_edit_body", "repo_commit",
    "repo_merge", "repo_merge_abort", "repo_rebase", "repo_rebase_abort",
    "repo_revert", "repo_revert_abort", "repo_push",
}
_OLD_EXTERNAL_RESULTS = {
    "web_search", "pr_job_log", "pr_metadata", "pr_files", "pr_diff", "pr_checks",
    "pr_reviews", "pr_comments", "pr_review_requests",
}
_OLD_GIT_RESULTS = {
    "repo_fetch", "repo_status", "repo_diff", "repo_unmerged", "repo_stage",
    "repo_commit", "repo_merge", "repo_merge_abort", "repo_rebase",
    "repo_rebase_abort", "repo_revert", "repo_revert_abort", "repo_push",
}
_OLD_IFC_DELEGATION = {"task", "spawn_open_code", "bash_async"}
_OLD_BUDGET_EXEMPT = {"send_message", "react"}
_OLD_FETCH_AUTHORIZATION = {
    "fetch_url": FetchAuthorizationKind.FETCH_URL,
    "web_search": FetchAuthorizationKind.WEB_SEARCH,
}

_OLD_SPECIAL_EXTRACTORS = {
    "operator_alert": "_operator_alert_target",
    "send_message": "_channel_target",
    "react": "_channel_target",
    "fetch_channel_history": "_channel_target",
    "write_file": "_file_target",
    "edit_file": "_file_target",
    "shell_exec": "_command_target",
    "bash_async": "_command_target",
    "spawn_open_code": "_spawn_target",
    "worklink_run": "_worklink_target",
    "worklink_resume": "_worklink_target",
    "fetch_url": "_url_target",
    "http_request": "_url_target",
    "webhook": "_url_target",
    "web_search": "_web_search_target",
    "add_schedule": "_schedule_target",
    "set_schedule_priority": "_schedule_target",
    "remove_schedule": "_schedule_target",
    "set_poller_overrides": "_poller_overrides_target",
    "reload_pollers": "extract",
    "commitment_complete": "_commitment_target",
    "commitment_snooze": "_commitment_target",
    "commitment_dismiss": "_commitment_target",
    "defer_injected_message": "_injected_message_target",
    "request_mimir_update": "_update_target",
    "rebuild_index": "_index_target",
    **{
        name: "_repo_pr_target"
        for name in {
            "pr_submit_review", "pr_inline_review_comment", "pr_comment",
            "pr_edit_body", "pr_rerequest_review", "unsupported_operation",
            "repo_checkout", "repo_cleanup", "repo_fetch", "repo_test", "repo_stage",
            "repo_commit", "repo_merge", "repo_merge_abort", "repo_rebase",
            "repo_rebase_abort", "repo_revert", "repo_revert_abort", "repo_push",
        }
    },
}


def test_descriptors_are_equivalent_to_all_pre_migration_policy_tables() -> None:
    old_categories = {
        name: category
        for category, names in _OLD_SINK_CATEGORIES.items()
        for name in names
    }
    old_destinations = {
        name: destination
        for destination, names in _OLD_SINK_DESTINATIONS.items()
        for name in names
    }
    known_names = set().union(
        old_categories,
        old_destinations,
        _OLD_NON_INGESTING,
        _OLD_REPOSITORY_RESULTS,
        _OLD_EXTERNAL_RESULTS,
        _OLD_GIT_RESULTS,
        _OLD_IFC_DELEGATION,
        _OLD_BUDGET_EXEMPT,
        _OLD_FETCH_AUTHORIZATION,
        _OLD_SPECIAL_EXTRACTORS,
        {"fetch_channel_history"},
    )

    assert set(TOOL_DESCRIPTORS) == known_names
    for name in sorted(known_names):
        descriptor = TOOL_DESCRIPTORS[name]
        expected_origin = ResultOriginKind.NONE
        if name in _OLD_EXTERNAL_RESULTS:
            expected_origin |= ResultOriginKind.EXTERNAL
        if name in _OLD_REPOSITORY_RESULTS:
            expected_origin |= ResultOriginKind.REPOSITORY
        if name in _OLD_NON_INGESTING:
            expected_origin |= ResultOriginKind.NON_INGESTING
        if name == "web_search":
            expected_origin |= ResultOriginKind.WEB_SEARCH_FORMAT

        extractor = descriptor.sink_target_extractor
        expected_extractor = _OLD_SPECIAL_EXTRACTORS.get(name)
        if expected_extractor is None and name in old_categories:
            expected_extractor = "_generic_target"

        assert descriptor.sink_category is old_categories.get(name), name
        assert descriptor.sink_destination == old_destinations.get(name), name
        assert descriptor.fetch_authorization is _OLD_FETCH_AUTHORIZATION.get(
            name, FetchAuthorizationKind.NONE
        ), name
        assert descriptor.result_origin == expected_origin, name
        assert descriptor.git_operation_result is (name in _OLD_GIT_RESULTS), name
        assert descriptor.ifc_delegation is (name in _OLD_IFC_DELEGATION), name
        assert descriptor.budget_exempt is (name in _OLD_BUDGET_EXEMPT), name
        assert (extractor.__name__ if callable(extractor) else None) == expected_extractor, name


def test_sink_category_requires_declared_target_extractor() -> None:
    with pytest.raises(ValueError, match="category_only"):
        validate_tool_descriptors({
            "category_only": ToolDescriptor(sink_category=SinkCategory.FILE),
        })

    validate_tool_descriptors({
        "intentionally_targetless": ToolDescriptor(
            sink_category=SinkCategory.FILE,
            sink_target_extractor=None,
        ),
    })


def test_external_sink_requires_declared_payload_extractor() -> None:
    with pytest.raises(ValueError, match="payload extractors.*external"):
        validate_tool_descriptors({
            "external": ToolDescriptor(
                sink_category=SinkCategory.NETWORK,
                sink_target_extractor=None,
            ),
        })

    validate_tool_descriptors({
        "intentionally_payloadless": ToolDescriptor(
            sink_category=SinkCategory.NETWORK,
            sink_target_extractor=None,
            sink_payload_extractor=None,
        ),
    })


def test_tool_descriptors_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        TOOL_DESCRIPTORS["send_message"].budget_exempt = False
    with pytest.raises(TypeError):
        TOOL_DESCRIPTORS["new_tool"] = ToolDescriptor()
