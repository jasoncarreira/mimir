"""The changes-requested remediation prompt's verification contract.

The prompt tells a remediation turn how to verify its own fix. That guidance is
deployment-dependent: ``repo_test`` only exists when ``MIMIR_CODING_ENABLED`` is
true, and that defaults to false, so a prompt that names the tool
unconditionally misdirects a default deployment. These tests pin the contract in
both configurations, since the prompt is the only thing carrying it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_POLLER = (
    Path(__file__).resolve().parent.parent
    / "mimir" / "optional-skills" / "github-poller" / "poller.py"
)


@pytest.fixture(scope="module")
def poller():
    """Load the skill's poller.py the way production runs it: as a file."""
    spec = importlib.util.spec_from_file_location("github_poller_under_test", _POLLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_guidance_names_repo_test_only_when_coding_is_enabled(poller, monkeypatch):
    # Every truthy spelling registers the coding tools, so every one of them
    # must also advertise repo_test. `on` and `y` are the two that a narrower
    # 1/true/yes check silently got wrong.
    for value in ("1", "true", "yes", "on", "y", "TRUE", "On", " yes "):
        monkeypatch.setenv("MIMIR_CODING_ENABLED", value)
        assert "repo_test" in poller._verification_guidance(), value

    # ...and no falsy spelling may, since the tool is not registered.
    for value in ("", "false", "0", "no", "FALSE", "off", "n", "Off"):
        monkeypatch.setenv("MIMIR_CODING_ENABLED", value)
        assert "repo_test" not in poller._verification_guidance(), value

    # An unrecognised value must fall back to the default rather than guess.
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "maybe")
    assert "repo_test" not in poller._verification_guidance()


def test_env_flag_alphabet_matches_mimir_config(poller):
    """The duplicated boolean parser must not drift from the canonical one.

    ``poller.py`` cannot import ``mimir`` in production, so the alphabet is
    copied. This test runs where ``mimir`` IS importable and fails if the copy
    diverges — which is how the `on`/`y` gap was found in the first place.
    """
    from mimir.config import _ENV_BOOL_FALSY, _ENV_BOOL_TRUTHY

    assert poller._ENV_TRUTHY == _ENV_BOOL_TRUTHY
    assert poller._ENV_FALSY == _ENV_BOOL_FALSY


def test_env_flag_agrees_with_config_env_bool(poller, monkeypatch):
    """Same inputs, same answer — compared against the canonical parser."""
    from mimir.config import _env_bool

    for value in ("1", "true", "yes", "on", "y", "0", "false", "no", "off",
                  "n", "", "  TRUE  ", "maybe", "2"):
        monkeypatch.setenv("MIMIR_FLAG_UNDER_TEST", value)
        for default in (False, True):
            assert poller._env_flag("MIMIR_FLAG_UNDER_TEST", default) == _env_bool(
                "MIMIR_FLAG_UNDER_TEST", default
            ), (value, default)

    monkeypatch.delenv("MIMIR_FLAG_UNDER_TEST", raising=False)
    for default in (False, True):
        assert poller._env_flag("MIMIR_FLAG_UNDER_TEST", default) is default


def test_guidance_absent_flag_behaves_as_disabled(poller, monkeypatch):
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    guidance = poller._verification_guidance()
    assert "repo_test" not in guidance
    # Silence would let a turn present unverified work as verified.
    assert "not present changes as verified" in guidance


def test_enabled_guidance_is_runner_neutral_and_selector_accurate(poller, monkeypatch):
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "1")
    guidance = poller._verification_guidance()
    # ``project_tests`` forwards ``path::node_id`` to whatever runner
    # worklink.yaml configures; only pytest-style runners accept it.
    assert "only when the configured runner accepts" in guidance
    # Selectors are resolved strictly and symlinks are refused.
    assert "already exist" in guidance
    assert "symlink" in guidance
    assert "no flags" in guidance
    assert "whole suite" in guidance
    # No runner is named: this file ships to every deployment.
    for runner in ("pytest", "uv ", "npm ", "gradle", "cargo", "go test"):
        assert runner not in guidance, runner


def test_changes_requested_prompt_embeds_the_guidance(poller, monkeypatch):
    """The guidance must actually reach the emitted prompt, not just exist."""
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    emitted: list[str] = []
    monkeypatch.setattr(poller, "_emit", lambda prompt, **kw: emitted.append(prompt))

    pr = {
        "number": 42,
        "title": "a title",
        "html_url": "https://example.invalid/pr/42",
        "head": {"sha": "a" * 40, "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main", "sha": "b" * 40},
        "user": {"login": "self"},
    }
    review = {
        "state": "CHANGES_REQUESTED",
        "user": {"login": "reviewer"},
        "submitted_at": "2026-01-01T00:00:00Z",
    }

    def fake_gh_api(endpoint, token):
        return [review] if endpoint.endswith("/reviews") else [pr]

    monkeypatch.setattr(poller, "_gh_api", fake_gh_api)

    count, _state = poller._check_own_changes_requested("owner/repo", "token", "self", {})

    assert count == 1, "expected one reminder to be emitted"
    assert emitted, "no prompt was emitted"
    assert poller._verification_guidance() in emitted[0]
    assert "re-request review" in emitted[0]
