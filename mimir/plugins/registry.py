"""Frozen declarations used to assemble core and enabled-plugin tool policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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


_FLOW = frozenset({"source", "sink", "both", "neither"})
_TIERS = frozenset({"scope-contained", "scoped-with-provenance", "code-execution", "unbounded"})
_OPERATIONS = frozenset({"open", "resource_scoped", "admin_required"})
_SCOPES = frozenset({"channel", "repo_pr"})
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
        self._profiles: dict[str, AuthorityProfile] = {}
        self._shell_profiles: dict[str, str] = {}
        self._principals: dict[str, Any] = {}
        self._principal_owners: dict[str, str] = {}
        self._frozen = False

    def registrar(self, plugin: str) -> PolicyRegistrar:
        self._writable()
        return PolicyRegistrar(self, _name(plugin, "plugin"))

    def _writable(self) -> None:
        if self._frozen:
            raise PolicyRegistrationError("policy registry is frozen")

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
    def profiles(self) -> Mapping[str, AuthorityProfile]:
        return MappingProxyType(self._profiles)

    @property
    def shell_profile_mappings(self) -> Mapping[str, str]:
        return MappingProxyType(self._shell_profiles)

    @property
    def service_principals(self) -> Mapping[str, Any]:
        return MappingProxyType(self._principals)

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
        if owner != "core" and (operation_class == "resource_scoped") != (resource_scope is not None):
            raise PolicyRegistrationError(f"{name}: resource_scoped requires explicit channel or repo_pr scope")
        if not isinstance(result_origin, ResultOriginKind) or not isinstance(sink_category, (SinkCategory, type(None))):
            raise PolicyRegistrationError(f"{name}: invalid origin or sink category")
        if not isinstance(sink_destination, (str, type(None))) or (
            owner != "core" and sink_category is not None and not sink_destination
        ):
            raise PolicyRegistrationError(f"{name}: invalid sink destination")
        if (sink_target_extractor is _TARGET_EXTRACTOR_UNDECLARED and owner != "core") or (
            sink_target_extractor is not None
            and sink_target_extractor is not _TARGET_EXTRACTOR_UNDECLARED
            and not callable(sink_target_extractor)
        ):
            raise PolicyRegistrationError(f"{name}: invalid target extractor")
        if (sink_payload_extractor is _PAYLOAD_EXTRACTOR_UNDECLARED and owner != "core") or (
            sink_payload_extractor is not None
            and sink_payload_extractor is not _PAYLOAD_EXTRACTOR_UNDECLARED
            and not callable(sink_payload_extractor)
        ):
            raise PolicyRegistrationError(f"{name}: invalid payload extractor")
        if not isinstance(writes_code_or_state, bool) or not isinstance(read_backend, bool):
            raise PolicyRegistrationError(f"{name}: invalid boolean metadata")
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
            shell_process, declared_memberships,
        )

    def register_capability(self, name: str, *, owner: str = "core", tier: str | None = None) -> None:
        self._writable()
        name = _name(name, "capability")
        if name in self._capabilities:
            raise PolicyRegistrationError(f"duplicate capability: {name}")
        if tier is not None:
            if tier not in _TIERS or owner != "core":
                raise PolicyRegistrationError(f"{owner}: capability {name}: invalid core-only tier")
            tool = self._tools.get(name)
            if tool is not None and tool.tier is not None and tool.tier != tier:
                raise PolicyRegistrationError(
                    f"{owner}: capability {name}: tier {tier} conflicts with tool tier {tool.tier}"
                )
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
        self._shell_profiles[profile] = _name(shell_profile, "shell profile")

    def register_service_principal(self, principal: Any, *, owner: str = "core") -> None:
        self._writable()
        from mimir.access_control import ServicePrincipal

        if not isinstance(principal, ServicePrincipal):
            raise PolicyRegistrationError(f"{owner}: expected frozen ServicePrincipal")
        trigger = _name(getattr(principal, "trigger", None), "principal trigger")
        canonical = _name(getattr(principal, "canonical", None), "principal canonical")
        if trigger in self._principals or any(p.canonical == canonical for p in self._principals.values()):
            raise PolicyRegistrationError(f"duplicate service principal: {trigger} ({canonical})")
        self._principals[trigger] = principal
        self._principal_owners[trigger] = owner

    def freeze(self) -> PolicyRegistry:
        self._writable()
        from mimir.models import RepoPRAction
        from mimir.tool_descriptors import validate_tool_descriptors

        validate_tool_descriptors(self.descriptors)
        for name, tool in self._tools.items():
            for profile in tool.profiles:
                if profile not in self._profiles or name not in self._profiles[profile].capabilities:
                    raise PolicyRegistrationError(f"{name}: missing profile membership {profile}")
            if tool.typed_action is not None and not isinstance(tool.typed_action, RepoPRAction):
                raise PolicyRegistrationError(f"{name}: invalid repo/PR action {tool.typed_action!r}")
            if tool.owner != "core" and tool.resource_scope == "repo_pr" and tool.typed_action is None:
                raise PolicyRegistrationError(f"{name}: repo_pr scope requires typed action")
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
            if not profile.bounded_capabilities <= profile.capabilities:
                raise PolicyRegistrationError(f"{name}: bounded capability outside profile")
            for capability, companions in profile.capability_companions.items():
                if capability not in profile.capabilities or not companions <= profile.capabilities:
                    raise PolicyRegistrationError(f"{name}: invalid companion capabilities for {capability}")
        for profile in self._shell_profiles:
            if profile not in self._profiles:
                raise PolicyRegistrationError(f"unknown shell authority profile: {profile}")
        for trigger, principal in self._principals.items():
            profile = getattr(principal, "authority_profile", None)
            if profile is not None and profile not in self._profiles:
                raise PolicyRegistrationError(f"{trigger}: unknown authority profile {profile}")
            if self._principal_owners[trigger] != "core":
                if profile is None or not set(principal.capabilities) <= self._profiles[profile].capabilities:
                    raise PolicyRegistrationError(f"{trigger}: principal capabilities outside profile")
        self._frozen = True
        return self


class PolicyRegistrar:
    """A plugin's restricted registration surface; it cannot edit core records."""

    def __init__(self, registry: PolicyRegistry, plugin: str) -> None:
        self._registry = registry
        self.plugin = plugin

    def register_tool(self, **kwargs: Any) -> None:
        if "owner" in kwargs:
            raise PolicyRegistrationError(f"{self.plugin}: owner cannot be overridden")
        self._registry.register_tool(owner=self.plugin, **kwargs)

    def register_capability(self, name: str) -> None:
        self._registry.register_capability(name, owner=self.plugin)

    def register_authority_profile(self, name: str, **kwargs: Any) -> None:
        if "owner" in kwargs:
            raise PolicyRegistrationError(f"{self.plugin}: owner cannot be overridden")
        self._registry.register_authority_profile(name, owner=self.plugin, **kwargs)

    def add_to_profile(self, profile: str, tool: str) -> None:
        registry = self._registry
        registry._writable()
        if tool not in registry._tools or registry._tools[tool].owner != self.plugin:
            raise PolicyRegistrationError(f"{self.plugin}: cannot add foreign tool {tool} to {profile}")
        if profile not in registry._profiles:
            raise PolicyRegistrationError(f"unknown profile: {profile}")
        current = registry._profiles[profile]
        registry._profiles[profile] = replace(current, capabilities=current.capabilities | {tool})
        registered = registry._tools[tool]
        registry._tools[tool] = replace(registered, profiles=registered.profiles | {profile})

    def register_shell_profile_mapping(self, profile: str, shell_profile: str) -> None:
        if profile not in self._registry._profiles or self._registry._profiles[profile].owner != self.plugin:
            raise PolicyRegistrationError(f"{self.plugin}: cannot map foreign profile {profile}")
        self._registry.register_shell_profile_mapping(profile, shell_profile, owner=self.plugin)

    def register_service_principal(self, principal: Any) -> None:
        self._registry.register_service_principal(principal, owner=self.plugin)
