"""Build a frozen policy from core and explicitly selected installed plugins."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from .core import register_core_tools
from .discovery import PluginCandidate, PluginDiscoveryError, enabled_plugins
from .registry import PolicyRegistry

log = logging.getLogger(__name__)


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
        self._shutdown_started = False

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

    async def startup_preflight(self, bundle: Any) -> None:
        for name, hook in self.hooks("startup_preflight"):
            try:
                result = hook(bundle)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                raise RuntimeError(f"plugin {name!r} startup preflight failed") from exc

    async def turn_preflight(self, event: Any, auth_context: Any) -> None:
        for name, hook in self.hooks("turn_preflight"):
            try:
                result = hook(event, auth_context)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                raise RuntimeError(f"plugin {name!r} turn preflight failed") from exc

    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        for name, hook in self.hooks("shutdown"):
            try:
                if inspect.iscoroutinefunction(hook):
                    await asyncio.wait_for(hook(), timeout=5)
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(hook), timeout=5)
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=5)
            except Exception:
                # One plugin cannot prevent later hooks or the server's teardown.
                log.exception("plugin %s shutdown failed", name)

    async def health(self) -> dict[str, object]:
        fields: dict[str, object] = {}
        for name, hook in self.hooks("health"):
            try:
                if inspect.iscoroutinefunction(hook):
                    value = await asyncio.wait_for(hook(), timeout=5)
                else:
                    value = await asyncio.wait_for(asyncio.to_thread(hook), timeout=5)
                    if inspect.isawaitable(value):
                        value = await asyncio.wait_for(value, timeout=5)
                if not isinstance(value, dict) or any(
                    not isinstance(key, str) or not key.isidentifier()
                    or not isinstance(item, (bool, int, float))
                    for key, item in value.items()
                ):
                    raise ValueError("health fields must be named numeric or boolean values")
                fields[name] = value
            except Exception:
                fields[name] = {"error": "health contributor failed"}
        return fields


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
        subject = str(exc).split(":", 1)[0]
        tool = policy.tools.get(subject)
        profile = policy.profiles.get(subject)
        owner = (tool.owner if tool is not None else
                 profile.owner if profile is not None else
                 policy.capabilities.get(subject) or policy._principal_owners.get(subject))
        if owner is not None and owner != "core":
            names = (owner,)
        else:
            names = tuple(candidate.name for candidate in selected)
        raise PluginDiscoveryError(
            f"enabled plugin(s) {', '.join(repr(name) for name in names)}: "
            f"policy validation failed: {exc}"
        ) from exc
    return PluginManager(policy, tuple(loaded))
