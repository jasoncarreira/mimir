"""Frozen declarations used to assemble core and enabled-plugin tool policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import PluginCandidate
    from mimir.tool_descriptors import (
        FetchAuthorizationKind, ResultOriginKind, SinkCategory,
        SinkPayloadExtractor, SinkTargetExtractor, ToolDescriptor,
    )


class PolicyRegistrationError(ValueError):
    """An incomplete or conflicting policy declaration."""


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    owner: str
    factory: Callable[..., Any] | None
    policy_only: bool
    flow: str | None
    tier: str | None
    operation_class: str | None
    result_domain: str | None
    descriptor: ToolDescriptor | None
    writes_code_or_state: bool
    profiles: frozenset[str]
    resource_scope: str | None = None
    typed_action: Any | None = None
    read_backend: bool = False
    readable_domain: str | None = None
    shell_process: bool = False
    memberships: frozenset[str] = frozenset()
    service_sink_adapter: str | None = None


@dataclass(frozen=True)
class ScopedActionProtocol:
    owner: str
    action_type: type[StrEnum]
    validate_scope: Callable[..., bool]
    checkout_action: StrEnum | None = None
    live_lease: Callable[..., Any] | None = None
    checkout_tool: str | None = None
    trusted_scope: TrustedScopeConstructors | None = None


@dataclass(frozen=True)
class TrustedScopeConstructors:
    """Owner-bound constructors called only after trusted ingress validates input."""

    scope_type: type
    issue: Callable[..., Any]
    review: Callable[..., Any]
    registry: Callable[..., Any]
    discovered: Callable[..., Any]
    store: Callable[..., Any]
    retained: Callable[..., Any]


@dataclass(frozen=True)
class PolicyConsumerHooks:
    owner: str
    resolve_review: Callable[..., Any]
    revalidate_head: Callable[..., Any]
    resolve_issue: Callable[..., Any]
    superseded_push: Callable[..., bool]
    verified_push: Callable[..., bool]
    agent_push: Callable[..., bool]
    tracked_file: Callable[..., bool]
    derive_retained_scope: Callable[..., Any]
    action_for: Callable[[str], Any]
    retained_checkout_root: Callable[[], Any]
    active_checkout: Callable[..., Any]
    git_command: Callable[..., Any]
    protected_refs: frozenset[str]
    lineage: Callable[..., Any]
    verified_base: Callable[..., Any]
    git_binary: Any
    git_config: tuple[str, ...]
    git_env: Callable[..., Any]
    lease_metadata: str
    review_submission: Callable[..., Any]
    claim_review: Callable[..., Any]
    repository_inventory: Callable[..., Any]
    repository_selection: Callable[..., None]
    shell_environment: Callable[..., dict[str, str]]


@dataclass(frozen=True)
class AuthorityProfile:
    name: str
    owner: str
    capabilities: frozenset[str] = frozenset()
    bounded_capabilities: frozenset[str] = frozenset()
    capability_companions: Mapping[str, frozenset[str]] = field(default_factory=dict)
    scheduler: bool = False
    fetch_url_policy: tuple[str, str] | None = None
    pr_review_scope: bool = False
    builtin_trigger_config: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SchedulerJob:
    owner: str
    name: str
    job_id: str
    callback: Callable[..., Any]


@dataclass(frozen=True)
class CliCommand:
    owner: str
    name: str
    configure: Callable[..., Any]
    dispatch: Callable[..., Any]


@dataclass(frozen=True)
class PluginRoute:
    owner: str
    method: str
    path: str
    handler: Callable[..., Any]
    role: str


@dataclass(frozen=True)
class PromptBlockProvider:
    owner: str
    name: str
    section: str
    max_bytes: int
    applies: Callable[..., bool]
    render: Callable[..., str]
    static: bool = False


_FLOW = frozenset({"source", "sink", "both", "neither"})
_TIERS = frozenset({"scope-contained", "scoped-with-provenance", "code-execution", "unbounded"})
_OPERATIONS = frozenset({"open", "resource_scoped", "admin_required"})
_SCOPES = frozenset({"channel", "repo_pr", "worklink", "ci_run", "ci_branch"})
_PROMPT_SECTIONS = frozenset({
    "channel_memory", "feedback", "core_proposals", "session_summaries",
    "usage", "upcoming", "commitments", "self_state",
})
# The executable profiles in access_control.parse_service_shell_argv_with_diagnostics.
# Keep this inert: importing access_control here would cycle through core policy.
_SHELL_PROFILES = frozenset({
    "scheduler_read_only", "repo_review", "maintenance", "session_boundary",
    "upgrade_workspace",
})
_REQUIRED_TOOL_FIELDS = frozenset({
    "name", "flow", "tier", "operation_class", "result_domain", "result_origin",
    "sink_category", "sink_destination", "sink_target_extractor",
    "sink_payload_extractor", "writes_code_or_state", "profiles", "read_backend",
})
_TOOL_MEMBERSHIPS = frozenset({
    "forge_actions", "repo_actions", "retained_repo_tools", "read_resource_operations",
    "acp_hands_result_tools", "channel_operations", "write_operations",
    "write_resource_operations", "fixed_service_sinks", "taint_independent_egress",
    "worklink_build_tools", "protected_metadata_operations", "catalog_admin_builtin",
    "tainted_writes", "tainted_file_writes", "schedule_writes", "saga_mutations",
    "standing_review", "remediation_effect", "typed_error_signal", "repo_publish_tools",
})
_CORE_ONLY_MEMBERSHIPS = frozenset({
    "retained_repo_tools", "fixed_service_sinks", "worklink_build_tools",
    "taint_independent_egress", "catalog_admin_builtin", "acp_hands_result_tools",
    "repo_publish_tools",
})
_FIRST_PARTY_PROJECTED_MEMBERSHIPS = frozenset({
    "retained_repo_tools", "worklink_build_tools", "repo_publish_tools",
})


def selected_first_party_available() -> bool:
    """Check the installed first-party entry point without importing its provider."""
    from .discovery import enabled_plugins

    return any(
        candidate.enabled and candidate.diagnostic is None
        and candidate.name == "cultivate"
        and candidate.distribution == "mimir-cultivate"
        and candidate.entry_point.group == "mimir.plugins"
        and candidate.entry_point.value == "mimir_cultivate:provider"
        for candidate in enabled_plugins()
    )


def _name(value: str, kind: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PolicyRegistrationError(f"invalid {kind}: {value!r}")
    return value


def _immutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_immutable(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_immutable(item) for item in value)
    return value


class PolicyRegistry:
    """Build once from core registrations, then selected plugin registrations."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolPolicy] = {}
        self._capabilities: dict[str, str] = {}
        self._capability_tiers: dict[str, str] = {}
        self._capability_companions: dict[str, frozenset[str]] = {}
        self._legacy_service_sink_tiers: dict[str, str] = {}
        self._profiles: dict[str, AuthorityProfile] = {}
        self._shell_profiles: dict[str, str] = {}
        self._operator_shell_profile: str | None = None
        self._principals: dict[str, Any] = {}
        self._principal_owners: dict[str, str] = {}
        self._poller_properties: dict[str, Any] = {}
        self._plugin_poller_properties: dict[str, tuple[str, Any]] = {}
        self._poller_hooks: dict[str, tuple[str, Callable[..., Any]]] = {}
        self._turn_hooks: dict[tuple[str, str], Callable[..., Any]] = {}
        self._server_hooks: dict[tuple[str, str], Callable[..., Any]] = {}
        self._credential_probes: dict[str, tuple[str, str, tuple[str, ...], str, Callable[..., Any]]] = {}
        self._scheduler_jobs: dict[str, SchedulerJob] = {}
        self._cli_commands: dict[str, CliCommand] = {}
        self._routes: dict[tuple[str, str], PluginRoute] = {}
        self._dashboard_entries: dict[str, tuple[str, Any]] = {}
        self._prompt_providers: dict[str, PromptBlockProvider] = {}
        self._state_slots: dict[str, type] = {}
        self._scoped_actions: dict[str, ScopedActionProtocol] = {}
        self._retained_checkout_operation: tuple[str, Callable[..., dict[str, object]]] | None = None
        self._retained_effect_lease: tuple[str, Callable[..., Any], Callable[..., Any]] | None = None
        self._policy_consumer_hooks: PolicyConsumerHooks | None = None
        self._selected_plugin_candidates: tuple[PluginCandidate, ...] = ()
        self._selection_bound = False
        self._projected_owner: str | None = None
        self._frozen = False

    def registrar(self, plugin: str) -> PolicyRegistrar:
        self._writable()
        return PolicyRegistrar(self, _name(plugin, "plugin"))

    @property
    def selected_plugin_candidates(self) -> tuple[PluginCandidate, ...]:
        """The process selection captured before provider registration and freeze."""
        return self._selected_plugin_candidates

    def select_plugin_candidates(self, candidates: tuple[PluginCandidate, ...]) -> None:
        self._writable()
        if self._selection_bound or not isinstance(candidates, tuple):
            raise PolicyRegistrationError("invalid or repeated selected plugin snapshot")
        self._selected_plugin_candidates = candidates
        self._selection_bound = True

    def _project_first_party_memberships(
        self, candidate: PluginCandidate, memberships: Mapping[str, Iterable[str]],
    ) -> None:
        """Apply the selected first-party package's host-owned membership projection."""
        self._writable()
        ep = candidate.entry_point
        dist = getattr(ep, "dist", None)
        if (
            not self._selection_bound
            or not any(selected is candidate for selected in self._selected_plugin_candidates)
            or sum(selected.name == candidate.name for selected in self._selected_plugin_candidates) != 1
            or self._projected_owner is not None
            or candidate.name != "cultivate"
            or candidate.distribution != "mimir-cultivate"
            or not candidate.enabled or candidate.diagnostic is not None
            or candidate.manifest is None
            or ep.group != "mimir.plugins" or ep.name != candidate.name
            or ep.value != "mimir_cultivate:provider"
            or dist is None or dist.metadata.get("Name", "").lower().replace("_", "-") != "mimir-cultivate"
        ):
            raise PolicyRegistrationError("invalid selected first-party membership owner")
        if not isinstance(memberships, Mapping) or memberships.keys() - _FIRST_PARTY_PROJECTED_MEMBERSHIPS:
            raise PolicyRegistrationError("invalid first-party membership projection")
        updates: dict[str, frozenset[str]] = {}
        for category, names in memberships.items():
            if isinstance(names, (str, bytes)):
                raise PolicyRegistrationError(f"invalid projection for {category}")
            try:
                entries = tuple(names)
            except TypeError as exc:
                raise PolicyRegistrationError(f"invalid projection for {category}") from exc
            if any(not isinstance(name, str) for name in entries):
                raise PolicyRegistrationError(f"invalid projection for {category}")
            if len(entries) != len(set(entries)):
                raise PolicyRegistrationError(f"duplicate projected tool in {category}")
            for name in entries:
                tool = self._tools.get(name)
                if tool is None or tool.owner != candidate.name:
                    raise PolicyRegistrationError(f"{category}: absent or foreign tool {name!r}")
                updates[name] = updates.get(name, frozenset()) | {category}
        for name, categories in updates.items():
            tool = self._tools[name]
            self._tools[name] = replace(tool, memberships=tool.memberships | categories)
        self._projected_owner = candidate.name

    def _writable(self) -> None:
        if self._frozen:
            raise PolicyRegistrationError("policy registry is frozen")

    def _selected_first_party(self, owner: str) -> bool:
        if not self._selection_bound or owner != "cultivate":
            return False
        candidates = [candidate for candidate in self._selected_plugin_candidates
                      if candidate.name == owner]
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        entry_point = getattr(candidate, "entry_point", None)
        distribution = getattr(entry_point, "dist", None)
        return bool(
            getattr(candidate, "enabled", False) and getattr(candidate, "diagnostic", None) is None
            and getattr(candidate, "distribution", None) == "mimir-cultivate"
            and getattr(entry_point, "group", None) == "mimir.plugins"
            and getattr(entry_point, "name", None) == owner
            and getattr(entry_point, "value", None) == "mimir_cultivate:provider"
            and distribution is not None
            and distribution.metadata.get("Name", "").lower().replace("_", "-") == "mimir-cultivate"
        )

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def tools(self) -> Mapping[str, ToolPolicy]:
        return MappingProxyType(self._tools)

    @property
    def capabilities(self) -> Mapping[str, str]:
        return MappingProxyType(self._capabilities)

    @property
    def capability_companions(self) -> Mapping[str, frozenset[str]]:
        return MappingProxyType(self._capability_companions)

    @property
    def legacy_service_sink_tiers(self) -> Mapping[str, str]:
        return MappingProxyType(self._legacy_service_sink_tiers)

    @property
    def profiles(self) -> Mapping[str, AuthorityProfile]:
        return MappingProxyType(self._profiles)

    @property
    def shell_profile_mappings(self) -> Mapping[str, str]:
        return MappingProxyType(self._shell_profiles)

    @property
    def operator_shell_profile(self) -> str | None:
        return self._operator_shell_profile

    @property
    def service_principals(self) -> Mapping[str, Any]:
        return MappingProxyType(self._principals)

    @property
    def poller_properties(self) -> Mapping[str, Any]:
        return MappingProxyType(self._poller_properties)

    @property
    def plugin_poller_properties(self) -> Mapping[str, tuple[str, Any]]:
        return MappingProxyType(self._plugin_poller_properties)

    @property
    def poller_hooks(self) -> Mapping[str, tuple[str, Callable[..., Any]]]:
        return MappingProxyType(self._poller_hooks)

    @property
    def turn_hooks(self) -> Mapping[tuple[str, str], Callable[..., Any]]:
        return MappingProxyType(self._turn_hooks)

    def register_turn_hook(self, name: str, hook: Callable[..., Any], *, owner: str = "core") -> None:
        self._writable()
        name = _name(name, "turn hook")
        if (owner == "core" or (owner, name) in self._turn_hooks or not callable(hook)
                or (self._selection_bound and not any(
                    candidate.name == owner and candidate.enabled and candidate.diagnostic is None
                    for candidate in self._selected_plugin_candidates
                ))):
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate turn hook: {name}")
        self._turn_hooks[owner, name] = hook

    @property
    def server_hooks(self) -> Mapping[tuple[str, str], Callable[..., Any]]:
        return MappingProxyType(self._server_hooks)

    def register_server_hook(self, name: str, hook: Callable[..., Any], *, owner: str = "core") -> None:
        self._writable()
        name = _name(name, "server hook")
        if (owner == "core" or (owner, name) in self._server_hooks or not callable(hook)
                or not self._selection_bound or not any(
                    candidate.name == owner and candidate.enabled and candidate.diagnostic is None
                    for candidate in self._selected_plugin_candidates
                )):
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate server hook: {name}")
        self._server_hooks[owner, name] = hook

    @property
    def credential_probes(self) -> Mapping[str, tuple[str, str, tuple[str, ...], str, Callable[..., Any]]]:
        return MappingProxyType(self._credential_probes)

    def register_credential_probe(
        self, name: str, cred_type: str, env_vars: tuple[str, ...],
        description: str, probe: Callable[[], tuple[bool, str]], *, owner: str = "core",
    ) -> None:
        self._writable()
        selected_owner = self._selection_bound and sum(
            candidate.name == owner and candidate.enabled and candidate.diagnostic is None
            for candidate in self._selected_plugin_candidates
        ) == 1
        private_prefix = f"MIMIR_PLUGIN_{owner.upper().replace('-', '_')}_"
        if (not selected_owner or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                or not (name.startswith(private_prefix) and name != private_prefix
                        or self._selected_first_party(owner))
                or name in self._credential_probes or cred_type not in {"A", "B", "C", "D"}
                or not isinstance(env_vars, tuple) or not env_vars
                or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                       for key in env_vars)
                or not isinstance(description, str) or not description
                or not callable(probe)):
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate credential probe: {name}")
        self._credential_probes[name] = (owner, cred_type, env_vars, description, probe)

    @property
    def scheduler_jobs(self) -> Mapping[str, SchedulerJob]:
        return MappingProxyType(self._scheduler_jobs)

    @property
    def cli_commands(self) -> Mapping[str, CliCommand]:
        return MappingProxyType(self._cli_commands)

    @property
    def routes(self) -> Mapping[tuple[str, str], PluginRoute]:
        return MappingProxyType(self._routes)

    @property
    def dashboard_entries(self) -> Mapping[str, tuple[str, Any]]:
        return MappingProxyType(self._dashboard_entries)

    @property
    def prompt_providers(self) -> Mapping[str, PromptBlockProvider]:
        return MappingProxyType(self._prompt_providers)

    @property
    def state_slots(self) -> Mapping[str, type]:
        return MappingProxyType(self._state_slots)

    @property
    def scoped_actions(self) -> Mapping[str, ScopedActionProtocol]:
        return MappingProxyType(self._scoped_actions)

    @property
    def retained_checkout_operation(self) -> tuple[str, Callable[..., dict[str, object]]] | None:
        return self._retained_checkout_operation

    def register_retained_checkout_operation(
        self, operation: Callable[..., dict[str, object]], *, owner: str = "core",
    ) -> None:
        self._writable()
        if (owner == "core" or self._retained_checkout_operation is not None
                or not callable(operation)
                or not any(candidate.name == owner and candidate.enabled
                           and candidate.diagnostic is None
                           for candidate in self._selected_plugin_candidates)):
            raise PolicyRegistrationError(f"{owner}: invalid retained checkout operation")
        self._retained_checkout_operation = (owner, operation)

    @property
    def retained_effect_lease(self) -> tuple[str, Callable[..., Any], Callable[..., Any]] | None:
        return self._retained_effect_lease

    def register_retained_effect_lease(
        self, lease: Callable[..., Any], runner: Callable[..., Any], *, owner: str = "core",
    ) -> None:
        self._writable()
        if (not self._selected_first_party(owner) or self._retained_effect_lease is not None
                or not callable(lease) or not callable(runner)):
            raise PolicyRegistrationError(f"{owner}: invalid retained effect lease")
        self._retained_effect_lease = (owner, lease, runner)

    @property
    def policy_consumer_hooks(self) -> PolicyConsumerHooks | None:
        return self._policy_consumer_hooks

    def register_policy_consumer_hooks(self, hooks: PolicyConsumerHooks, *, owner: str = "core") -> None:
        self._writable()
        if (not self._selected_first_party(owner) or self._policy_consumer_hooks is not None
                or not isinstance(hooks, PolicyConsumerHooks) or hooks.owner != owner
                or any(not callable(getattr(hooks, name)) for name in (
                    "resolve_review", "revalidate_head", "resolve_issue", "superseded_push",
                    "verified_push", "agent_push", "tracked_file",
                    "derive_retained_scope", "action_for", "retained_checkout_root", "active_checkout", "git_command", "lineage",
                    "verified_base", "git_env", "review_submission", "claim_review",
                    "repository_inventory", "repository_selection", "shell_environment",
                )) or not isinstance(hooks.protected_refs, frozenset)
                or not isinstance(hooks.git_config, tuple) or not isinstance(hooks.lease_metadata, str)):
            raise PolicyRegistrationError(f"{owner}: invalid policy consumer hooks")
        self._policy_consumer_hooks = hooks

    def register_scoped_action(
        self, action_type: type[StrEnum], validate_scope: Callable[..., bool],
        *, checkout_action: StrEnum | None = None,
        live_lease: Callable[..., Any] | None = None,
        checkout_tool: str | None = None,
        trusted_scope: TrustedScopeConstructors | None = None,
        owner: str = "core",
    ) -> None:
        self._writable()
        if (owner == "core" or owner in self._scoped_actions
                or not isinstance(action_type, type) or not issubclass(action_type, StrEnum)
                or not tuple(action_type) or not callable(validate_scope)
                or (checkout_action is None) != (live_lease is None)
                or (checkout_action is not None and type(checkout_action) is not action_type)
                or (checkout_tool is not None and (
                    checkout_action is None or not isinstance(checkout_tool, str)
                    or not checkout_tool or checkout_tool != checkout_tool.strip()
                ))
                or (live_lease is not None and not callable(live_lease))
                or (trusted_scope is not None and (
                    not self._selected_first_party(owner)
                    or type(trusted_scope) is not TrustedScopeConstructors
                    or not isinstance(trusted_scope.scope_type, type)
                    or any(not callable(getattr(trusted_scope, name)) for name in (
                        "issue", "review", "registry", "discovered", "store", "retained",
                    ))
                ))
                or any(protocol.action_type is action_type for protocol in self._scoped_actions.values())):
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate scoped action protocol")
        self._scoped_actions[owner] = ScopedActionProtocol(
            owner, action_type, validate_scope, checkout_action, live_lease,
            checkout_tool, trusted_scope,
        )

    def register_poller_hook(self, name: str, hook: Callable[..., Any], *, owner: str = "core") -> None:
        self._writable()
        name = _name(name, "poller hook")
        if not callable(hook) or name in self._poller_hooks:
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate poller hook: {name}")
        self._poller_hooks[name] = (owner, hook)

    def register_scheduler_job(self, name: str, job_id: str, callback: Callable[..., Any], *, owner: str = "core") -> None:
        self._writable()
        name, job_id = _name(name, "scheduler job"), _name(job_id, "scheduler job ID")
        if name in self._scheduler_jobs or any(job.job_id == job_id for job in self._scheduler_jobs.values()):
            raise PolicyRegistrationError(f"{owner}: duplicate scheduler job name or ID: {name} ({job_id})")
        if not callable(callback):
            raise PolicyRegistrationError(f"{owner}: scheduler job {name} requires a callable")
        self._scheduler_jobs[name] = SchedulerJob(owner, name, job_id, callback)

    def register_cli_command(self, name: str, configure: Callable[..., Any], dispatch: Callable[..., Any], *, owner: str = "core") -> None:
        self._writable()
        name = _name(name, "CLI command")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in self._cli_commands:
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate CLI command: {name}")
        if not callable(configure) or not callable(dispatch):
            raise PolicyRegistrationError(f"{owner}: CLI command {name} requires parser and handler")
        self._cli_commands[name] = CliCommand(owner, name, configure, dispatch)

    def register_route(self, method: str, path: str, handler: Callable[..., Any], *, role: str = "user", owner: str = "core") -> None:
        self._writable()
        method = _name(method, "route method").upper()
        path = _name(path, "route path")
        if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", path) or "//" in path or path.endswith("/"):
            raise PolicyRegistrationError(f"{owner}: invalid route path: {path}")
        if owner != "core" and not path.startswith(f"/api/v1/plugins/{owner}/"):
            raise PolicyRegistrationError(f"{owner}: route must be under /api/v1/plugins/{owner}/")
        key = (method, path)
        if key in self._routes or not callable(handler) or role not in {"user", "admin"}:
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate route: {method} {path}")
        self._routes[key] = PluginRoute(owner, method, path, handler, role)

    def register_dashboard_entry(self, entry_id: str, entry: Any, *, owner: str = "core") -> None:
        self._writable()
        entry_id = _name(entry_id, "dashboard entry ID")
        if entry_id in self._dashboard_entries:
            raise PolicyRegistrationError(f"duplicate dashboard entry: {entry_id}")
        self._dashboard_entries[entry_id] = (owner, entry)

    def register_prompt_provider(self, name: str, section: str, max_bytes: int,
                                 applies: Callable[..., bool], render: Callable[..., str],
                                 *, static: bool = False, owner: str = "core") -> None:
        self._writable()
        name = _name(name, "prompt provider")
        if ":" in name or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in self._prompt_providers:
            raise PolicyRegistrationError(f"{owner}: invalid or duplicate prompt provider: {name}")
        if section not in _PROMPT_SECTIONS or type(max_bytes) is not int or max_bytes <= 0:
            raise PolicyRegistrationError(f"{owner}: invalid prompt section or byte budget")
        if not callable(applies) or not callable(render) or type(static) is not bool:
            raise PolicyRegistrationError(f"{owner}: invalid prompt provider callbacks or static declaration")
        self._prompt_providers[name] = PromptBlockProvider(owner, name, section, max_bytes, applies, render, static)

    def register_state_slot(self, slot_type: type, *, owner: str = "core") -> None:
        self._writable()
        if owner in self._state_slots or not isinstance(slot_type, type) or not hasattr(slot_type, "__dataclass_fields__") \
                or not getattr(slot_type, "__dataclass_params__").frozen:
            raise PolicyRegistrationError(f"{owner}: state slot must be a unique frozen dataclass type")
        self._state_slots[owner] = slot_type

    def register_poller_properties(self, canonical: str, properties: Any, *, owner: str = "core") -> None:
        self._writable()
        from .core import PollerProperties

        owner = _name(owner, "poller owner")
        canonical = _name(canonical, "poller canonical")
        if not canonical.startswith("poller:") or not canonical.removeprefix("poller:"):
            raise PolicyRegistrationError("poller properties require a poller canonical")
        if not isinstance(properties, PollerProperties) or any(
            not isinstance(value, bool) for value in properties.principal_fields().values()
        ):
            raise PolicyRegistrationError("invalid poller properties")
        if owner != "core" and properties.worklink_retained_checkout and not self._selected_first_party(owner):
            raise PolicyRegistrationError(f"{owner}: retained checkout requires selected first-party owner")
        if canonical in self._poller_properties or canonical in self._plugin_poller_properties:
            raise PolicyRegistrationError(f"duplicate poller properties: {canonical}")
        if owner == "core":
            self._poller_properties[canonical] = properties
        else:
            self._plugin_poller_properties[canonical] = (owner, properties)

    @property
    def descriptors(self) -> Mapping[str, ToolDescriptor]:
        return MappingProxyType({name: tool.descriptor for name, tool in self._tools.items() if tool.descriptor is not None})

    @property
    def flow(self) -> Mapping[str, str]:
        return MappingProxyType({name: tool.flow for name, tool in self._tools.items() if tool.flow is not None})

    @property
    def tiers(self) -> Mapping[str, str]:
        tiers = {name: tool.tier for name, tool in self._tools.items() if tool.tier is not None}
        tiers.update(self._capability_tiers)
        return MappingProxyType(tiers)

    @property
    def authority_profiles(self) -> Mapping[str, frozenset[str]]:
        return MappingProxyType({name: profile.capabilities for name, profile in self._profiles.items()})

    def register_tool(self, **kwargs: Any) -> None:
        """Require explicit metadata even when its declared value is None or False."""
        self._writable()
        owner = kwargs.get("owner", "core")
        name = kwargs.get("name", "<unnamed>")
        missing = _REQUIRED_TOOL_FIELDS - kwargs.keys()
        if missing:
            raise PolicyRegistrationError(
                f"{owner}: tool {name}: missing required metadata: {', '.join(sorted(missing))}"
            )
        try:
            self._register_tool(**kwargs)
        except TypeError as exc:
            raise PolicyRegistrationError(f"{owner}: tool {name}: invalid registration: {exc}") from exc

    def _register_tool(
        self, *, name: str, factory: Callable[..., Any] | None = None,
        policy_only: bool = False, flow: str | None, tier: str | None,
        operation_class: str | None, result_domain: str | None,
        result_origin: ResultOriginKind, sink_category: SinkCategory | None,
        sink_destination: str | None,
        sink_target_extractor: SinkTargetExtractor | None | object,
        sink_payload_extractor: SinkPayloadExtractor | None | object,
        writes_code_or_state: bool, profiles: Iterable[str], read_backend: bool,
        resource_scope: str | None = None, typed_action: Any | None = None,
        readable_domain: str | None = None,
        shell_process: bool = False, memberships: Iterable[str] = (),
        service_sink_adapter: str | None = None,
        fetch_authorization: FetchAuthorizationKind | None = None,
        git_operation_result: bool = False, ifc_delegation: bool = False,
        budget_exempt: bool = False, owner: str = "core",
        descriptor_registered: bool = True,
    ) -> None:
        from mimir.tool_descriptors import (
            _PAYLOAD_EXTRACTOR_UNDECLARED, _TARGET_EXTRACTOR_UNDECLARED,
            FetchAuthorizationKind, ResultOriginKind, SinkCategory, ToolDescriptor,
        )

        self._writable()
        name = _name(name, "tool")
        if name in self._tools:
            raise PolicyRegistrationError(f"duplicate tool: {name}")
        if owner != "core" and name.startswith("mcp_"):
            raise PolicyRegistrationError(f"{owner}: tool {name}: mcp_ prefix is reserved")
        capability_owner = self._capabilities.get(name)
        if capability_owner is not None and capability_owner != owner:
            raise PolicyRegistrationError(f"{owner}: tool {name}: capability owned by {capability_owner}")
        if not isinstance(policy_only, bool) or (factory is None) == (not policy_only):
            raise PolicyRegistrationError(f"{name}: supply a factory or explicit policy_only=True")
        if factory is not None and not callable(factory):
            raise PolicyRegistrationError(f"{name}: invalid factory")
        if flow not in _FLOW | ({None} if owner == "core" else set()) or operation_class not in _OPERATIONS | ({None} if owner == "core" else set()) or tier not in _TIERS | {None}:
            raise PolicyRegistrationError(f"{name}: invalid flow, tier or operation class")
        if owner != "core" and tier is None:
            raise PolicyRegistrationError(f"{name}: plugin tier is required")
        capability_tier = self._capability_tiers.get(name)
        if capability_tier is not None and tier is not None and capability_tier != tier:
            raise PolicyRegistrationError(
                f"{owner}: tool {name}: tier {tier} conflicts with capability tier {capability_tier}"
            )
        if resource_scope is not None and resource_scope not in _SCOPES:
            raise PolicyRegistrationError(f"{name}: unknown resource scope {resource_scope!r}")
        if resource_scope in {"worklink", "ci_run", "ci_branch"} and not self._selected_first_party(owner):
            raise PolicyRegistrationError(f"{owner}: selected first-party resource scope required")
        if owner != "core" and (operation_class == "resource_scoped") != (resource_scope is not None):
            raise PolicyRegistrationError(f"{name}: resource_scoped requires an explicit resource scope")
        if not isinstance(result_origin, ResultOriginKind) or not isinstance(sink_category, (SinkCategory, type(None))):
            raise PolicyRegistrationError(f"{name}: invalid origin or sink category")
        if not isinstance(sink_destination, (str, type(None))) or (
            owner != "core" and sink_category is not None and not sink_destination
        ):
            raise PolicyRegistrationError(f"{name}: invalid sink destination")
        if (sink_target_extractor is _TARGET_EXTRACTOR_UNDECLARED and owner != "core"
                and not (sink_category is None and self._selected_first_party(owner))) or (
            sink_target_extractor is not None
            and sink_target_extractor is not _TARGET_EXTRACTOR_UNDECLARED
            and not callable(sink_target_extractor)
        ):
            raise PolicyRegistrationError(f"{name}: invalid target extractor")
        if (sink_payload_extractor is _PAYLOAD_EXTRACTOR_UNDECLARED and owner != "core"
                and not (self._selected_first_party(owner) and sink_category not in {
                    SinkCategory.NETWORK, SinkCategory.HTTP_WEBHOOK, SinkCategory.NOTIFICATION,
                    SinkCategory.EXTERNAL_MCP, SinkCategory.CROSS_CHANNEL, SinkCategory.FORGE,
                })) or (
            sink_payload_extractor is not None
            and sink_payload_extractor is not _PAYLOAD_EXTRACTOR_UNDECLARED
            and not callable(sink_payload_extractor)
        ):
            raise PolicyRegistrationError(f"{name}: invalid payload extractor")
        if not isinstance(writes_code_or_state, bool) or not isinstance(read_backend, bool):
            raise PolicyRegistrationError(f"{name}: invalid boolean metadata")
        if owner != "core" and read_backend and (
            flow not in {"source", "both"}
            or not result_domain
            or result_origin & ResultOriginKind.NON_INGESTING
            or not result_origin & (ResultOriginKind.EXTERNAL | ResultOriginKind.REPOSITORY)
        ):
            raise PolicyRegistrationError(f"{owner}: tool {name}: read backend requires a protected ingesting result and source flow")
        if owner != "core" and result_origin & ResultOriginKind.NON_INGESTING and (
            flow != "sink"
            or result_origin & (ResultOriginKind.EXTERNAL | ResultOriginKind.REPOSITORY)
        ):
            raise PolicyRegistrationError(f"{owner}: tool {name}: NON_INGESTING requires sink-only flow and no external or repository origin")
        if owner != "core" and sink_category in {
            SinkCategory.NETWORK, SinkCategory.HTTP_WEBHOOK, SinkCategory.NOTIFICATION,
            SinkCategory.EXTERNAL_MCP, SinkCategory.CROSS_CHANNEL, SinkCategory.FORGE,
        } and not callable(sink_payload_extractor) and not (
            sink_category == SinkCategory.FORGE and sink_payload_extractor is None
            and self._selected_first_party(owner)
        ):
            raise PolicyRegistrationError(f"{owner}: tool {name}: outbound sink requires a callable payload extractor")
        if service_sink_adapter is not None and not isinstance(service_sink_adapter, str):
            raise PolicyRegistrationError(f"{owner}: tool {name}: invalid service sink adapter")
        if result_domain is not None:
            _name(result_domain, "result domain")
        if readable_domain is not None:
            _name(readable_domain, "readable domain")
        declared_profiles = frozenset(_name(profile, "profile") for profile in profiles)
        declared_memberships = frozenset(_name(member, "membership") for member in memberships)
        if declared_memberships - _TOOL_MEMBERSHIPS:
            raise PolicyRegistrationError(f"{owner}: tool {name}: unknown memberships {sorted(declared_memberships - _TOOL_MEMBERSHIPS)}")
        if owner != "core" and declared_memberships & _CORE_ONLY_MEMBERSHIPS:
            raise PolicyRegistrationError(f"{owner}: tool {name}: core-only memberships {sorted(declared_memberships & _CORE_ONLY_MEMBERSHIPS)}")
        if not isinstance(descriptor_registered, bool) or owner != "core" and not descriptor_registered:
            raise PolicyRegistrationError(f"{owner}: tool {name}: descriptor exclusion is core-only")
        descriptor = ToolDescriptor(
            sink_category=sink_category, sink_target_extractor=sink_target_extractor,
            sink_destination=sink_destination, sink_payload_extractor=sink_payload_extractor,
            fetch_authorization=(
                fetch_authorization if fetch_authorization is not None
                else FetchAuthorizationKind.NONE
            ),
            result_origin=result_origin,
            git_operation_result=git_operation_result, ifc_delegation=ifc_delegation,
            budget_exempt=budget_exempt,
        ) if descriptor_registered else None
        self._tools[name] = ToolPolicy(
            name, owner, factory, policy_only, flow, tier, operation_class,
            result_domain, descriptor, writes_code_or_state, declared_profiles,
            resource_scope, typed_action, read_backend, readable_domain,
            shell_process, declared_memberships, service_sink_adapter,
        )

    def register_capability(
        self, name: str, *, owner: str = "core", tier: str | None = None,
        companions: Iterable[str] = (), legacy_service_sink_tier: str | None = None,
    ) -> None:
        self._writable()
        name = _name(name, "capability")
        if name in self._capabilities:
            raise PolicyRegistrationError(f"duplicate capability: {name}")
        tool = self._tools.get(name)
        if tool is not None and tool.owner != owner:
            raise PolicyRegistrationError(f"{owner}: capability {name}: tool owned by {tool.owner}")
        declared_companions = frozenset(_name(item, "companion") for item in companions)
        if legacy_service_sink_tier is not None:
            if owner != "core" or legacy_service_sink_tier not in _TIERS:
                raise PolicyRegistrationError(f"{owner}: invalid legacy sink tier for {name}")
        if tier is not None:
            if tier not in _TIERS or (owner != "core" and tool is not None):
                raise PolicyRegistrationError(f"{owner}: capability {name}: invalid capability tier")
            tool = self._tools.get(name)
            if tool is not None and tool.tier is not None and tool.tier != tier:
                raise PolicyRegistrationError(
                    f"{owner}: capability {name}: tier {tier} conflicts with tool tier {tool.tier}"
                )
        # Publish only after every check: a rejected collision must not leave
        # companion or sink-tier grants behind in a still-writable registry.
        if declared_companions:
            self._capability_companions[name] = declared_companions
        if legacy_service_sink_tier is not None:
            self._legacy_service_sink_tiers[name] = legacy_service_sink_tier
        if tier is not None:
            self._capability_tiers[name] = tier
        self._capabilities[name] = owner

    def register_authority_profile(
        self, name: str, *, capabilities: Iterable[str] = (),
        bounded_capabilities: Iterable[str] = (),
        capability_companions: Mapping[str, Iterable[str]] | None = None,
        scheduler: bool = False, fetch_url_policy: tuple[str, str] | None = None,
        pr_review_scope: bool = False,
        builtin_trigger_config: Mapping[str, Any] | None = None,
        owner: str = "core",
    ) -> None:
        self._writable()
        name = _name(name, "profile")
        if name in self._profiles:
            raise PolicyRegistrationError(f"duplicate profile: {name}")
        if owner != "core":
            for field_name, value, default in (
                ("builtin_trigger_config", builtin_trigger_config, None),
                ("fetch_url_policy", fetch_url_policy, None),
                ("pr_review_scope", pr_review_scope, False),
            ):
                if value != default:
                    raise PolicyRegistrationError(f"{owner}: plugin profile cannot declare {field_name}")
        self._profiles[name] = AuthorityProfile(
            name, owner, frozenset(capabilities), frozenset(bounded_capabilities),
            MappingProxyType({key: frozenset(values) for key, values in (capability_companions or {}).items()}),
            scheduler, fetch_url_policy, pr_review_scope,
            _immutable(builtin_trigger_config) if builtin_trigger_config is not None else None,
        )

    def register_shell_profile_mapping(self, profile: str, shell_profile: str, *, owner: str = "core") -> None:
        self._writable()
        profile = _name(profile, "profile")
        if profile in self._shell_profiles:
            raise PolicyRegistrationError(f"duplicate shell profile mapping: {profile}")
        if owner != "core" and (
            profile not in self._profiles or self._profiles[profile].owner != owner
        ):
            raise PolicyRegistrationError(f"{owner}: cannot map foreign profile {profile}")
        shell_profile = _name(shell_profile, "shell profile")
        if shell_profile not in _SHELL_PROFILES:
            raise PolicyRegistrationError(f"unknown shell profile: {shell_profile}")
        self._shell_profiles[profile] = shell_profile

    def register_operator_shell_profile(self, shell_profile: str, *, owner: str = "core") -> None:
        self._writable()
        if owner != "core":
            raise PolicyRegistrationError(f"{owner}: operator shell profile is core-only")
        if self._operator_shell_profile is not None:
            raise PolicyRegistrationError("duplicate operator shell profile")
        shell_profile = _name(shell_profile, "operator shell profile")
        if shell_profile not in _SHELL_PROFILES:
            raise PolicyRegistrationError(f"unknown shell profile: {shell_profile}")
        self._operator_shell_profile = shell_profile

    def register_service_principal(self, principal: Any, *, owner: str = "core") -> None:
        self._writable()
        from .core import CapabilityTier, ServicePrincipal, ServiceSinkPolicy

        if not isinstance(principal, ServicePrincipal):
            raise PolicyRegistrationError(f"{owner}: expected frozen ServicePrincipal")
        if not isinstance(principal.capabilities, tuple) or not all(
            isinstance(capability, str) and capability for capability in principal.capabilities
        ):
            raise PolicyRegistrationError(f"{owner}: invalid principal capabilities")
        for field_name in ("readable_domains", "sink_destinations"):
            values = getattr(principal, field_name)
            if not isinstance(values, tuple) or not all(isinstance(value, str) and value for value in values):
                raise PolicyRegistrationError(f"{owner}: invalid principal {field_name}")
        if not isinstance(principal.sink_policies, tuple) or not all(
            isinstance(policy, ServiceSinkPolicy)
            and all(isinstance(value, str) and value for value in (policy.operation, policy.adapter, policy.destination))
            for policy in principal.sink_policies
        ):
            raise PolicyRegistrationError(f"{owner}: invalid principal sink policies")
        if principal.authority_profile is not None:
            _name(principal.authority_profile, "principal authority profile")
        if principal.capability_tier is not None and not isinstance(principal.capability_tier, CapabilityTier):
            raise PolicyRegistrationError(f"{owner}: invalid principal capability tier")
        if not isinstance(principal.filesystem_read_roots, tuple) or not all(
            isinstance(root, str) and root for root in principal.filesystem_read_roots
        ):
            raise PolicyRegistrationError(f"{owner}: invalid principal read roots")
        trigger = _name(getattr(principal, "trigger", None), "principal trigger")
        canonical = _name(getattr(principal, "canonical", None), "principal canonical")
        if owner != "core":
            prefix = f"plugin:{owner}:"
            if any(not value.startswith(prefix) or not value[len(prefix):]
                   or ":" in value[len(prefix):]
                   or value[len(prefix):] != value[len(prefix):].strip()
                   for value in (trigger, canonical)):
                raise PolicyRegistrationError(f"{owner}: principal canonical and trigger must use {prefix}<local>")
            if principal.sink_policies:
                raise PolicyRegistrationError(f"{owner}: plugin principal cannot declare raw sink_policies")
            restricted_defaults = {
                "filesystem_read_roots": (), "declared_shell_commands": (),
                "owned_skill_directory": None, "channel_memory_directory": None,
                "configured_delivery_channel": None,
            }
            for field_name, default in restricted_defaults.items():
                if getattr(principal, field_name) != default:
                    raise PolicyRegistrationError(f"{owner}: plugin principal cannot declare {field_name}")
            for field_name in (
                "saga_full_corpus_read", "github_activity",
                "worklink_retained_checkout", "github_ci_watch",
            ):
                if getattr(principal, field_name, False) is not False:
                    raise PolicyRegistrationError(f"{owner}: plugin principal cannot declare {field_name}")
            if principal.proposal_surface != "wiki":
                raise PolicyRegistrationError(f"{owner}: plugin principal cannot declare proposal_surface")
            if principal.capability_tier is not None:
                raise PolicyRegistrationError(f"{owner}: plugin principal tier is derived from capabilities")
        if trigger in self._principals or any(p.canonical == canonical for p in self._principals.values()):
            raise PolicyRegistrationError(f"duplicate service principal: {trigger} ({canonical})")
        self._principals[trigger] = principal
        self._principal_owners[trigger] = owner

    def freeze(self) -> PolicyRegistry:
        self._writable()
        from mimir.tool_descriptors import SinkCategory, validate_tool_descriptors
        from .core import CapabilityTier, ServiceSinkPolicy

        service_adapters = {
            SinkCategory.FILE: "trigger_service_write_roots",
            SinkCategory.NETWORK: "approved_urls",
            SinkCategory.HTTP_WEBHOOK: "approved_urls",
            SinkCategory.NOTIFICATION: "operator_alert",
            SinkCategory.SHELL_PROCESS: "shell_profile",
            SinkCategory.SPAWN: "spawn_workspace",
        }
        service_destinations = {
            SinkCategory.FILE: "filesystem",
            SinkCategory.NETWORK: "network",
            SinkCategory.HTTP_WEBHOOK: "network",
            SinkCategory.NOTIFICATION: "notification",
            SinkCategory.SHELL_PROCESS: "shell_process",
            SinkCategory.SPAWN: "spawn_process",
        }

        first_party_worklink = {
            "worklink_repo": "worklink",
        }

        validate_tool_descriptors(self.descriptors)
        for owner, protocol in self._scoped_actions.items():
            if protocol.checkout_tool is not None:
                checkout = self._tools.get(protocol.checkout_tool)
                if (checkout is None or checkout.owner != owner
                        or checkout.resource_scope != "repo_pr"
                        or checkout.typed_action is not protocol.checkout_action):
                    raise PolicyRegistrationError(f"{owner}: invalid checkout tool designation")
        for name, companions in self._capability_companions.items():
            if not companions <= self._capabilities.keys():
                raise PolicyRegistrationError(f"{name}: unknown companion capability")
        for name, tool in self._tools.items():
            if tool.resource_scope == "worklink" and "worklink_build_tools" not in tool.memberships:
                raise PolicyRegistrationError(f"{name}: worklink scope requires host-projected build membership")
            if tool.resource_scope in {"ci_run", "ci_branch"} and (
                tool.result_domain != "repository" or not tool.read_backend or tool.flow != "source"
            ):
                raise PolicyRegistrationError(f"{name}: CI scope requires repository read metadata")
            if tool.owner != "core":
                category = tool.descriptor.sink_category if tool.descriptor else None
                if tool.profiles and category in {SinkCategory.EXTERNAL_MCP, SinkCategory.PROPOSAL}:
                    raise PolicyRegistrationError(
                        f"{tool.owner}: tool {name}: profile sink has no executable service sink adapter"
                    )
                expected = service_adapters.get(category)
                destination = tool.descriptor.sink_destination if tool.descriptor else None
                if (category == SinkCategory.SPAWN and destination == "worklink"
                        and self._selected_first_party(tool.owner)):
                    expected = "worklink_repo"
                if tool.service_sink_adapter is not None and tool.service_sink_adapter != expected:
                    raise PolicyRegistrationError(f"{tool.owner}: tool {name}: invalid service sink adapter {tool.service_sink_adapter!r}")
                if tool.profiles and expected is not None and tool.service_sink_adapter is None:
                    raise PolicyRegistrationError(f"{tool.owner}: tool {name}: profile sink requires a service sink adapter")
                allowed_destination = first_party_worklink.get(expected, service_destinations.get(category))
                if tool.service_sink_adapter is not None and destination != allowed_destination:
                    raise PolicyRegistrationError(f"{tool.owner}: tool {name}: invalid service sink destination")
            for profile in tool.profiles:
                if profile not in self._profiles or name not in self._profiles[profile].capabilities:
                    raise PolicyRegistrationError(f"{name}: missing profile membership {profile}")
            protocol = self._scoped_actions.get(tool.owner)
            if tool.owner != "core" and tool.resource_scope == "repo_pr":
                if tool.typed_action is None:
                    raise PolicyRegistrationError(f"{name}: repo_pr scope requires typed action")
                if protocol is None:
                    raise PolicyRegistrationError(f"{name}: repo_pr scope requires owner action protocol")
            if tool.typed_action is not None:
                if tool.owner == "core":
                    valid = isinstance(tool.typed_action, StrEnum)
                else:
                    valid = protocol is not None and type(tool.typed_action) is protocol.action_type
                if not valid:
                    raise PolicyRegistrationError(f"{name}: invalid repo/PR action {tool.typed_action!r}")
        for name, profile in self._profiles.items():
            for capability in profile.capabilities | profile.bounded_capabilities:
                if capability not in self._capabilities:
                    raise PolicyRegistrationError(f"{name}: unknown capability {capability}")
                if profile.owner != "core" and self._capabilities[capability] not in {"core", profile.owner}:
                    raise PolicyRegistrationError(f"{name}: foreign capability {capability}")
                tool = self._tools.get(capability)
                if tool is not None and tool.owner != "core" and name not in tool.profiles:
                    raise PolicyRegistrationError(f"{tool.owner}: tool {capability}: undeclared profile membership {name}")
                if profile.owner != "core" and (tool is None or tool.owner != profile.owner):
                    raise PolicyRegistrationError(f"{name}: plugin profile cannot add foreign capability {capability}")
            # Historic session-boundary fetch_url is bounded but not granted.
            legacy_bound = (
                frozenset({"fetch_url"})
                if name == "session-boundary" and profile.owner == "core" else frozenset()
            )
            if not profile.bounded_capabilities - legacy_bound <= profile.capabilities:
                raise PolicyRegistrationError(f"{name}: bounded capability outside profile")
            for capability, companions in profile.capability_companions.items():
                if capability not in profile.capabilities or not companions <= profile.capabilities:
                    raise PolicyRegistrationError(f"{name}: invalid companion capabilities for {capability}")
        for profile in self._shell_profiles:
            if profile not in self._profiles:
                raise PolicyRegistrationError(f"unknown shell authority profile: {profile}")
        derived_principals = {}
        for trigger, principal in self._principals.items():
            profile = getattr(principal, "authority_profile", None)
            if profile is not None and profile not in self._profiles:
                raise PolicyRegistrationError(f"{trigger}: unknown authority profile {profile}")
            owner = self._principal_owners[trigger]
            if owner != "core":
                if profile is None or self._profiles[profile].owner != owner:
                    raise PolicyRegistrationError(f"{trigger}: plugin principal requires its own authority profile")
                if not set(principal.capabilities) <= self._profiles[profile].capabilities:
                    raise PolicyRegistrationError(f"{trigger}: principal capabilities outside profile")
                if not principal.capabilities or not principal.readable_domains or not principal.sink_destinations:
                    raise PolicyRegistrationError(f"{trigger}: incomplete service principal")
                if not principal.creation_path:
                    raise PolicyRegistrationError(f"{trigger}: missing principal creation path")
                policies = []
                tiers = []
                for capability in principal.capabilities:
                    tool = self._tools.get(capability)
                    if tool is None or tool.owner != owner or self._capabilities.get(capability) != owner:
                        raise PolicyRegistrationError(f"{trigger}: principal capability {capability} is not its own tool")
                    tiers.append(tool.tier)
                    descriptor = tool.descriptor
                    if descriptor is None or descriptor.sink_category is None:
                        continue
                    if descriptor.sink_destination not in principal.sink_destinations:
                        raise PolicyRegistrationError(f"{trigger}: missing declared sink destination for {capability}")
                    adapter = tool.service_sink_adapter
                    if adapter is None:
                        raise PolicyRegistrationError(f"{trigger}: sink {capability} requires a service sink adapter")
                    bound = {
                        # Static plugin principals cannot mint filesystem roots.
                        "trigger_service_write_roots": "[]",
                        "approved_urls": "MIMIR_EGRESS_APPROVED_URLS",
                        "operator_alert": "MIMIR_OPERATOR_ALERT_CHANNEL",
                        "shell_profile": self._shell_profiles.get(profile, "scheduler_read_only"),
                        "spawn_workspace": "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
                        "worklink_repo": "WORKLINK_REPO/MIMIR_WORKLINK_REPO",
                    }[adapter]
                    policies.append(ServiceSinkPolicy(capability, adapter, bound))
                tier_order = (
                    "scope-contained", "scoped-with-provenance", "code-execution", "unbounded",
                )
                derived_principals[trigger] = replace(
                    principal, sink_policies=tuple(policies),
                    capability_tier=CapabilityTier(max(tiers, key=tier_order.index)),
                )
        self._principals.update(derived_principals)
        self._frozen = True
        return self


class PolicyRegistrar:
    """A plugin's restricted registration surface; it cannot edit core records."""

    def __init__(self, registry: PolicyRegistry, plugin: str) -> None:
        plugin = _name(plugin, "plugin")
        if plugin == "core":
            raise PolicyRegistrationError("plugin name 'core' is reserved")
        self._registry = registry
        self.plugin = plugin

    def register_tool(self, **kwargs: Any) -> None:
        if "owner" in kwargs:
            raise PolicyRegistrationError(f"{self.plugin}: owner cannot be overridden")
        self._registry.register_tool(owner=self.plugin, **kwargs)

    def register_capability(self, name: str, *, tier: str | None = None) -> None:
        self._registry.register_capability(name, owner=self.plugin, tier=tier)

    def register_authority_profile(self, name: str, **kwargs: Any) -> None:
        if "owner" in kwargs:
            raise PolicyRegistrationError(f"{self.plugin}: owner cannot be overridden")
        self._registry.register_authority_profile(name, owner=self.plugin, **kwargs)

    def add_to_profile(self, profile: str, tool: str) -> None:
        registry = self._registry
        registry._writable()
        registered = registry._tools.get(tool)
        capability_owner = registry._capabilities.get(tool)
        if (registered is None and (capability_owner != self.plugin or tool not in registry._capability_tiers)) or (
            registered is not None and registered.owner != self.plugin
        ):
            raise PolicyRegistrationError(f"{self.plugin}: cannot add foreign tool {tool} to {profile}")
        if profile not in registry._profiles:
            raise PolicyRegistrationError(f"unknown profile: {profile}")
        current = registry._profiles[profile]
        if registered is None and current.owner not in {"core", self.plugin}:
            raise PolicyRegistrationError(f"{self.plugin}: cannot add capability to foreign profile {profile}")
        registry._profiles[profile] = replace(current, capabilities=current.capabilities | {tool})
        if registered is not None:
            registry._tools[tool] = replace(registered, profiles=registered.profiles | {profile})

    def register_shell_profile_mapping(self, profile: str, shell_profile: str) -> None:
        if profile not in self._registry._profiles or self._registry._profiles[profile].owner != self.plugin:
            raise PolicyRegistrationError(f"{self.plugin}: cannot map foreign profile {profile}")
        self._registry.register_shell_profile_mapping(profile, shell_profile, owner=self.plugin)

    def register_service_principal(self, principal: Any) -> None:
        self._registry.register_service_principal(principal, owner=self.plugin)

    def register_poller_hook(self, name: str, hook: Callable[..., Any]) -> None:
        self._registry.register_poller_hook(name, hook, owner=self.plugin)

    def register_poller_properties(self, canonical: str, properties: Any) -> None:
        self._registry.register_poller_properties(canonical, properties, owner=self.plugin)

    def register_turn_hook(self, name: str, hook: Callable[..., Any]) -> None:
        self._registry.register_turn_hook(name, hook, owner=self.plugin)

    def register_server_hook(self, name: str, hook: Callable[..., Any]) -> None:
        self._registry.register_server_hook(name, hook, owner=self.plugin)

    def register_credential_probe(
        self, name: str, cred_type: str, env_vars: tuple[str, ...],
        description: str, probe: Callable[[], tuple[bool, str]],
    ) -> None:
        self._registry.register_credential_probe(
            name, cred_type, env_vars, description, probe, owner=self.plugin,
        )

    def register_retained_checkout_operation(
        self, operation: Callable[..., dict[str, object]],
    ) -> None:
        self._registry.register_retained_checkout_operation(operation, owner=self.plugin)

    def register_retained_effect_lease(
        self, lease: Callable[..., Any], runner: Callable[..., Any],
    ) -> None:
        self._registry.register_retained_effect_lease(lease, runner, owner=self.plugin)

    def register_policy_consumer_hooks(self, hooks: PolicyConsumerHooks) -> None:
        self._registry.register_policy_consumer_hooks(hooks, owner=self.plugin)

    def register_scheduler_job(self, name: str, job_id: str, callback: Callable[..., Any]) -> None:
        self._registry.register_scheduler_job(name, job_id, callback, owner=self.plugin)

    def register_cli_command(self, name: str, configure: Callable[..., Any], dispatch: Callable[..., Any]) -> None:
        self._registry.register_cli_command(name, configure, dispatch, owner=self.plugin)

    def register_route(self, method: str, path: str, handler: Callable[..., Any], *, role: str = "user") -> None:
        self._registry.register_route(method, path, handler, role=role, owner=self.plugin)

    def register_dashboard_entry(self, entry_id: str, entry: Any) -> None:
        self._registry.register_dashboard_entry(entry_id, entry, owner=self.plugin)

    def register_prompt_provider(self, name: str, section: str, max_bytes: int,
                                 applies: Callable[..., bool], render: Callable[..., str], *, static: bool = False) -> None:
        self._registry.register_prompt_provider(name, section, max_bytes, applies, render, static=static, owner=self.plugin)

    def register_state_slot(self, slot_type: type) -> None:
        self._registry.register_state_slot(slot_type, owner=self.plugin)

    def register_scoped_action(
        self, action_type: type[StrEnum], validate_scope: Callable[..., bool],
        *, checkout_action: StrEnum | None = None,
        live_lease: Callable[..., Any] | None = None,
        checkout_tool: str | None = None,
        trusted_scope: TrustedScopeConstructors | None = None,
    ) -> None:
        self._registry.register_scoped_action(
            action_type, validate_scope, checkout_action=checkout_action,
            live_lease=live_lease, checkout_tool=checkout_tool,
            trusted_scope=trusted_scope, owner=self.plugin,
        )
