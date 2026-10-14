"""Build a frozen policy from core and explicitly selected installed plugins."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from .core import register_core_tools, scoped_policy_call
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

    @scoped_policy_call(lambda bound: bound.arguments["self"].policy)
    async def startup_preflight(self, bundle: Any) -> None:
        for plugin in self.plugins:
            name = plugin.candidate.name
            try:
                hook = getattr(plugin.contributor, "startup_preflight", None)
                if hook is None:
                    continue
                result = hook(bundle)
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    raise ValueError("preflight returned False")
            except Exception as exc:
                raise RuntimeError(f"plugin {name!r} startup preflight failed") from exc

    @scoped_policy_call(lambda bound: bound.arguments["self"].policy)
    async def turn_preflight(self, event: Any, auth_context: Any) -> None:
        for plugin in self.plugins:
            name = plugin.candidate.name
            try:
                hook = getattr(plugin.contributor, "turn_preflight", None)
                if hook is None:
                    continue
                result = hook(event, auth_context)
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    raise ValueError("preflight returned False")
            except Exception as exc:
                raise RuntimeError(f"plugin {name!r} turn preflight failed") from exc

    @scoped_policy_call(lambda bound: bound.arguments["self"].policy)
    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        for plugin in self.plugins:
            name = plugin.candidate.name
            try:
                hook = getattr(plugin.contributor, "shutdown", None)
                if hook is None:
                    continue
                if inspect.iscoroutinefunction(hook):
                    await asyncio.wait_for(hook(), timeout=5)
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(hook), timeout=5)
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=5)
            except Exception:
                # One plugin cannot prevent later hooks or the server's teardown.
                log.exception("plugin %s shutdown failed", name)

    @scoped_policy_call(lambda bound: bound.arguments["self"].policy)
    async def health(self) -> dict[str, object]:
        fields: dict[str, object] = {}
        for plugin in self.plugins:
            name = plugin.candidate.name
            try:
                hook = getattr(plugin.contributor, "health", None)
                if hook is None:
                    continue
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
                json.dumps(value, allow_nan=False)
                fields[name] = value
            except Exception:
                fields[name] = {"error": "health contributor failed"}
        return fields


def load_plugins(*, enable: str) -> PluginManager:
    """Resolve metadata before importing only selected providers, then freeze once."""
    selected = enabled_plugins(enable=enable)
    # Selection resolves ambiguous nested environment prefixes before any provider
    # is imported. The most specific selected prefix owns a key, whether or not
    # that plugin declares the key itself (foo must not claim foo-bar's secrets).
    prefixes = sorted(
        ((f"MIMIR_PLUGIN_{candidate.name.upper().replace('-', '_')}_", candidate.name)
         for candidate in selected),
        key=lambda item: len(item[0]), reverse=True,
    )
    for candidate in selected:
        for key in getattr(getattr(candidate, "manifest", None), "config", {}):
            owner = next((owner for prefix, owner in prefixes if key.startswith(prefix)), None)
            if owner != candidate.name:
                raise PluginDiscoveryError(
                    f"plugin {candidate.name!r}: config key {key!r} belongs to selected plugin {owner!r}"
                )
    defaults = [(candidate.name, skin) for candidate in selected
                for skin in getattr(getattr(candidate, "manifest", None), "skin_defaults", ())]
    if len(defaults) > 1 and not os.environ.get("MIMIR_WEB_DEFAULT_SKIN"):
        raise PluginDiscoveryError("conflicting plugin default skins: " +
                                   ", ".join(f"{owner}:{skin}" for owner, skin in defaults))
    policy = PolicyRegistry()
    register_core_tools(policy)
    policy.select_plugin_candidates(selected)
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
    for plugin in loaded:
        if plugin.candidate.name != "cultivate":
            continue
        try:
            projection = getattr(plugin.contributor, "host_membership_projection", None)
            memberships = projection() if callable(projection) else {}
            policy._project_first_party_memberships(plugin.candidate, memberships)
        except Exception as exc:
            raise PluginDiscoveryError(
                f"plugin {plugin.candidate.name!r}: membership projection failed: {exc}"
            ) from exc
    try:
        policy.freeze()
    except Exception as exc:
        # Principal triggers use plugin:<owner>:<local>; splitting at the first
        # colon loses their identity and wrongly blames every selected provider.
        message = str(exc)
        subject = next(
            (trigger for trigger in policy.service_principals
             if message.startswith(f"{trigger}:")),
            message.split(":", 1)[0],
        )
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
