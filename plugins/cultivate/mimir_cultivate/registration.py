"""Register Cultivate contributions when the provider is selected."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


class CultivateProvider:
    def register(self, registrar: PolicyRegistrar) -> None:
        from .policy import register_action_protocol, validate_scoped_pr_action

        register_action_protocol(registrar, validate_scoped_pr_action)


provider = CultivateProvider()
