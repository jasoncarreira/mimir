"""Integration: every delivered tracker_replay scenario carries a core-safe cutoff event id.

The replay harness never checks the shape of the cutoff event id, so an adapter that
leaked a raw provider comment id would pass every contract scenario. This asserts, for
all three adapters, that the cutoff id of each delivered attest scenario survives
core's ``safe_event_id`` unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from mimir import pollers
from mimir.poller_tracker import safe_event_id
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.thread import delivery_decision
from tracker_replay import (  # noqa: F401  (opt-in fixture)
    ReplayTransport,
    forbid_default_transport,
    scenarios_for,
)

SETUPS: dict[str, tuple[TrackerAdapterConfig, dict[str, str], str]] = {
    "github_issues": (
        TrackerAdapterConfig(name="github_issues", repos=("acme/widgets",)),
        {"GITHUB_TOKEN": "tok", "MIMIR_GITHUB_SELF_LOGIN": "MimirBot"},
        "acme/widgets#42",
    ),
    "linear": (TrackerAdapterConfig(name="linear"), {"LINEAR_API_KEY": "lin_oauth_test"}, "ENG-123"),
    "jira": (
        TrackerAdapterConfig(
            name="jira", account_id="acct-bot", projects=("PROJ",), trusted_groups=("jira-trusted",)
        ),
        {"JIRA_SITE": "acme.atlassian.net", "JIRA_EMAIL": "bot@acme.test", "JIRA_API_TOKEN": "token"},
        "PROJ-45",
    ),
}

DELIVERED = [
    (adapter, scenario)
    for adapter in SETUPS
    for scenario in scenarios_for(adapter)
    if scenario.operation == "attest" and scenario.expectation(adapter).get("decision") == "deliver"
]


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, forbid_default_transport: None) -> None:  # noqa: F811
    trusted = {"alice": True, "mallory": False}
    monkeypatch.setattr(
        pollers, "_github_author_is_trusted", lambda repo, author, token, *, timeout=None: trusted.get(author)
    )


def test_every_adapter_has_a_delivered_scenario() -> None:
    assert {adapter for adapter, _ in DELIVERED} == set(SETUPS)


@pytest.mark.parametrize("adapter,scenario", DELIVERED, ids=lambda v: v if isinstance(v, str) else v.name)
def test_delivered_cutoff_id_is_a_safe_event_id(adapter: str, scenario: Any) -> None:
    config, env, ref = SETUPS[adapter]
    merged = {"merged": "Done"} if adapter in scenario.merged_mapping else {}
    if merged:
        config = TrackerAdapterConfig(**{**config.__dict__, "transitions": merged})
    built = build_adapter(adapter, config, env, ReplayTransport.from_fixture(adapter, scenario.name))
    decision = delivery_decision(built.read(ref), built.trust)
    assert decision.kind == "deliver" and decision.cutoff is not None
    assert safe_event_id(decision.cutoff.id) == decision.cutoff.id
