"""Tracker tool policy: closed-world declarations, descriptor shape and authorization."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.plugins.manager import load_plugins
from mimir.tool_descriptors import ResultOriginKind, SinkCategory
from mimir_cultivate import policy

TRACKER = sorted(policy.TRACKER_TOOL_NAMES)
PAYLOAD_ARGUMENT = {
    "tracker_file_work": "description", "tracker_comment": "body", "tracker_set_state": "state",
}


@pytest.fixture(scope="module")
def selected():
    return load_plugins(enable="cultivate").policy


def test_name_sets_are_pinned() -> None:
    assert policy.TRACKER_TOOL_NAMES == frozenset(PAYLOAD_ARGUMENT)
    assert policy.CULTIVATE_TOOL_NAMES == policy.CODING_TOOLS | policy.TRACKER_TOOL_NAMES
    assert len(policy.CODING_TOOLS) == 40
    assert not policy.CODING_TOOLS & policy.TRACKER_TOOL_NAMES


@pytest.mark.parametrize("name", TRACKER)
def test_registered_descriptor_shape(name, selected) -> None:
    tool = selected.tools[name]
    assert tool.owner == "cultivate"
    assert tool.flow == "sink" and tool.tier == "scoped-with-provenance"
    assert tool.operation_class == "resource_scoped" and tool.resource_scope == "tracker_issue"
    assert tool.typed_action is None and tool.result_domain is None
    assert tool.writes_code_or_state is True
    assert tool.descriptor.sink_category == SinkCategory.FORGE
    assert tool.descriptor.sink_destination == "bound_tracker_issue"
    assert tool.descriptor.result_origin == ResultOriginKind.NON_INGESTING
    assert tool.memberships == frozenset()
    assert tool.profiles == frozenset({"github"})
    assert not tool.read_backend and tool.readable_domain is None
    assert tool.factory().name == name


@pytest.mark.parametrize("name", TRACKER)
def test_payload_extractor_reads_the_declared_argument(name, selected) -> None:
    extractor = selected.tools[name].descriptor.sink_payload_extractor
    arguments = {"description": "D", "body": "B", "state": "S", "external_ref": "linear:ENG-1"}
    assert extractor(name, arguments, None) == (arguments[PAYLOAD_ARGUMENT[name]],)


@pytest.mark.parametrize("name", TRACKER)
def test_target_extractor_binds_only_the_attested_issue(name, selected) -> None:
    extractor = selected.tools[name].descriptor.sink_target_extractor
    context = SimpleNamespace(tracker_issue_targets=frozenset({
        ("linear", "ENG-1", "https://linear.app/acme/issue/ENG-1"),
    }))
    assert extractor(name, {"external_ref": "linear:ENG-1"}, context) == ("tracker:linear:ENG-1",)
    for ref in ("linear:ENG-2", "jira:ENG-1", None, 7):
        assert extractor(name, {"external_ref": ref}, context) == (None,)
    assert extractor(name, {"external_ref": "linear:ENG-1"}, SimpleNamespace()) == (None,)


def test_custom_and_heartbeat_profiles_exclude_tracker_tools(selected) -> None:
    for name in TRACKER:
        assert "custom" not in selected.tools[name].profiles
        assert "heartbeat" not in selected.tools[name].profiles


def test_tracker_tools_are_in_no_other_table() -> None:
    for name in TRACKER:
        assert name not in policy._ACTIONS and name not in policy._RESULT_DOMAIN
        assert name not in policy._HEARTBEAT
        assert all(name not in names for names in policy._MEMBERSHIPS.values())
        assert name in policy._WRITES


def test_module_routing_materializes_the_real_tools() -> None:
    from mimir_cultivate.tracker.tools import TRACKER_TOOLS

    assert {tool.name for tool in TRACKER_TOOLS} == policy.TRACKER_TOOL_NAMES
    for name in TRACKER:
        assert policy._factory("mimir_cultivate.tracker.tools", name)().name == name
