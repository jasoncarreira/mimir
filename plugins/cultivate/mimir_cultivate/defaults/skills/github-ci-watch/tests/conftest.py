"""Test configuration for github-ci-watch."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def age_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_CI_MAX_AGE_DAYS", raising=False)
    monkeypatch.delenv("GITHUB_CI_MAX_AGE_DAYS_BY_REPO", raising=False)
