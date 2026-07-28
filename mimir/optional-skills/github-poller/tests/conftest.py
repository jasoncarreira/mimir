"""Make ``poller.py`` importable from the sibling skill dir for tests.

The skill ships as a flat directory (``poller.py`` lives next to
``SKILL.md`` and ``pollers.json``), not as a package, so tests need
to inject the parent dir onto ``sys.path`` before they can ``import
poller``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))


@pytest.fixture(autouse=True)
def _trusted_pr_authors_by_default(monkeypatch: pytest.MonkeyPatch):
    """Keep legacy selection tests focused; trust-policy tests override this."""
    import poller

    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args: True)
