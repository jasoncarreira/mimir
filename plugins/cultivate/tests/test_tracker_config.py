from __future__ import annotations

import pytest
import yaml

from mimir_cultivate.tracker.config import (
    TrackerConfig,
    configured_states,
    parse_tracker_config,
    resolve_transition,
)
from mimir_cultivate.tracker.record import ExternalRecord
from mimir_cultivate.worklink.backends.registry import WorklinkConfig

JIRA_BASE = {
    "account_id": "abc123",
    "projects": ["PROJ"],
    "trusted_groups": ["devs"],
}
LINEAR_REC = ExternalRecord("linear", "ENG-1", "https://linear.app/a/issue/ENG-1")
JIRA_REC = ExternalRecord("jira", "PROJ-45", "https://x.atlassian.net/browse/PROJ-45")
GITHUB_REC = ExternalRecord("github_issues", "acme/app#1", "https://github.com/acme/app/issues/1")


def test_absent_section_is_disabled_default() -> None:
    assert parse_tracker_config(None, "acme/app") == TrackerConfig()
    assert parse_tracker_config({}, None).adapters == {}
    assert parse_tracker_config(None, None).close_on_merge is False


def test_github_repos_default_to_repository() -> None:
    config = parse_tracker_config({"adapters": {"github_issues": None}}, "acme/app")

    assert config.adapters["github_issues"].repos == ("acme/app",)


def test_full_config_parses() -> None:
    config = parse_tracker_config(
        {
            "close_on_merge": True,
            "adapters": {
                "github_issues": {"repos": ["acme/app", "acme/lib"]},
                "linear": {"team_keys": ["ENG"], "transitions": {"merged": "Done"}},
                "jira": {**JIRA_BASE, "trusted_project_roles": ["Developers"]},
            },
        },
        None,
    )

    assert config.close_on_merge is True
    assert config.adapters["github_issues"].repos == ("acme/app", "acme/lib")
    assert config.adapters["linear"].team_keys == ("ENG",)
    assert config.adapters["jira"].account_id == "abc123"
    assert config.adapters["jira"].trusted_project_roles == ("Developers",)


@pytest.mark.parametrize(
    ("data", "repository"),
    [
        ("nope", None),
        ({"surprise": 1}, None),
        ({"close_on_merge": "yes"}, None),
        ({"adapters": []}, None),
        ({"adapters": {"gitlab": {}}}, None),
        ({"adapters": {"github_issues": []}}, "acme/app"),
        ({"adapters": {"github_issues": {}}}, None),
        ({"adapters": {"github_issues": {"repos": "acme/app"}}}, None),
        ({"adapters": {"github_issues": {"repos": ["bad slug"]}}}, None),
        ({"adapters": {"github_issues": {"repos": [3]}}}, None),
        ({"adapters": {"github_issues": {"transitions": {"merged": "Done"}}}}, "a/b"),
        ({"adapters": {"github_issues": {"project_transitions": {}}}}, "a/b"),
        ({"adapters": {"github_issues": {"other": 1}}}, "a/b"),
        ({"adapters": {"linear": {"team_keys": ["eng"]}}}, None),
        ({"adapters": {"linear": {"team_keys": "ENG"}}}, None),
        ({"adapters": {"linear": {"other": 1}}}, None),
        ({"adapters": {"linear": {"transitions": {"closed": "Done"}}}}, None),
        ({"adapters": {"linear": {"transitions": {"merged": ""}}}}, None),
        ({"adapters": {"linear": {"transitions": {"merged": "x" * 65}}}}, None),
        ({"adapters": {"linear": {"transitions": {"merged": "a\nb"}}}}, None),
        ({"adapters": {"linear": {"transitions": {"merged": 3}}}}, None),
        ({"adapters": {"linear": {"transitions": []}}}, None),
        ({"adapters": {"linear": {"project_transitions": {"eng": {}}}}}, None),
        ({"adapters": {"linear": {"project_transitions": {"ENG": {"x": "y"}}}}}, None),
        ({"adapters": {"jira": {k: v for k, v in JIRA_BASE.items() if k != "account_id"}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "account_id": ""}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "projects": []}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "projects": ["p"]}}}, None),
        ({"adapters": {"jira": {k: v for k, v in JIRA_BASE.items() if k != "projects"}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "trusted_groups": []}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "trusted_groups": [""]}}}, None),
        ({"adapters": {"jira": {**JIRA_BASE, "other": 1}}}, None),
    ],
)
def test_rejections(data: object, repository: str | None) -> None:
    with pytest.raises(ValueError):
        parse_tracker_config(data, repository)


def test_resolve_transition_prefers_project_then_global_then_unset() -> None:
    config = parse_tracker_config(
        {
            "adapters": {
                "linear": {
                    "transitions": {"merged": "Done", "pr_opened": "In Review"},
                    "project_transitions": {"ENG": {"merged": "Shipped"}},
                },
                "jira": JIRA_BASE,
            }
        },
        None,
    )
    other = ExternalRecord("linear", "OPS-2", "https://linear.app/a/issue/OPS-2")

    assert resolve_transition(config, LINEAR_REC, "merged") == "Shipped"
    assert resolve_transition(config, LINEAR_REC, "pr_opened") == "In Review"
    assert resolve_transition(config, other, "merged") == "Done"
    assert resolve_transition(config, LINEAR_REC, "filed") is None
    assert resolve_transition(config, JIRA_REC, "merged") is None
    assert resolve_transition(config, GITHUB_REC, "merged") is None
    assert configured_states(config, LINEAR_REC) == {"Shipped", "In Review"}
    assert configured_states(config, other) == {"Done", "In Review"}
    assert configured_states(config, GITHUB_REC) == frozenset()


def test_worklink_config_loads_tracker_section(tmp_path) -> None:
    path = tmp_path / "worklink.yaml"
    path.write_text(
        yaml.safe_dump(
            {"repository": "Acme/App", "tracker": {"adapters": {"github_issues": {}}}}
        )
    )

    config = WorklinkConfig.load(path)

    assert config.tracker.adapters["github_issues"].repos == ("acme/app",)
    assert WorklinkConfig().tracker == TrackerConfig()


def test_worklink_config_rejects_bad_tracker_section(tmp_path) -> None:
    path = tmp_path / "worklink.yaml"
    path.write_text(yaml.safe_dump({"tracker": {"adapters": {"gitlab": {}}}}))

    with pytest.raises(ValueError, match="unknown keys"):
        WorklinkConfig.load(path)
