"""Make ``scripts/poller.py`` importable for tests."""
from __future__ import annotations

import pytest

from github_poller_test_support import poller


@pytest.fixture(autouse=True)
def _trusted_pr_authors_by_default(monkeypatch: pytest.MonkeyPatch):
    """Keep legacy selection tests focused; trust-policy tests override this."""
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args: True)


@pytest.fixture(autouse=True)
def _no_leaked_tick_budget():
    """Clear the module-level tick budget around every test.

    ``_gh_api`` clamps its subprocess timeout from this handle, so a budget left
    installed by a test that raised would silently shorten timeouts in unrelated
    tests. Reset both before and after rather than trusting the test to clean up.
    """
    poller.set_active_tick_budget(None)
    yield
    poller.set_active_tick_budget(None)
