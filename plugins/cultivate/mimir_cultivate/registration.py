"""Register Cultivate contributions when the provider is selected."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


class CultivateProvider:
    def register(self, registrar: PolicyRegistrar) -> None:
        """The package skeleton has no contributions until their resources ship."""


provider = CultivateProvider()
