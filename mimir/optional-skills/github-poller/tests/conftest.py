"""Make ``scripts/poller.py`` importable for tests."""
from __future__ import annotations

import pytest

from github_poller_test_support import poller


@pytest.fixture(autouse=True)
def _trusted_pr_authors_by_default(monkeypatch: pytest.MonkeyPatch):
    """Keep legacy selection tests focused; trust-policy tests override this."""
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args: True)
