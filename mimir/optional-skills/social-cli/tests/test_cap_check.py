"""Tests for ``cap_check.py`` — the archive/ledger cross-check.

The two properties worth pinning are both about *placement*, because this
script resolves both the agent home and its ``count.py`` delegate from
``__file__``. It was written when it sat at the skill root; moving it into
``scripts/`` changes both, and getting either wrong degrades the cap check
silently — it still exits 0 and prints a number, just a wrong one.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def fresh_cap_check():
    sys.modules.pop("cap_check", None)
    return importlib.import_module("cap_check")


def test_count_delegate_sits_beside_this_script() -> None:
    """``count.py`` must be resolvable from cap_check's own directory.

    ``count_ledger`` runs ``Path(__file__).parent / "count.py"``. When a stray
    root-level copy of ``count.py`` was removed from an install, this lookup
    started failing — and the failure surfaced as ``ledger=0`` with exit 0, so
    the cap check silently became archive-only and would under-report posts
    against the daily cap.
    """
    module = fresh_cap_check()
    beside = Path(module.__file__).resolve().parent / "count.py"
    assert beside.is_file(), f"count.py must sit beside cap_check.py, looked at {beside}"


def test_home_fallback_resolves_to_the_agent_home(monkeypatch) -> None:
    """With MIMIR_HOME unset, the fallback must reach the home, not skills/.

    The script lives at ``<home>/skills/social-cli/scripts/cap_check.py``, so the
    walk up is four levels. At three — correct when it sat at the skill root —
    it resolves to ``skills/`` and every archive glob silently finds nothing.
    """
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    module = fresh_cap_check()
    home = module._home()
    script = Path(module.__file__).resolve()
    assert home == script.parents[3]
    assert (home / "skills").is_dir() or home.name != "skills", (
        f"fallback resolved to {home}, which looks like the skills dir rather "
        "than the agent home"
    )


def test_mimir_home_env_wins_over_the_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    module = fresh_cap_check()
    assert module._home() == tmp_path


def test_cap_and_post_class_actions_match_the_guideline() -> None:
    """The cap and the action set are the contract this script enforces."""
    module = fresh_cap_check()
    assert module.CAP == 5
    assert module.POST_CLASS_ACTIONS == {"post", "reply", "thread", "quote"}
