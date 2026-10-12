"""Core tool declarations for the frozen policy registry."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import PolicyRegistry


def register_core_tools(registry: PolicyRegistry) -> None:
    """Register core descriptors; later policy families add their own metadata here."""
    from mimir.tool_descriptors import _CORE_DESCRIPTOR_DECLARATIONS

    for name, descriptor in _CORE_DESCRIPTOR_DECLARATIONS.items():
        registry.register_tool(
            name=name,
            policy_only=True,
            flow=None,
            tier=None,
            operation_class=None,
            result_domain=None,
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
            profiles=(),
            read_backend=False,
        )


@lru_cache(maxsize=1)
def core_policy_registry() -> PolicyRegistry:
    from .registry import PolicyRegistry

    registry = PolicyRegistry()
    register_core_tools(registry)
    return registry.freeze()
