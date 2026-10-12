"""Core tool declarations for the frozen policy registry."""

from __future__ import annotations

from functools import lru_cache
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import PolicyRegistry
    from mimir.access_control import DeclaredShellCommand


class CapabilityTier(StrEnum):
    """Blast-radius ceiling for authority declared by autonomous triggers."""

    SCOPE_CONTAINED = "scope-contained"
    SCOPED_WITH_PROVENANCE = "scoped-with-provenance"
    CODE_EXECUTION = "code-execution"
    UNBOUNDED = "unbounded"


@dataclass(frozen=True)
class ServiceSinkPolicy:
    """One executable, operation-specific service destination grant."""

    operation: str
    adapter: str
    destination: str


@dataclass(frozen=True)
class ServicePrincipal:
    """Trusted autonomous service principal with explicit capabilities and destinations."""

    canonical: str
    trigger: str
    capabilities: tuple[str, ...] = ()
    readable_domains: tuple[str, ...] = ()
    sink_destinations: tuple[str, ...] = ()
    sink_policies: tuple[ServiceSinkPolicy, ...] = ()
    filesystem_read_roots: tuple[str, ...] = ()
    owned_skill_directory: str | None = None
    channel_memory_directory: str | None = None
    saga_full_corpus_read: bool = False
    creation_path: str | None = None
    authority_profile: str | None = None
    capability_tier: CapabilityTier | None = None
    declared_shell_commands: tuple[DeclaredShellCommand, ...] = ()
    configured_delivery_channel: str | None = None
    proposal_surface: str = "wiki"

    def can_read_domain(self, domain: str) -> bool:
        return domain in self.readable_domains

    def can_write_sink(self, sink: str) -> bool:
        return sink in self.sink_destinations

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def sink_policy_for(self, operation: str) -> ServiceSinkPolicy | None:
        return next((policy for policy in self.sink_policies if policy.operation == operation), None)


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


_FLOWS = {
    "neither": _names("""approve_declassification request_operator_approval write_todos
        task hands_request_scope"""),
    "source": _names("""memory_query memory_get proposal_diff file_search mimir_get_turn
        get_turn bash_jobs_list bash_job_output fetch_channel_history list_channels
        list_schedules commitment_list read_file aread ls als glob aglob grep agrep
        hands_read Read Glob Grep pr_metadata pr_spec pr_list pr_files pr_diff
        pr_file_content pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs
        pr_reviews pr_comments pr_review_requests repo_status repo_diff
        repo_unmerged"""),
    "sink": _names("""memory_store memory_propose open_proposal submit_proposal
        abandon_proposal saga_feedback saga_mark_contributions saga_end_session
        saga_forget saga_record_skill_learning rebuild_index send_message
        operator_alert react defer_injected_message add_schedule
        set_schedule_priority remove_schedule set_poller_overrides reload_pollers
        commitment_complete commitment_snooze commitment_dismiss
        request_mimir_update post_message webhook ntfy_send write_file edit_file
        replace_file Write Edit harness_auto_deliver harness_resend_nudge
        activity_panel_post activity_panel_edit pr_submit_review
        pr_inline_review_comment pr_comment pr_edit_body issue_comment
        pr_rerequest_review unsupported_operation repo_cleanup repo_stage repo_commit
        repo_merge repo_merge_abort repo_rebase repo_rebase_abort repo_revert
        repo_revert_abort repo_push"""),
    "both": _names("""shell_exec bash_async worklink_run worklink_resume web_search
        fetch_url http_request spawn_open_code download_files adownload_files
        Bash bash bash_exec execute aexecute shell hands_edit hands_shell hands_python
        repo_checkout repo_fetch repo_test"""),
}


_TIERS = {
    "scope-contained": _names("""write_file edit_file replace_file rebuild_index shell_exec
        bash_async bash_jobs_list bash_job_output read_file proposal_diff aread ls als
        glob aglob grep agrep file_search get_turn mimir_get_turn memory_get
        send_message operator_alert task list_schedules pr_metadata pr_spec pr_list
        pr_files pr_diff pr_file_content pr_checks pr_job_log ci_run_jobs ci_run
        ci_recent_runs pr_reviews pr_comments pr_review_requests repo_status
        repo_diff repo_unmerged"""),
    "scoped-with-provenance": _names("""memory_store memory_propose open_proposal
        submit_proposal abandon_proposal saga_feedback saga_mark_contributions
        saga_end_session saga_record_skill_learning pr_review_others pr_submit_review
        pr_inline_review_comment pr_comment pr_edit_body issue_comment
        pr_rerequest_review unsupported_operation repo_checkout repo_cleanup repo_fetch
        repo_stage repo_commit repo_merge repo_merge_abort repo_rebase repo_rebase_abort
        repo_revert repo_revert_abort repo_push"""),
    "code-execution": _names("worklink_run worklink_resume spawn_open_code repo_test"),
    "unbounded": _names("fetch_url web_search webhook http_request ntfy_send"),
}
_PROFILES = {
    "research": _names("""write_file edit_file replace_file read_file aread ls als glob aglob
        grep agrep file_search memory_store memory_propose saga_feedback
        saga_mark_contributions send_message saga_record_skill_learning operator_alert
        shell_exec bash_jobs_list bash_job_output fetch_url open_proposal
        submit_proposal abandon_proposal"""),
    "github": _names("""worklink_run worklink_resume write_file edit_file replace_file
        shell_exec bash_async bash_jobs_list bash_job_output read_file aread ls als glob
        aglob grep agrep file_search get_turn mimir_get_turn send_message operator_alert
        task memory_store saga_mark_contributions saga_end_session
        saga_record_skill_learning fetch_url pr_metadata pr_spec pr_list pr_files
        pr_diff pr_file_content pr_checks pr_reviews pr_comments pr_review_requests
        pr_submit_review pr_inline_review_comment pr_comment pr_rerequest_review
        pr_edit_body pr_review_others pr_job_log ci_run_jobs ci_run ci_recent_runs
        issue_comment unsupported_operation repo_checkout repo_cleanup repo_fetch
        repo_status repo_test repo_diff repo_unmerged repo_stage repo_commit repo_merge
        repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort
        repo_push"""),
    "custom": frozenset.union(*_TIERS.values()) - _names(
        "issue_comment open_proposal submit_proposal abandon_proposal proposal_diff"
    ),
    "heartbeat": _names("""write_file edit_file replace_file shell_exec bash_async
        bash_jobs_list bash_job_output read_file aread ls als glob aglob grep agrep
        file_search get_turn mimir_get_turn memory_store saga_feedback
        saga_mark_contributions worklink_run send_message operator_alert fetch_url
        task list_schedules pr_metadata pr_spec pr_list pr_files pr_diff
        pr_file_content pr_checks pr_reviews pr_comments pr_review_requests
        pr_submit_review pr_inline_review_comment pr_comment pr_rerequest_review
        pr_edit_body unsupported_operation repo_checkout repo_cleanup repo_fetch
        repo_status repo_test repo_diff repo_unmerged repo_stage repo_commit repo_merge
        repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort
        repo_push"""),
    "session-boundary": _names(
        "memory_get mimir_get_turn saga_feedback saga_end_session write_file"
    ),
}
_BOUNDS = {name: _names("fetch_url") for name in ("research", "github", "session-boundary")}
_COMPANIONS = {
    "shell_exec": _names("bash_jobs_list bash_job_output"),
    "bash_async": _names("bash_jobs_list bash_job_output"),
}
_LEGACY_SINK_TIERS = {
    name: "scope-contained" for name in _names("open_proposal submit_proposal abandon_proposal")
}
_TRIGGERS = {
    "heartbeat": {
        "canonical": "heartbeat", "trigger": "scheduled_tick", "tier": "unbounded",
        "root_parts": ("state", "triggers", "heartbeat"),
        "channel_memory_directory": "scheduler:heartbeat",
        "creation_path": "mimir.scheduler.Scheduler._fire:heartbeat",
        "saga_full_corpus_read": True,
    },
    "session-boundary": {
        "canonical": "synthesis", "trigger": "saga_session_end",
        "tier": "scoped-with-provenance", "root_parts": None,
        "channel_memory_directory": None,
        "creation_path": "mimir.server._on_session_idle",
        "saga_full_corpus_read": True,
    },
}
_SHELL = {
    "github": "repo_review", "heartbeat": "maintenance",
    "session-boundary": "session_boundary",
}
_FETCH = {
    "heartbeat": ("approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS"),
    "github": ("github_pr_api", "GITHUB_REPOS"),
    "session-boundary": ("github_pr_api", "GITHUB_REPOS"),
}

_CHANNEL_OPERATIONS = _names("send_message react fetch_channel_history")
_WRITE_OPERATIONS = _names("write_file edit_file replace_file")
_WRITE_RESOURCE_OPERATIONS = _WRITE_OPERATIONS | _names("worklink_run worklink_resume")
_READ_RESOURCE_OPERATIONS = _names(
    "read_file aread ls als glob aglob grep agrep file_search get_turn mimir_get_turn"
)
_RESULT_DOMAINS = {
    "channel_history": _names("fetch_channel_history"),
    "channel_metadata": _names("list_channels"),
    "schedule_metadata": _names("list_schedules"),
    "shell_jobs": _names("bash_jobs_list bash_job_output"),
    "filesystem": _names("read_file proposal_diff aread ls als glob aglob grep agrep download_files adownload_files Read Glob Grep file_search"),
    "turn_history": _names("get_turn mimir_get_turn"),
    "saga": _names("memory_query memory_get saga_forget"),
    "commitments": _names("commitment_list"),
    "shell": _names("shell_exec execute"),
    "web": _names("web_search"),
    "worklink": _names("worklink_run worklink_resume"),
    "coding_worker": _names("spawn_open_code"),
    "repository": _names("""pr_metadata pr_spec pr_list pr_files pr_diff
        pr_file_content pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs
        pr_reviews pr_comments pr_review_requests repo_checkout repo_fetch
        repo_status repo_test repo_diff repo_unmerged pr_submit_review
        pr_inline_review_comment pr_comment pr_edit_body issue_comment
        repo_commit repo_merge repo_merge_abort repo_rebase repo_rebase_abort
        repo_revert repo_revert_abort repo_push"""),
    "client_provider": _names("hands_read hands_edit hands_shell hands_python"),
}
_READABLE_DOMAINS = {
    "repository": _names("""pr_list ci_run_jobs ci_run ci_recent_runs
        pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_job_log pr_reviews pr_comments pr_review_requests repo_checkout
        repo_fetch repo_status repo_test repo_diff repo_unmerged"""),
    "channel_metadata": _names("list_channels"),
    "schedule_metadata": _names("list_schedules"),
    "shell_jobs": _names("bash_jobs_list bash_job_output"),
    "filesystem": _names("read_file proposal_diff aread ls als glob aglob grep agrep file_search"),
    "turn_history": _names("get_turn mimir_get_turn"),
    "saga": _names("memory_query memory_get"),
    "client_provider": _names("hands_read hands_python"),
}
_READ_BACKEND = _names("""pr_job_log ci_run_jobs ci_run ci_recent_runs Read Glob
    Grep read_file proposal_diff aread ls als glob aglob grep agrep fetch_url
    pr_metadata pr_spec pr_list pr_files pr_diff pr_file_content pr_checks
    pr_reviews pr_comments pr_review_requests repo_checkout repo_fetch
    repo_status repo_test repo_diff repo_unmerged hands_read""")
_ACP_HANDS_RESULTS = _names("hands_read hands_edit hands_shell hands_python")
_CATALOG_OPEN = _names("""commitment_list memory_query memory_get web_search fetch_url
    write_todos defer_injected_message request_operator_approval commitment_complete
    commitment_snooze commitment_dismiss""")
_CATALOG_REPO_PR = _names("""pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_job_log pr_reviews pr_comments pr_review_requests pr_submit_review
    pr_inline_review_comment pr_comment pr_edit_body pr_rerequest_review
    unsupported_operation repo_checkout repo_cleanup repo_fetch repo_status repo_test
    repo_diff repo_unmerged repo_stage repo_commit repo_merge repo_merge_abort
    repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push""")
_CATALOG_RESOURCE = (_READ_RESOURCE_OPERATIONS | _WRITE_RESOURCE_OPERATIONS
                     | _CATALOG_REPO_PR
                     | _names("hands_read ci_run_jobs ci_run ci_recent_runs"))
_CATALOG_ADMIN = _names("""pr_list issue_comment operator_alert
    approve_declassification list_channels list_schedules add_schedule
    set_schedule_priority remove_schedule reload_pollers open_proposal proposal_diff
    submit_proposal abandon_proposal request_mimir_update shell_exec bash_async
    bash_jobs_list bash_job_output spawn_open_code task memory_store memory_propose
    saga_feedback saga_mark_contributions saga_end_session saga_record_skill_learning
    saga_forget set_poller_overrides download_files adownload_files rebuild_index
    hands_edit hands_shell hands_python hands_request_scope""")
_PROTECTED_METADATA = _names("list_channels list_schedules bash_jobs_list bash_job_output")
_CATALOG_ADMIN_BUILTIN = _names(
    "Bash bash bash_exec execute aexecute shell Write Edit Read Glob Grep download_files"
)


def register_core_tools(registry: PolicyRegistry) -> None:
    """Register core descriptors; later policy families add their own metadata here."""
    from mimir.tool_descriptors import _CORE_DESCRIPTOR_DECLARATIONS, ResultOriginKind

    flow_by_name = {
        name: flow for flow, names in _FLOWS.items() for name in names
    }
    if sum(map(len, _FLOWS.values())) != len(flow_by_name):
        raise ValueError("duplicate core tool flow classification")
    result_domains = {name: domain for domain, names in _RESULT_DOMAINS.items() for name in names}
    readable_domains = {name: domain for domain, names in _READABLE_DOMAINS.items() for name in names}
    if sum(map(len, _RESULT_DOMAINS.values())) != len(result_domains) or sum(map(len, _READABLE_DOMAINS.values())) != len(readable_domains):
        raise ValueError("duplicate core result or readable domain")

    for name, descriptor in _CORE_DESCRIPTOR_DECLARATIONS.items():
        operation_class = (
            "open" if name in _CATALOG_OPEN else
            "resource_scoped" if name in _CATALOG_RESOURCE else
            "admin_required" if name in _CATALOG_ADMIN else None
        )
        memberships = set()
        for member, names in (
            ("channel_operations", _CHANNEL_OPERATIONS),
            ("write_operations", _WRITE_OPERATIONS),
            ("write_resource_operations", _WRITE_RESOURCE_OPERATIONS),
            ("protected_metadata_operations", _PROTECTED_METADATA),
            ("catalog_admin_builtin", _CATALOG_ADMIN_BUILTIN),
            ("read_resource_operations", _READ_RESOURCE_OPERATIONS),
            ("acp_hands_result_tools", _ACP_HANDS_RESULTS),
        ):
            if name in names:
                memberships.add(member)
        registry.register_tool(
            name=name,
            policy_only=True,
            flow=flow_by_name.get(name),
            tier=next((tier for tier, names in _TIERS.items() if name in names), None),
            operation_class=operation_class,
            result_domain=result_domains.get(name),
            result_origin=descriptor.result_origin,
            sink_category=descriptor.sink_category,
            sink_destination=descriptor.sink_destination,
            sink_target_extractor=descriptor.sink_target_extractor,
            sink_payload_extractor=descriptor.sink_payload_extractor,
            fetch_authorization=descriptor.fetch_authorization,
            git_operation_result=descriptor.git_operation_result,
            ifc_delegation=descriptor.ifc_delegation,
            budget_exempt=descriptor.budget_exempt,
            writes_code_or_state=False,
            profiles=tuple(profile for profile, names in _PROFILES.items() if name in names),
            read_backend=name in _READ_BACKEND,
            readable_domain=readable_domains.get(name),
            memberships=memberships,
        )

    # Framework aliases and native operations without descriptors still require
    # an explicit IFC decision; they are not candidates for runtime factories.
    for name in sorted(flow_by_name.keys() - _CORE_DESCRIPTOR_DECLARATIONS.keys()):
        operation_class = (
            "open" if name in _CATALOG_OPEN else
            "resource_scoped" if name in _CATALOG_RESOURCE else
            "admin_required" if name in _CATALOG_ADMIN else None
        )
        memberships = set()
        if name in _CATALOG_ADMIN_BUILTIN:
            memberships.add("catalog_admin_builtin")
        if name in _PROTECTED_METADATA:
            memberships.add("protected_metadata_operations")
        if name in _READ_RESOURCE_OPERATIONS:
            memberships.add("read_resource_operations")
        if name in _ACP_HANDS_RESULTS:
            memberships.add("acp_hands_result_tools")
        registry.register_tool(
            name=name, policy_only=True, descriptor_registered=False,
            flow=flow_by_name[name], tier=None, operation_class=operation_class,
            result_domain=result_domains.get(name), result_origin=ResultOriginKind.NONE,
            sink_category=None, sink_destination=None,
            sink_target_extractor=None, sink_payload_extractor=None,
            writes_code_or_state=False, profiles=(), read_backend=name in _READ_BACKEND,
            readable_domain=readable_domains.get(name),
            memberships=memberships,
        )

    for tier, names in _TIERS.items():
        for name in names:
            registry.register_capability(
                name, tier=tier, companions=_COMPANIONS.get(name, ()),
                legacy_service_sink_tier=_LEGACY_SINK_TIERS.get(name),
            )
    for name, capabilities in _PROFILES.items():
        registry.register_authority_profile(
            name, capabilities=capabilities, bounded_capabilities=_BOUNDS.get(name, ()),
            capability_companions=(
                {"worklink_run": _names("read_file ls shell_exec")}
                if name == "heartbeat" else {}
            ),
            scheduler=name == "heartbeat", fetch_url_policy=_FETCH.get(name),
            pr_review_scope=name == "heartbeat",
            builtin_trigger_config=_TRIGGERS.get(name),
        )
    for profile, shell in _SHELL.items():
        registry.register_shell_profile_mapping(profile, shell)
    registry.register_operator_shell_profile("scheduler_read_only")
    register_core_principals(registry)


def register_core_principals(registry: PolicyRegistry) -> None:
    """Register static server principals; poller authorities are manifest instances."""
    registry.register_service_principal(ServicePrincipal(
        canonical="scheduler", trigger="scheduled_tick",
        capabilities=tuple("""send_message operator_alert shell_exec bash_async bash_jobs_list
            bash_job_output spawn_open_code task saga_forget write_file edit_file
            replace_file open_proposal submit_proposal abandon_proposal worklink_run
            read_file aread ls als glob aglob grep agrep file_search get_turn
            mimir_get_turn list_schedules""".split()),
        readable_domains=("configured_inputs", "filesystem", "turn_history", "shell_jobs", "schedule_metadata"),
        sink_destinations=("configured_channel", "message", "notification", "filesystem", "shell_process", "spawn_process", "proposal", "saga", "worklink"),
        sink_policies=(
            *(ServiceSinkPolicy(name, "static_service_write_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS")
              for name in ("write_file", "edit_file", "replace_file")),
            ServiceSinkPolicy("shell_exec", "shell_profile", "maintenance"),
            ServiceSinkPolicy("bash_async", "shell_profile", "maintenance"),
            ServiceSinkPolicy("spawn_open_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
            ServiceSinkPolicy("worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO"),
            ServiceSinkPolicy("operator_alert", "operator_alert", "MIMIR_OPERATOR_ALERT_CHANNEL"),
        ),
        saga_full_corpus_read=True, creation_path="mimir.scheduler.Scheduler._fire_job",
    ))
    registry.register_service_principal(ServicePrincipal(
        canonical="synthesis", trigger="saga_session_end",
        capabilities=tuple(sorted(registry.authority_profiles["session-boundary"])),
        readable_domains=("session", "saga", "turn_history"),
        sink_destinations=("filesystem", "session_boundary", "saga"),
        sink_policies=(ServiceSinkPolicy("write_file", "static_service_write_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),),
        saga_full_corpus_read=True, creation_path="mimir.server._on_session_idle",
        authority_profile="session-boundary", capability_tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
    ))
    registry.register_service_principal(ServicePrincipal(
        canonical="system", trigger="upgrade",
        capabilities=tuple("""shell_exec bash_async bash_jobs_list bash_job_output
            write_file edit_file replace_file open_proposal submit_proposal
            abandon_proposal proposal_diff add_schedule set_schedule_priority
            list_schedules read_file aread ls als glob aglob grep agrep send_message""".split()),
        readable_domains=("defaults", "proposal", "filesystem", "schedule_metadata", "shell_jobs"),
        sink_destinations=("operator_alert", "filesystem", "shell_process", "proposal", "scheduler", "message"),
        sink_policies=(
            *(ServiceSinkPolicy(name, "upgrade_proposals", "MIMIR_HOME/scratch/proposals")
              for name in ("write_file", "edit_file", "replace_file")),
            ServiceSinkPolicy("shell_exec", "shell_profile", "upgrade_workspace"),
            ServiceSinkPolicy("bash_async", "shell_profile", "upgrade_workspace"),
        ),
        creation_path="mimir.defaults_upgrade.enqueue_upgrade_prompt_turns",
    ))


@lru_cache(maxsize=1)
def core_policy_registry() -> PolicyRegistry:
    from .registry import PolicyRegistry

    registry = PolicyRegistry()
    register_core_tools(registry)
    return registry.freeze()
