"""Process-environment isolation between tests, and the enforcement-skip guard.

Two invariants live here, both prerequisites for running the suite with
``MIMIR_ACCESS_CONTROL_ENFORCED=1`` in CI:

1. Loading a home ``.env`` must not leak keys into ``os.environ`` past the test
   that loaded it (``tests/conftest.py::_isolate_process_env``).
2. No test may be skipped *because* enforcement is on — otherwise the enforced
   CI job stays green while the paths it exists to cover quietly stop running.
3. A host-only setting that overrides a value the suite asserts on must be
   cleared for the session (``tests/conftest.py::_clear_host_mimir_environment``),
   so the suite's result does not depend on the deployment it runs inside.
4. The env the poller framework injects into a child process must be cleared too.
   ``STATE_DIR`` is a write target, so inheriting it makes the suite mutate the
   live poller store rather than merely read the wrong value.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mimir.config import _load_home_dotenv

_CANARY = "MIMIR_TEST_DOTENV_LEAK_CANARY"
_ENFORCEMENT_FLAG = "MIMIR_ACCESS_CONTROL_ENFORCED"
_LONGMEMEVAL_SMOKE = "tests/test_longmemeval_via_memory_smoke.py"
_LONGMEMEVAL_NODE_IDS = {
    f"{_LONGMEMEVAL_SMOKE}::test_runner_parser_defaults_to_boundary_rrf_adoption_settings",
    f"{_LONGMEMEVAL_SMOKE}::test_runner_parser_preserves_boundary_ablation_flags",
    f"{_LONGMEMEVAL_SMOKE}::test_session_boundary_rrf_pathway_searches_sessions_and_expands_atoms",
    f"{_LONGMEMEVAL_SMOKE}::test_runner_completes_one_question[shadow]",
    f"{_LONGMEMEVAL_SMOKE}::test_runner_completes_one_question[enforced]",
    f"{_LONGMEMEVAL_SMOKE}::test_session_boundary_rrf_lane_keeps_summaries_out_of_reader",
    f"{_LONGMEMEVAL_SMOKE}::test_runner_no_consolidate_path",
    f"{_LONGMEMEVAL_SMOKE}::test_generated_session_boundaries_persist_real_sessions",
    f"{_LONGMEMEVAL_SMOKE}::test_capture_reader_prompt_writes_debug_without_bloating_metrics",
}


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


def test_longmemeval_memory_smoke_cases_are_collected():
    """Fail if the optional bench dependency silently skips pipeline coverage."""
    root = Path(__file__).parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            _LONGMEMEVAL_SMOKE,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith(f"{_LONGMEMEVAL_SMOKE}::")
    }

    assert result.returncode == 0, result.stdout + result.stderr
    assert collected == _LONGMEMEVAL_NODE_IDS, (
        "LongMemEval via_memory collection changed:\n"
        f"missing: {sorted(_LONGMEMEVAL_NODE_IDS - collected)}\n"
        f"unexpected: {sorted(collected - _LONGMEMEVAL_NODE_IDS)}"
    )


# ── 3. host-only overrides must not change the suite's result ─────────

_PUBLISHING_IDENTITY = "MIMIR_FACTORY_PUBLISHING_IDENTITY"
_DECLARED_IDENTITY_TESTS = (
    "tests/test_worklink_orchestrator.py"
    "::test_factory_new_run_uses_resolved_base_for_single_checkout_placement"
)


def test_declared_publishing_identity_tests_ignore_the_host_override() -> None:
    """The suite must pass with the deployment's publishing identity exported.

    ``MIMIR_FACTORY_PUBLISHING_IDENTITY`` overrides the ``publishing_identity``
    a checkout declares in ``.factory.json``. The tests that assert on the
    declared value therefore read the deployment's value instead whenever it is
    set, which is why this ran green in CI -- where it is unset -- and failed
    inside mimirbot, where ``compose.env`` sets it. The worklink test gate went
    red on every build, so ``review_ready`` stayed false and no build reached
    the commit step or published a PR.

    Asserting the variable is absent would only restate the fixture. Running the
    affected tests in a child process with it *present* is the property that
    actually matters, and it fails if the name is dropped from
    ``_clear_host_mimir_environment``.
    """
    env = dict(os.environ)
    env[_PUBLISHING_IDENTITY] = "deployment-owner"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", _DECLARED_IDENTITY_TESTS],
        capture_output=True,
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
    )
    assert completed.returncode == 0, (
        f"{_PUBLISHING_IDENTITY} leaked into the suite:\n"
        f"{completed.stdout.decode(errors='replace')[-2000:]}"
    )


# ── 4. poller-injected env must not reach the suite ───────────────────

_POSTCLAIM_FAILURE_TEST = (
    "tests/test_worklink_orchestrator.py::test_postclaim_failure_emits_same_failure_event"
)


def test_conftest_clears_every_poller_injected_env_key() -> None:
    """``conftest._POLLER_INJECTED_ENV`` must stay in step with its source of truth.

    ``mimir.pollers._POLLER_INJECTED_ENV_KEYS`` is the authoritative set of names
    the poller framework injects into a poller's child process. A name added
    there and not here would silently start leaking into the suite, so this fails
    the moment the two drift.
    """
    from mimir.pollers import _POLLER_INJECTED_ENV_KEYS

    from tests.conftest import _POLLER_INJECTED_ENV

    assert _POLLER_INJECTED_ENV_KEYS <= _POLLER_INJECTED_ENV, (
        "poller-injected env keys missing from the clearing fixture: "
        f"{sorted(_POLLER_INJECTED_ENV_KEYS - _POLLER_INJECTED_ENV)}"
    )


def test_suite_does_not_write_dispatch_failures_into_an_inherited_state_dir(
    tmp_path: Path,
) -> None:
    """A test must not write into the poller store it happens to inherit.

    ``_record_run_failure`` takes its destination from ``os.environ["STATE_DIR"]``
    rather than from the ``home`` it was passed, and
    ``test_postclaim_failure_emits_same_failure_event`` drives it with
    ``autonomous=True``. Inside mimirbot the worklink gate runs this suite as a
    child of a poller, so that test wrote a fabricated failure record --
    ``issue 441``, ``attempt 2``, ``"backend exploded api_key=[REDACTED]"`` -- into
    the live ``worklink-ready-queue`` store, against an issue that is closed.

    The test passes either way, which is why nothing surfaced it. The property
    worth asserting is that the store stays untouched.
    """
    probe = tmp_path / "state"
    probe.mkdir()
    env = dict(os.environ)
    env["STATE_DIR"] = str(probe)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", _POSTCLAIM_FAILURE_TEST],
        capture_output=True,
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout.decode(errors="replace")[-2000:]
    written = sorted(child.name for child in probe.iterdir())
    assert written == [], f"suite wrote into an inherited STATE_DIR: {written}"
