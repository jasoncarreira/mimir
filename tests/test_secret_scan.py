"""Tests for mimir.secret_scan (commit-time secret detection)."""

from __future__ import annotations

import pytest

from mimir.secret_scan import contains_secret


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "a" * 36,
        "sk-ant-" + "A1b2" * 6,
        "sk-" + "A" * 24,
        "AKIA" + "A" * 16,
        "ASIA" + "0" * 16,
        "xoxb-" + "0123456789abcdef0123",
        'config = {"refresh_token": "' + "x" * 30 + '"}',
        "Authorization: Bearer " + "z" * 30,
        "github_pat_" + "A" * 60,
    ],
)
def test_contains_secret_matches_high_signal_shapes(text: str) -> None:
    assert contains_secret(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Low-signal / placeholder shapes the log redactor would flag but the
        # commit-refusal gate must NOT (they block benign generated content).
        "token=YOUR_TOKEN_HERE",
        "password=changeme",
        "api_key=<set-me>",
        "export TOKEN=$MY_TOKEN",
        # Short / non-credential lookalikes below the length floors.
        "ghp_short",
        "sk-foo-bar",  # wiki-slug shape (has hyphens; not base62 body)
        "AKIA123",  # too short
        "just some normal prose about tokens and passwords",
    ],
)
def test_contains_secret_ignores_low_signal_and_placeholders(text: str) -> None:
    assert contains_secret(text) is False
