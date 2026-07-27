"""Process-environment isolation between tests, and the enforcement-skip guard.

Two invariants live here, both prerequisites for running the suite with
``MIMIR_ACCESS_CONTROL_ENFORCED=1`` in CI:

1. Loading a home ``.env`` must not leak keys into ``os.environ`` past the test
   that loaded it (``tests/conftest.py::_isolate_process_env``).
2. No test may be skipped *because* enforcement is on — otherwise the enforced
   CI job stays green while the paths it exists to cover quietly stop running.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from mimir.config import _load_home_dotenv

_CANARY = "MIMIR_TEST_DOTENV_LEAK_CANARY"
_ENFORCEMENT_FLAG = "MIMIR_ACCESS_CONTROL_ENFORCED"


# ── 1. dotenv leakage ────────────────────────────────────────────────
#
# These two run in definition order (no pytest-randomly in the dep set), which
# is what makes the pair a valid probe: the first deliberately leaks, and the
# second observes whether the autouse fixture cleaned up. Without the fixture
# the second test FAILS — that is the regression this file guards.


def test_dotenv_load_puts_keys_in_the_process_env(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    """The hazard itself: ``_load_home_dotenv`` mutates ``os.environ``.

    This is correct runtime behavior, not a bug — asserted here so the pair
    below is meaningful. If this ever stops being true, the isolation fixture
    is guarding nothing and the second test would pass vacuously.
    """
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(f"{_CANARY}=leaked\n", encoding="utf-8")

    assert _CANARY not in os.environ
    loaded = _load_home_dotenv(tmp_path)

    assert _CANARY in loaded
    assert os.environ[_CANARY] == "leaked"


def test_dotenv_keys_do_not_survive_into_the_next_test():
    """The invariant: the previous test's leaked key is gone.

    ``monkeypatch`` cannot restore this — python-dotenv set it underneath, not
    through monkeypatch — so this passes only because of the conftest
    ``_isolate_process_env`` fixture.
    """
    assert _CANARY not in os.environ, (
        f"{_CANARY} leaked out of the previous test; the _isolate_process_env "
        "autouse fixture in tests/conftest.py is missing or broken"
    )


# ── 2. no enforcement-keyed skips ────────────────────────────────────


def _enforcement_tainted_names(tree: ast.AST) -> set[str]:
    """Module-level names whose value derives from the enforcement flag.

    Catches the indirection that actually occurred: a module constant reads the
    env var, and the ``skipif`` condition references the constant rather than
    the flag, so a plain string search for the flag next to ``skipif`` misses it.
    """
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if _ENFORCEMENT_FLAG not in ast.dump(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                tainted.add(target.id)
    return tainted


def _skipif_conditions(tree: ast.AST) -> list[ast.expr]:
    """Every ``pytest.mark.skipif(...)`` condition expression in the module,
    whether applied as a decorator or bound to a module-level marker name."""
    conditions: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "skipif" and node.args:
            conditions.append(node.args[0])
    return conditions


def test_no_test_is_skipped_because_enforcement_is_on():
    """A green enforced CI run must mean "nothing failed", not "nothing ran".

    #1001 shipped four ``skipif(enforcement)`` markers as an accepted interim;
    the enforced suite was green at 20 skips while those paths went unexercised
    until #910 landed the carrier and deleted them. Absent this guard, the
    cheapest fix for any future enforced failure is another skip, and the job
    rots while staying green.

    If you genuinely need one, delete this test in the same PR and say why —
    the point is that it costs an argument, not that it is impossible.
    """
    offenders: list[str] = []
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable test file
            continue
        source = path.read_text(encoding="utf-8")
        if _ENFORCEMENT_FLAG not in source:
            continue
        tainted = _enforcement_tainted_names(tree)
        for condition in _skipif_conditions(tree):
            dumped = ast.dump(condition)
            references_flag = _ENFORCEMENT_FLAG in dumped
            references_tainted = any(
                isinstance(sub, ast.Name) and sub.id in tainted
                for sub in ast.walk(condition)
            )
            if references_flag or references_tainted:
                offenders.append(f"{path.name}:{condition.lineno}")

    assert not offenders, (
        "tests skipped on the enforcement flag: " + ", ".join(offenders)
    )
