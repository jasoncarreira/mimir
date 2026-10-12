"""Selected plugin factories join the model surface without changing core defaults."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.access_control import CapabilityMatrixError, assert_model_tool_inventory_cataloged
from mimir.plugins.core import register_core_tools
from mimir.plugins.registry import PolicyRegistry
from mimir.tool_descriptors import ResultOriginKind
from mimir.tools.registry import all_mimir_tools


def _policy(factory, *, result_domain="plugin-data"):
    registry = PolicyRegistry()
    register_core_tools(registry)
    registry.registrar("sample").register_tool(
        name="sample_read", factory=factory, flow="source", tier="scope-contained",
        operation_class="open", result_domain=result_domain,
        result_origin=ResultOriginKind.NONE, sink_category=None,
        sink_destination=None, sink_target_extractor=None,
        sink_payload_extractor=None, writes_code_or_state=False, profiles=(),
        read_backend=False,
    )
    return registry.freeze()


def test_selected_factory_is_not_invoked_by_enforced_inventory_and_is_assembled_once():
    calls = []

    def factory():
        calls.append(True)
        return SimpleNamespace(name="sample_read")

    policy = _policy(factory)
    assert_model_tool_inventory_cataloged(policy=policy)
    assert calls == []
    tools = all_mimir_tools(policy=policy)
    assert calls == [True]
    assert [tool.name for tool in tools].count("sample_read") == 1
    assert "sample_read" not in {tool.name for tool in all_mimir_tools()}
    assert policy.tools["sample_read"].result_domain == "plugin-data"
    assert policy.tools["sample_read"].descriptor.result_origin == ResultOriginKind.NONE


def test_plugin_factory_must_match_declared_identity():
    policy = _policy(lambda: SimpleNamespace(name="other"))
    with pytest.raises(ValueError, match="plugin sample: factory for sample_read returned 'other'"):
        all_mimir_tools(policy=policy)


def test_enforced_inventory_rejects_unclassified_plugin_result():
    policy = _policy(lambda: SimpleNamespace(name="sample_read"), result_domain=None)
    with pytest.raises(CapabilityMatrixError, match="plugin sample: tool sample_read has incomplete model policy"):
        assert_model_tool_inventory_cataloged(policy=policy)
