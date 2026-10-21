"""Core tool declarations for the frozen policy registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Set
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from inspect import iscoroutinefunction, signature
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeVar

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
    github_activity: bool = False
    worklink_retained_checkout: bool = False
    github_ci_watch: bool = False

    def can_read_domain(self, domain: str) -> bool:
        return domain in self.readable_domains

    def can_write_sink(self, sink: str) -> bool:
        return sink in self.sink_destinations

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def sink_policy_for(self, operation: str) -> ServiceSinkPolicy | None:
        return next((policy for policy in self.sink_policies if policy.operation == operation), None)


_selected_policy: ContextVar[PolicyRegistry | None] = ContextVar(
    "selected_runtime_policy", default=None,
)
_V = TypeVar("_V")


def selected_policy_registry() -> PolicyRegistry:
    """Read context-local policy; constructing a Config never changes this."""
    policy = _selected_policy.get()
    return policy if policy is not None else core_policy_registry()


@contextmanager
def policy_scope(policy: PolicyRegistry | None = None) -> Iterator[PolicyRegistry]:
    """Select a frozen policy for one operation/turn, restoring it on every exit."""
    selected = policy if policy is not None else selected_policy_registry()
    if not selected.frozen:
        raise ValueError("runtime policy selection requires a frozen policy")
    token = _selected_policy.set(selected)
    try:
        yield selected
    finally:
        _selected_policy.reset(token)


def scoped_policy_call(select: Callable[..., Any]) -> Callable[..., Any]:
    """Scope a sync/async API using its bound arguments, resetting on every exit."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        call_signature = signature(function)
        if iscoroutinefunction(function):
            @wraps(function)
            async def async_call(*args: Any, **kwargs: Any) -> Any:
                with policy_scope(select(call_signature.bind(*args, **kwargs))):
                    return await function(*args, **kwargs)
            return async_call

        @wraps(function)
        def call(*args: Any, **kwargs: Any) -> Any:
            with policy_scope(select(call_signature.bind(*args, **kwargs))):
                return function(*args, **kwargs)
        return call
    return decorate


class PolicyMappingView(Mapping[str, _V]):
    """Read-only live projection, safe even when imported before selection."""

    def __init__(self, project: Callable[[PolicyRegistry], Mapping[str, _V]]) -> None:
        self._project = project

    def __getitem__(self, key: str) -> _V:
        return self._project(selected_policy_registry())[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._project(selected_policy_registry()))

    def __len__(self) -> int:
        return len(self._project(selected_policy_registry()))

    def items(self):
        return self._project(selected_policy_registry()).items()

    def keys(self):
        return self._project(selected_policy_registry()).keys()

    def values(self):
        return self._project(selected_policy_registry()).values()


class PolicySetView(Set[str]):
    """Context-local immutable set projection with ordinary set algebra."""

    def __init__(self, project: Callable[[PolicyRegistry], Any]) -> None:
        self._project = project

    def __contains__(self, value: object) -> bool:
        return value in self._project(selected_policy_registry())

    def __iter__(self) -> Iterator[str]:
        return iter(self._project(selected_policy_registry()))

    def __len__(self) -> int:
        return len(self._project(selected_policy_registry()))

    @classmethod
    def _from_iterable(cls, iterable):
        return frozenset(iterable)

    def union(self, *others):
        return frozenset(self).union(*others)

    def intersection(self, *others):
        return frozenset(self).intersection(*others)

    def difference(self, *others):
        return frozenset(self).difference(*others)


def policy_membership(name: str) -> PolicySetView:
    return PolicySetView(lambda policy: frozenset(
        tool_name for tool_name, tool in policy.tools.items() if name in tool.memberships
    ))


@dataclass(frozen=True)
class PollerProperties:
    """Reserved server semantics for an instance declared by the poller loader."""

    github_activity: bool = False
    worklink_retained_checkout: bool = False
    github_ci_watch: bool = False

    def principal_fields(self) -> dict[str, bool]:
        return {
            "github_activity": self.github_activity,
            "worklink_retained_checkout": self.worklink_retained_checkout,
            "github_ci_watch": self.github_ci_watch,
        }


def poller_properties(canonical: str) -> PollerProperties:
    return selected_policy_registry().poller_properties.get(canonical, PollerProperties())


def register_core_poller_properties(registry: PolicyRegistry) -> None:
    """Declare historical semantics once; manifests cannot mint these grants."""
    registry.register_poller_properties(
        "poller:github-activity", PollerProperties(github_activity=True),
    )
    registry.register_poller_properties(
        "poller:worklink-ready-queue", PollerProperties(worklink_retained_checkout=True),
    )
    registry.register_poller_properties(
        "poller:github-ci-watch", PollerProperties(github_ci_watch=True),
    )

    # All authorization consumers read properties, never canonical-name lists.


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


_FLOWS = {
    "neither": _names("""approve_declassification request_operator_approval write_todos
        task hands_request_scope"""),
    "source": _names("""memory_query memory_get proposal_diff file_search mimir_get_turn
        get_turn bash_jobs_list bash_job_output fetch_channel_history list_channels
        list_schedules commitment_list read_file aread ls als glob aglob grep agrep
        hands_read Read Glob Grep chainlink_issue_show"""),
    "sink": _names("""memory_store memory_propose open_proposal submit_proposal
        abandon_proposal saga_feedback saga_mark_contributions saga_end_session
        saga_forget saga_record_skill_learning rebuild_index send_message
        operator_alert react defer_injected_message add_schedule
        set_schedule_priority set_schedule_cron remove_schedule set_poller_overrides
        reload_pollers commitment_propose_closure commitment_complete
        commitment_snooze commitment_dismiss request_mimir_update post_message webhook ntfy_send write_file edit_file
        replace_file Write Edit harness_auto_deliver harness_resend_nudge
        activity_panel_post activity_panel_edit"""),
    "both": _names("""shell_exec bash_async web_search
        fetch_url http_request download_files adownload_files
        Bash bash bash_exec execute aexecute shell hands_edit hands_shell hands_python"""),
}


_TIERS = {
    "scope-contained": _names("""write_file edit_file replace_file rebuild_index shell_exec
        bash_async bash_jobs_list bash_job_output read_file proposal_diff aread ls als
        glob aglob grep agrep file_search get_turn mimir_get_turn memory_get
        send_message operator_alert task list_schedules chainlink_issue_show"""),
    "scoped-with-provenance": _names("""memory_store memory_propose
        commitment_propose_closure open_proposal submit_proposal abandon_proposal saga_feedback saga_mark_contributions
        saga_end_session saga_record_skill_learning"""),
    "unbounded": _names("fetch_url web_search webhook http_request ntfy_send"),
}
_PROFILES = {
    "research": _names("""write_file edit_file replace_file read_file aread ls als glob aglob
        grep agrep file_search memory_store memory_propose saga_feedback
        saga_mark_contributions send_message saga_record_skill_learning operator_alert
        shell_exec bash_jobs_list bash_job_output fetch_url open_proposal
        submit_proposal abandon_proposal"""),
    "github": _names("""chainlink_issue_show write_file edit_file replace_file
        shell_exec bash_async bash_jobs_list bash_job_output read_file aread ls als glob
        aglob grep agrep file_search get_turn mimir_get_turn send_message operator_alert
        task memory_store saga_mark_contributions saga_end_session
        saga_record_skill_learning fetch_url"""),
    "custom": frozenset.union(*_TIERS.values()) - _names(
        "issue_comment open_proposal submit_proposal abandon_proposal proposal_diff"
    ),
    "heartbeat": _names("""chainlink_issue_show write_file edit_file replace_file
        shell_exec bash_async bash_jobs_list bash_job_output read_file aread ls als glob aglob grep agrep
        file_search get_turn mimir_get_turn memory_store saga_feedback
        saga_mark_contributions send_message operator_alert fetch_url
        task list_schedules"""),
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
_WRITE_RESOURCE_OPERATIONS = _WRITE_OPERATIONS
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
    "chainlink": _names("chainlink_issue_show"),
    "shell": _names("shell_exec execute"),
    "web": _names("web_search"),
    "client_provider": _names("hands_read hands_edit hands_shell hands_python"),
}
_READABLE_DOMAINS = {
    "channel_metadata": _names("list_channels"),
    "schedule_metadata": _names("list_schedules"),
    "shell_jobs": _names("bash_jobs_list bash_job_output"),
    "filesystem": _names("read_file proposal_diff aread ls als glob aglob grep agrep file_search"),
    "turn_history": _names("get_turn mimir_get_turn"),
    "saga": _names("memory_query memory_get"),
    "client_provider": _names("hands_read hands_python"),
    "chainlink": _names("chainlink_issue_show"),
}
_READ_BACKEND = _names("""chainlink_issue_show Read Glob
    Grep read_file proposal_diff aread ls als glob aglob grep agrep fetch_url
    hands_read""")
_ACP_HANDS_RESULTS = _names("hands_read hands_edit hands_shell hands_python")
_CATALOG_OPEN = _names("""commitment_list memory_query memory_get web_search fetch_url
    write_todos defer_injected_message request_operator_approval commitment_complete
    commitment_snooze commitment_dismiss""")
_CATALOG_RESOURCE = (_READ_RESOURCE_OPERATIONS | _WRITE_RESOURCE_OPERATIONS
                     | _names("hands_read"))
_CATALOG_ADMIN = _names("""chainlink_issue_show operator_alert
    approve_declassification list_channels list_schedules add_schedule
    set_schedule_priority set_schedule_cron remove_schedule reload_pollers open_proposal proposal_diff
    submit_proposal abandon_proposal request_mimir_update shell_exec bash_async
    bash_jobs_list bash_job_output task memory_store memory_propose
    commitment_propose_closure saga_feedback saga_mark_contributions saga_end_session saga_record_skill_learning
    saga_forget set_poller_overrides download_files adownload_files rebuild_index
    hands_edit hands_shell hands_python hands_request_scope""")
_PROTECTED_METADATA = _names("list_channels list_schedules bash_jobs_list bash_job_output")
_FIXED_SERVICE_SINKS = _names("rebuild_index")
_TAINT_INDEPENDENT_EGRESS = _names("fetch_url web_search")
_SCHEDULE_WRITES = _names("add_schedule set_schedule_priority set_schedule_cron remove_schedule set_poller_overrides")
_TAINTED_WRITES = _names("""hands_edit hands_shell hands_python memory_store saga_feedback
    saga_mark_contributions saga_end_session saga_forget saga_record_skill_learning""")
_TAINTED_FILE_WRITES = _names("""write_file edit_file replace_file upload
    awrite_file aedit_file areplace_file aupload write edit replace awrite aedit areplace""")
_SAGA_MUTATIONS = _names("""memory_store saga_feedback saga_mark_contributions
    saga_end_session saga_record_skill_learning saga_forget""")
_CATALOG_ADMIN_BUILTIN = _names(
    "Bash bash bash_exec execute aexecute shell Write Edit Read Glob Grep download_files"
)
_REMEDIATION_EFFECT = _names("write_file edit_file replace_file")
_TYPED_ERROR_SIGNAL = _names("""bash_job_output fetch_url get_turn mimir_get_turn
    shell_exec web_search""")


# Mutation potential is descriptive metadata, not a replacement for historical
# target-aware vetoes (e.g. scratch file writes remain permitted).
_WRITES_CODE_OR_STATE = (
    _TAINTED_FILE_WRITES | _SAGA_MUTATIONS | _SCHEDULE_WRITES | _WRITE_RESOURCE_OPERATIONS
    | _names("""Write Edit memory_propose commitment_propose_closure open_proposal
        submit_proposal abandon_proposal rebuild_index reload_pollers
        commitment_complete commitment_snooze commitment_dismiss request_mimir_update
        shell_exec bash_async Bash bash bash_exec execute aexecute
        shell hands_edit hands_shell hands_python download_files adownload_files
        approve_declassification write_todos defer_injected_message task
        hands_request_scope send_message react
        post_message webhook ntfy_send operator_alert harness_auto_deliver
        harness_resend_nudge activity_panel_post activity_panel_edit""")
)


# The order is the historical model-bound tool order. A module/collection is
# resolved only when its surface is selected.
_RUNTIME_SURFACES = {
    "default": (
        ("mimir.tools.chainlink", "chainlink_issue_show"),
        ("mimir.tools.registry", "approve_declassification request_operator_approval"),
        ("mimir.tools.memory", "memory_query memory_get"),
        ("mimir.tools.store", "memory_store"),
        ("mimir.tools.memory_propose", "memory_propose"),
        ("mimir.tools.proposals", "open_proposal proposal_diff submit_proposal abandon_proposal"),
        ("mimir.tools.saga_ops", "saga_feedback saga_mark_contributions saga_end_session saga_forget saga_record_skill_learning"),
        ("mimir.tools.extra", "file_search rebuild_index mimir_get_turn get_turn shell_exec"),
        ("mimir.tools.shell_async", "bash_async bash_jobs_list bash_job_output"),
        ("mimir.tools.operator_alert", "operator_alert"),
        ("mimir.tools.registry", "send_message react fetch_channel_history list_channels defer_injected_message list_schedules add_schedule set_schedule_cron set_schedule_priority remove_schedule set_poller_overrides reload_pollers commitment_complete commitment_snooze commitment_dismiss commitment_list commitment_propose_closure request_mimir_update"),
        ("mimir.tools.client_provider:HANDS_TOOLS", "hands_read hands_edit hands_shell hands_python hands_request_scope"),
    ),
    "web_search": (("mimir.tools.web", "web_search"),),
    "fetch_url": (("mimir.tools.web", "fetch_url"),),
}


def core_runtime_names(surface: str) -> tuple[str, ...]:
    return tuple(name for _, names in _RUNTIME_SURFACES[surface] for name in names.split())


def _tool_factory(module_path: str, name: str):
    module_name, _, collection = module_path.partition(":")

    def materialize():
        module = import_module(module_name)
        if collection:
            return next(tool for tool in getattr(module, collection) if tool.name == name)
        return getattr(module, name)

    return materialize


def register_core_tools(
    registry: PolicyRegistry, *, selected_owners: frozenset[str] = frozenset(),
) -> None:
    """Register core tool policy and lazy runtime factories."""
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

    factories = {
        name: _tool_factory(module, name)
        for groups in _RUNTIME_SURFACES.values()
        for module, names in groups
        for name in names.split()
    }
    if len(factories) != sum(len(core_runtime_names(surface)) for surface in _RUNTIME_SURFACES):
        raise ValueError("duplicate core runtime factory")
    if not factories.keys() <= _CORE_DESCRIPTOR_DECLARATIONS.keys() | flow_by_name.keys():
        raise ValueError("core runtime factory has no policy declaration")

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
            ("fixed_service_sinks", _FIXED_SERVICE_SINKS),
            ("taint_independent_egress", _TAINT_INDEPENDENT_EGRESS),
            ("schedule_writes", _SCHEDULE_WRITES),
            ("tainted_writes", _TAINTED_WRITES),
            ("tainted_file_writes", _TAINTED_FILE_WRITES),
            ("saga_mutations", _SAGA_MUTATIONS),
            ("remediation_effect", _REMEDIATION_EFFECT),
            ("typed_error_signal", _TYPED_ERROR_SIGNAL),
        ):
            if name in names:
                memberships.add(member)
        registry.register_tool(
            name=name,
            factory=factories.get(name),
            policy_only=name not in factories,
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
            writes_code_or_state=name in _WRITES_CODE_OR_STATE,
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
        for member, names in (
            ("fixed_service_sinks", _FIXED_SERVICE_SINKS),
            ("taint_independent_egress", _TAINT_INDEPENDENT_EGRESS),
            ("schedule_writes", _SCHEDULE_WRITES),
            ("tainted_writes", _TAINTED_WRITES),
            ("tainted_file_writes", _TAINTED_FILE_WRITES),
            ("saga_mutations", _SAGA_MUTATIONS),
            ("remediation_effect", _REMEDIATION_EFFECT),
            ("typed_error_signal", _TYPED_ERROR_SIGNAL),
        ):
            if name in names:
                memberships.add(member)
        registry.register_tool(
            name=name, factory=factories.get(name), policy_only=name not in factories,
            descriptor_registered=False,
            flow=flow_by_name[name], tier=None, operation_class=operation_class,
            result_domain=result_domains.get(name), result_origin=ResultOriginKind.NONE,
            sink_category=None, sink_destination=None,
            sink_target_extractor=None, sink_payload_extractor=None,
            writes_code_or_state=name in _WRITES_CODE_OR_STATE, profiles=(), read_backend=name in _READ_BACKEND,
            readable_domain=readable_domains.get(name),
            memberships=memberships,
        )

    # Native file-write aliases have no core descriptor or flow declaration, but
    # must still participate in the same target-aware live-ingest veto.
    for name in sorted(_TAINTED_FILE_WRITES - registry.tools.keys()):
        registry.register_tool(
            name=name, policy_only=True, descriptor_registered=False,
            flow=None, tier=None, operation_class=None, result_domain=None,
            result_origin=ResultOriginKind.NONE, sink_category=None,
            sink_destination=None, sink_target_extractor=None,
            sink_payload_extractor=None, writes_code_or_state=name in _WRITES_CODE_OR_STATE,
            profiles=(), read_backend=False, memberships={"tainted_file_writes"},
        )

    for tier, names in _TIERS.items():
        for name in names:
            registry.register_capability(
                name, tier=tier, companions=_COMPANIONS.get(name, ()),
                legacy_service_sink_tier=_LEGACY_SINK_TIERS.get(name),
            )
    for name, capabilities in _PROFILES.items():
        registry.register_authority_profile(
            name, capabilities=capabilities,
            bounded_capabilities=_BOUNDS.get(name, ()),
            capability_companions={},
            scheduler=name == "heartbeat", fetch_url_policy=_FETCH.get(name),
            pr_review_scope=name == "heartbeat",
            builtin_trigger_config=_TRIGGERS.get(name),
        )
    for profile, shell in _SHELL.items():
        registry.register_shell_profile_mapping(profile, shell)
    registry.register_operator_shell_profile("scheduler_read_only")
    register_core_principals(registry)
    register_core_poller_properties(registry)


def register_core_principals(registry: PolicyRegistry) -> None:
    """Register static server principals; poller authorities are manifest instances."""
    registry.register_service_principal(ServicePrincipal(
        canonical="scheduler", trigger="scheduled_tick",
        capabilities=tuple("""send_message operator_alert shell_exec bash_async bash_jobs_list
             bash_job_output task saga_forget write_file edit_file
             replace_file open_proposal submit_proposal abandon_proposal
            read_file aread ls als glob aglob grep agrep file_search get_turn
            mimir_get_turn list_schedules""".split()),
        readable_domains=("configured_inputs", "filesystem", "turn_history", "shell_jobs", "schedule_metadata"),
        sink_destinations=("configured_channel", "message", "notification", "filesystem", "shell_process", "proposal", "saga"),
        sink_policies=(
            *(ServiceSinkPolicy(name, "static_service_write_roots", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS")
              for name in ("write_file", "edit_file", "replace_file")),
            ServiceSinkPolicy("shell_exec", "shell_profile", "maintenance"),
            ServiceSinkPolicy("bash_async", "shell_profile", "maintenance"),
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
            set_schedule_cron list_schedules read_file aread ls als glob aglob grep agrep send_message""".split()),
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
