"""Build a frozen policy from core and explicitly selected installed plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import register_core_tools
from .discovery import PluginCandidate, PluginDiscoveryError, enabled_plugins
from .registry import PolicyRegistry


@dataclass(frozen=True)
class LoadedPlugin:
    candidate: PluginCandidate
    contributor: Any


class PluginManager:
    """Runtime policy holder; not a Config-serializable dataclass field value."""

    def __init__(self, policy: PolicyRegistry, plugins: tuple[LoadedPlugin, ...]) -> None:
        if not policy.frozen:
            raise ValueError("plugin policy must be frozen")
        self.policy = policy
        self.plugins = plugins

    def __repr__(self) -> str:
        return "<plugin runtime policy>"

    def __deepcopy__(self, memo: dict[int, Any]) -> PluginManager:
        # The frozen policy contains mapping proxies and provider callables;
        # copying it for a Config snapshot would execute neither safely.
        return self

    @property
    def tools(self) -> tuple[Any, ...]:
        return tuple(tool.factory for tool in self.policy.tools.values()
                     if tool.owner != "core" and tool.factory is not None)

    def hooks(self, name: str) -> tuple[tuple[str, Any], ...]:
        return tuple((plugin.candidate.name, hook)
                     for plugin in self.plugins
                     if (hook := getattr(plugin.contributor, name, None)) is not None)


def load_plugins(*, enable: str) -> PluginManager:
    """Resolve metadata before importing only selected providers, then freeze once."""
    selected = enabled_plugins(enable=enable)
    policy = PolicyRegistry()
    register_core_tools(policy)
    loaded = []
    for candidate in selected:
        try:
            contributor = candidate.entry_point.load()
            register = getattr(contributor, "register", None)
            if not callable(register):
                raise TypeError("provider must expose register(registrar)")
            register(policy.registrar(candidate.name))
        except Exception as exc:
            raise PluginDiscoveryError(
                f"plugin {candidate.name!r} from {candidate.distribution}: registration failed: {exc}"
            ) from exc
        loaded.append(LoadedPlugin(candidate, contributor))
    try:
        policy.freeze()
    except Exception as exc:
        raise PluginDiscoveryError(f"plugin policy validation failed: {exc}") from exc
    return PluginManager(policy, tuple(loaded))
