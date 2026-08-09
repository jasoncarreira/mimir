"""Make ``scripts/poller.py`` importable for tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _trusted_pr_authors_by_default(monkeypatch: pytest.MonkeyPatch):
    """Keep legacy selection tests focused; trust-policy tests override this."""
    import poller

    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args: True)
