from __future__ import annotations

from datetime import UTC, datetime
import asyncio
import json
import shlex
import shutil
import subprocess
import sys
import threading
import pytest
from pathlib import Path
from typing import Sequence
from unittest.mock import MagicMock

from mimir.worklink.evidence import (
    TestResult,
    WorklinkEvidence,
    observe_evidence,
    read_pytest_result,
    validate_evidence,
)


def base_evidence(**overrides: object) -> WorklinkEvidence:
    values = dict(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        checkout=".worklink/439-1",
        started_at="2026-06-11T05:00:00+00:00",
        finished_at="2026-06-11T05:05:00+00:00",
        files_changed=["mimir/worklink/evidence.py"],
        diff_stat="1 file changed, 10 insertions(+)",
        commands=[],
        tests=TestResult("pytest", 0, "passed"),
        pr_url="https://github.com/example/repo/pull/1",
        status="completed",
        blocked_reason=None,
        transcript=None,
        diff_observed=True,
    )
    values.update(overrides)
    return WorklinkEvidence(**values)  # type: ignore[arg-type]


def test_completed_empty_diff_demotes_to_failed() -> None:
    result = validate_evidence(base_evidence(files_changed=[]))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "completed_empty_diff" in result.reasons


def test_backend_failure_without_text_still_records_a_reason() -> None:
    """A failed run must never be reasonless — chainlink #1108.

    Reproduces the shape of evidence files 1108-1/2/3: the backend reported
    ``failed`` with no ``failure_reason``, while the work was committed and the
    gate passed. Every other failure transition in ``validate_evidence`` names
    itself; this one only did when the backend supplied text, so the record was
    written with ``failure_reason: null`` and ``reasons: []`` and nobody could
    say why three complete builds were discarded.
    """
    result = validate_evidence(
        base_evidence(
            status="failed",
            failure_reason=None,
            files_changed=["mimir/worklink/orchestrator.py"],
            tests=TestResult("uv run pytest -q", 0, "ok", observed=True),
        )
    )

    assert result.status == "failed"
    assert result.review_ready is False
    assert result.reasons, "a failed run must state at least one reason"
    assert "failed_missing_reason" in result.reasons
    # the persisted record must carry it too, not just the validation object
    assert result.evidence.failure_reason
    assert "without a reason" in result.evidence.failure_reason


def test_backend_supplied_failure_reason_is_preserved() -> None:
    """The synthesized reason must not displace a real one."""
    result = validate_evidence(
        base_evidence(status="failed", failure_reason="executor exited 7")
    )

    assert "executor exited 7" in result.reasons
    assert "failed_missing_reason" not in result.reasons
    assert result.evidence.failure_reason == "executor exited 7"


def test_review_rejects_unobserved_fabricated_tests() -> None:
    result = validate_evidence(base_evidence(tests=TestResult("pytest", 0, "backend says passed", observed=False)))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_not_observed" in result.reasons


@pytest.mark.parametrize("exit_code", [None, 0])
def test_explicit_skipped_tests_are_not_review_ready(exit_code: int | None) -> None:
    result = validate_evidence(base_evidence(tests=TestResult("pytest", exit_code, skipped_reason="executor exited nonzero before the test gate")))

    assert result.status == "completed"
    assert result.review_ready is False
    assert result.reasons == ()
    assert result.evidence.tests.skipped_reason == "executor exited nonzero before the test gate"
    assert validate_evidence(base_evidence()).review_ready is True


def test_blocked_requires_reason() -> None:
    result = validate_evidence(base_evidence(status="blocked", blocked_reason=""))

    assert result.status == "failed"
    assert "blocked_missing_reason" in result.reasons


def test_blocked_reason_does_not_require_diff_or_tests() -> None:
    result = validate_evidence(
        base_evidence(
            status="blocked",
            blocked_reason="planner contradiction",
            files_changed=[],
            tests=None,
            diff_observed=False,
        )
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.reasons == ()


@pytest.mark.asyncio
async def test_observe_evidence_carries_backend_blocked_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    result = await observe_evidence(
        issue=466,
        attempt=1,
        backend="codex",
        branch="issue/466-a1",
        checkout=repo,
        started_at=datetime(2026, 6, 13, 12, tzinfo=UTC),
        base_ref="main",
        backend_status="blocked",
        blocked_reason="Acceptance criteria conflict with review criteria",
        test_command=None,
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.reasons == ()
    assert result.evidence.blocked_reason == "Acceptance criteria conflict with review criteria"


def test_gate_test_summary_keeps_output_tail_not_head() -> None:
    """chainlink #815: pytest prints the failure list LAST — the evidence test
    summary must keep the tail so retries can act on it."""
    from mimir.worklink.evidence import _summarize_test_output

    lines = [f"noise-{index}" for index in range(1, 101)] + [
        "FAILED tests/test_z.py::test_gate - AssertionError",
        "1 failed, 9 passed in 3.16s",
    ]
    result = subprocess.CompletedProcess(
        ["pytest"], 1, stdout="\n".join(lines), stderr="warning: deprecation"
    )

    summary = _summarize_test_output(result)

    assert "1 failed, 9 passed in 3.16s" in summary
    assert "FAILED tests/test_z.py::test_gate" in summary
    assert "warning: deprecation" in summary
    assert "noise-1\n" not in summary
    assert len(summary) <= 6000


def test_structured_pytest_failures_are_scrubbed_before_evidence(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_text(
        '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0" />'
        "</testsuites>",
        encoding="utf-8",
    )
    cache = tmp_path / "cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text(
        json.dumps({"tests/test_api.py::test_token[token=top-secret]": True}),
        encoding="utf-8",
    )

    result = read_pytest_result("pytest -q", tmp_path)

    assert result is not None
    assert "top-secret" not in result.failed_tests[0]
    assert "[REDACTED]" in result.failed_tests[0]


@pytest.mark.parametrize(
    ("contents", "max_bytes", "expected"),
    [
        (None, None, "junit_missing"),
        ("not xml", None, "junit_parse_error"),
        ('<testsuite tests="1" />', 1, "junit_oversize"),
    ],
)
def test_pytest_report_read_failures_are_distinguishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str | None,
    max_bytes: int | None,
    expected: str,
) -> None:
    if contents is not None:
        (tmp_path / "junit.xml").write_text(contents, encoding="utf-8")
    if max_bytes is not None:
        monkeypatch.setattr("mimir.worklink.evidence._PYTEST_REPORT_MAX_BYTES", max_bytes)

    result = read_pytest_result("pytest -q", tmp_path)

    assert result is not None
    assert result.counts is None
    assert result.report_error == expected


def _init_gate_repo(tmp_path: Path, test_source: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "test_gate_sample.py").write_text(test_source, encoding="utf-8")
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["opencode", "feature_factory"])
@pytest.mark.parametrize("injected", [False, True])
async def test_default_gate_bounds_checkout_environment(tmp_path, monkeypatch, backend, injected):
    from mimir.worklink.checkout import coding_enabled
    from mimir.worklink.orchestrator import _runner_for_home

    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    assert not coding_enabled()
    credentials = (
        "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SLACK_BOT_TOKEN",
        "DISCORD_TOKEN", "MIMIR_API_KEY", "MIMIR_MODEL_SPEC", "UNLISTED_GATE_SENTINEL",
    )
    for name in credentials:
        monkeypatch.setenv(name, "gate-secret-sentinel")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-W error")
    repo = _init_gate_repo(
        tmp_path,
        "from pathlib import Path\ndef test_checkout():\n    assert Path('seed.txt').read_text() == 'seed\\n'\n",
    )
    # conftest runs before collection, just as backend-authored code would.
    (repo / "conftest.py").write_text(
        "import os\n"
        f"assert not set({credentials!r}) & os.environ.keys()\n"
        "assert '-W error' in os.environ['PYTEST_ADDOPTS']\n"
        "assert '--junitxml=' in os.environ['PYTEST_ADDOPTS']\n",
        encoding="utf-8",
    )
    result = await observe_evidence(
        issue=1684, attempt=1, backend=backend, branch="issue/1684-a1",
        checkout=repo, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed",
        test_command=f"{shlex.quote(sys.executable)} -m pytest -q test_gate_sample.py",
        runner=_runner_for_home(tmp_path, "chainlink") if injected else None,
    )
    assert result.review_ready, result.evidence.tests
    assert result.evidence.tests.counts.passed == 1
    assert result.evidence.tests.report_error is None


@pytest.mark.parametrize("injected", [False, True])
def test_shell_gate_real_uv_has_provisioned_home(tmp_path, monkeypatch, injected):
    from mimir.worklink.evidence import _run
    from mimir.worklink.orchestrator import _runner_for_home

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv executable required for real cache initialization")
    monkeypatch.setenv("GITHUB_TOKEN", "gate-secret-sentinel")
    monkeypatch.setenv("HOME", str(tmp_path / "controller-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "controller-cache"))
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "assert 'GITHUB_TOKEN' not in os.environ\n"
        "home = Path(os.environ['HOME'])\n"
        "assert home.is_dir()\n"
        "assert home.stat().st_mode & 0o777 == 0o700\n"
        "for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):\n"
        "    path = Path(os.environ[key])\n"
        "    assert path.is_dir() and home in path.parents\n"
        "    (path / 'writable').write_text('yes')\n"
        "assert (Path(os.environ['XDG_CACHE_HOME']) / 'uv').is_dir()\n"
        "print(json.dumps(str(home)))\n",
        encoding="utf-8",
    )
    runner = _runner_for_home(tmp_path, "chainlink") if injected else _run
    # Execute uv itself, not a subprocess mock or bare Python: its startup
    # creates the cache that failed under /var/lib/mimir-worklink/homes/evidence.
    # No project/dependency resolution keeps this regression offline.
    command = (
        f"{shlex.quote(uv)} run --offline --no-project "
        f"--python {shlex.quote(sys.executable)} python {shlex.quote(str(probe))}"
    )
    homes = []
    for _ in range(2):
        result = runner(command, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        home = Path(json.loads(result.stdout))
        homes.append(home)
        assert not home.exists(), "gate home must be cleaned after execution"
    assert homes[0] != homes[1]
    assert not (tmp_path / "controller-cache").exists()
    assert not (tmp_path / "controller-home").exists()


@pytest.mark.asyncio
async def test_gate_records_counts_from_pytest_generated_junit(tmp_path: Path) -> None:
    repo = _init_gate_repo(
        tmp_path,
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n",
    )
    test_command = f"{shlex.quote(sys.executable)} -m pytest -q test_gate_sample.py"

    result = await observe_evidence(
        issue=1482,
        attempt=1,
        backend="codex",
        branch="issue/1482-a1",
        checkout=repo,
        started_at=datetime.now(UTC),
        base_ref="main",
        backend_status="completed",
        test_command=test_command,
    )

    assert result.review_ready is True
    assert result.evidence.tests is not None
    assert result.evidence.tests.report_error is None
    assert result.evidence.tests.counts is not None
    assert result.evidence.tests.counts.total == 2
    assert result.evidence.tests.counts.passed == 2


@pytest.mark.asyncio
async def test_gate_records_failed_node_ids_from_pytest_cache(tmp_path: Path) -> None:
    repo = _init_gate_repo(
        tmp_path,
        "def test_passes():\n    assert True\n\ndef test_fails():\n    assert False\n",
    )
    test_command = f"{shlex.quote(sys.executable)} -m pytest -q test_gate_sample.py"

    result = await observe_evidence(
        issue=1482,
        attempt=1,
        backend="codex",
        branch="issue/1482-a1",
        checkout=repo,
        started_at=datetime.now(UTC),
        base_ref="main",
        backend_status="completed",
        test_command=test_command,
    )

    assert result.status == "failed"
    assert result.evidence.tests is not None
    assert result.evidence.tests.counts is not None
    assert result.evidence.tests.counts.failed == 1
    assert result.evidence.tests.failed_tests == (
        "test_gate_sample.py::test_fails",
    )


@pytest.mark.parametrize("failure", ["parallel", "transient_infrastructure"])
@pytest.mark.asyncio
async def test_serial_rerun_cannot_publish_parallel_gate_as_pass(tmp_path, monkeypatch, failure):
    from mimir.worklink.orchestrator import IssueContext, _open_pr

    repo = _init_gate_repo(tmp_path, f'''
from pathlib import Path

def test_candidate(request):
    if {failure!r} == "parallel":
        assert not hasattr(request.config, "workerinput")
    else:
        marker = Path("infrastructure-ready")
        ready = marker.exists()
        marker.touch()
        assert ready, "temporary infrastructure outage"

def test_passes():
    pass
''')
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "false")
    command = f"{shlex.quote(sys.executable)} -m pytest -q -n 2"
    result = await observe_evidence(
        issue=1692, attempt=1, backend="codex", branch="issue/1692-a1",
        checkout=repo, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command=command,
    )
    tests = result.evidence.tests
    assert tests.initial_run.exit_code == 1
    assert tests.rerun.exit_code == 0
    assert "-n 0" in tests.rerun.cmd
    assert tests.flaky_tests == ("test_gate_sample.py::test_candidate",)
    if failure == "transient_infrastructure":
        assert "temporary infrastructure outage" in tests.initial_run.summary
        assert "1 passed" in tests.rerun.summary
    calls = []

    def runner(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/1\n", "")

    # Exercise the renderer even for failed evidence, without publishing anything.
    _open_pr(repo, IssueContext(1692, "gate", "", set()), "issue/1692-a1",
             result.evidence, base="main", runner=runner)
    pr_call = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    body = pr_call[pr_call.index("--body") + 1]
    assert (result.review_ready, tests.exit_code, f"- Tests: `{command}` → 0" in body) == (
        False, 1, False,
    ), body
    assert tests.counts == tests.initial_run.counts
    assert tests.failed_tests == tests.initial_run.failed_tests
    assert result.status == "failed"
    assert "tests_failed" in result.reasons
    assert f"- Tests: `{command}` → 1" in body
    assert f"- Original gate: `{command}` → 1" in body
    assert f"- Diagnostic rerun (serial, failed nodes only): `{tests.rerun.cmd}` → 0" in body
    assert "flaky_tests (passed in isolation; not proof of flakiness)" in body
    assert "test_gate_sample.py::test_candidate" in body


@pytest.mark.parametrize("mode,limit,green,flaky,failed,reran", [
    ("flaky", 10, False, 1, 1, True),
    ("persistent", 10, False, 0, 1, True),
    ("mixed", 10, False, 1, 2, True),
    ("mixed", 1, False, 0, 2, False),
    ("flaky", 0, False, 0, 1, False),
    ("skip", 10, False, 0, 1, True),
])
@pytest.mark.asyncio
async def test_gate_reruns_only_failed_nodes_once(
    tmp_path, monkeypatch, mode, limit, green, flaky, failed, reran,
) -> None:
    # Real pytest reports and an awkward parametrized ID exercise both exact
    # selection and the separation between private selectors and public evidence.
    repo = _init_gate_repo(tmp_path, f'''
from pathlib import Path
import os
import pytest

@pytest.mark.parametrize("value", [1], ids=["token=top-secret ; $literal"])
def test_candidate(value):
    assert "WORKLINK_GATE_TEST_ENV" not in os.environ
    path = Path("visits")
    visits = int(path.read_text()) if path.exists() else 0
    path.write_text(str(visits + 1))
    if {mode!r} == "skip" and visits:
        pytest.skip("not a passing rerun")
    assert visits and {mode!r} != "persistent"

def test_other():
    path = Path("other-visits")
    visits = int(path.read_text()) if path.exists() else 0
    path.write_text(str(visits + 1))
    assert {mode!r} != "mixed"
''')
    monkeypatch.setenv("WORKLINK_GATE_TEST_ENV", "same")
    result = await observe_evidence(
        issue=1557, attempt=1, backend="codex", branch="issue/1557-a1",
        checkout=repo, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed",
        test_command=f"{shlex.quote(sys.executable)} -m pytest -q test_gate_sample.py",
        gate_rerun_max_failures=limit,
    )
    tests = result.evidence.tests
    assert result.review_ready is green
    assert len(tests.flaky_tests) == flaky
    assert len(tests.failed_tests) == failed
    assert tests.counts.failed == failed
    assert (tests.rerun is not None) is reran
    assert (repo / "visits").read_text() == ("2" if reran else "1")
    assert (repo / "other-visits").read_text() == ("2" if mode == "mixed" and reran else "1")
    if reran:
        assert tests.initial_run.exit_code == 1
        assert "-n 0" in tests.rerun.cmd
        assert "top-secret" not in tests.rerun.cmd
    assert "top-secret" not in str(result.evidence)


@pytest.mark.parametrize("fault", [
    "exit", "missing", "total", "errors", "skipped", "failures",
    "collected", "foreign_failure", "exit_counts", "timeout",
])
@pytest.mark.asyncio
async def test_gate_rerun_incomplete_reports_fail_closed(tmp_path, monkeypatch, fault):
    import mimir.worklink.evidence as module

    calls = []
    node = "test_sample.py::test_one"
    def runner(command, **kwargs):
        if not isinstance(command, str):
            output = "test_sample.py\n" if "--name-only" in command else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        calls.append(command)
        options = shlex.split(shlex.split(command)[0].split("=", 1)[1])
        report = Path(next(part.split("=", 1)[1] for part in options if part.startswith("--junitxml="))).parent
        second = len(calls) == 2
        total, failures, errors, skipped, exit_code = 1, int(not second), 0, 0, int(not second)
        remaining = [] if second else [node]
        collected = [node]
        if second:
            if fault == "exit": exit_code = 2
            if fault == "total": total = 0
            if fault == "errors": errors = 1
            if fault == "skipped": skipped = 1
            if fault == "failures": failures = 1
            if fault == "collected": collected = ["test_sample.py::test_other"]
            if fault == "foreign_failure":
                remaining, failures, exit_code = ["test_sample.py::test_other"], 1, 1
            if fault == "exit_counts": exit_code = 1
        if not (second and fault == "missing"):
            (report / "junit.xml").write_text(
                f'<testsuite tests="{total}" failures="{failures}" errors="{errors}" skipped="{skipped}" />'
            )
        cache = report / "cache" / "v" / "cache"
        cache.mkdir(parents=True)
        (cache / "lastfailed").write_text(json.dumps(dict.fromkeys(remaining, True)))
        (cache / "nodeids").write_text(json.dumps(collected))
        if second and fault == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, exit_code, "output", "")

    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    result = await module.observe_evidence(
        issue=1557, attempt=1, backend="opencode", branch="issue/1557-a1",
        checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command="uv run --extra dev --extra bench pytest -q -n 6",
        runner=runner,
    )
    assert len(calls) == 2
    assert result.status == "failed"
    assert result.evidence.tests.failed_tests == (node,)
    assert result.evidence.tests.flaky_tests == ()
    if fault == "timeout":
        assert result.evidence.tests.timed_out
        assert result.evidence.tests.rerun.timed_out
        assert not result.evidence.tests.initial_run.timed_out
        assert result.reasons == ("gate_timed_out",)


@pytest.mark.parametrize("command", [
    "pytest -q && true", "pytest -q || true", "echo pytest", "pytest --unknown",
    "pytest $TEST_SELECTION", "pytest --ignore tests/foo.py", "pytest --collect-only",
    "uv run sh -c pytest", "uv run echo pytest", "uv run --unknown pytest",
    "uv sync pytest",
])
def test_gate_rerun_refuses_unknown_command_shapes(command):
    from mimir.worklink.evidence import _pytest_rerun_command
    assert _pytest_rerun_command(command, ("tests/test_one.py::test_one",)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_run", [1, 2], ids=["initial", "rerun"])
async def test_worker_timeout_with_complete_reports_fails_closed(tmp_path, monkeypatch, timeout_run):
    from mimir.worklink.compute import ComputeResult, LaunchHandle, WorkSpec

    node = "test_sample.py::test_one"
    specs = []

    class Compute:
        async def launch(self, spec):
            specs.append(spec)
            return LaunchHandle("local_subprocess", str(len(specs)))

        async def wait(self, handle, timeout_s):
            options = shlex.split(specs[-1].env["PYTEST_ADDOPTS"])
            report = tmp_path / next(part.split("=", 1)[1] for part in options if part.startswith("--junitxml="))
            failed = int(len(specs) == 1)
            report.write_text(f'<testsuite tests="1" failures="{failed}" errors="0" skipped="0" />')
            cache = report.parent / "cache" / "v" / "cache"
            cache.mkdir(parents=True)
            (cache / "lastfailed").write_text(json.dumps({node: True} if failed else {}))
            (cache / "nodeids").write_text(json.dumps([node]))
            # Compute preserves exit codes even when terminal collection times out
            # after the gate has written reports (compute.py:718-737).
            return ComputeResult(failed, "reports written", "", timed_out=len(specs) == timeout_run)

        async def cleanup(self, handle):
            pass

    def runner(command, **kwargs):
        assert not isinstance(command, str), "gate must use worker compute"
        return subprocess.CompletedProcess(command, 0, "test_sample.py\n" if "--name-only" in command else "", "")

    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    result = await observe_evidence(
        issue=1696, attempt=1, backend="opencode", branch="branch",
        checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command="pytest -q", runner=runner,
        work_spec=WorkSpec(1696, 1, "url", "main", "branch", "prompt", None, "pytest -q", "opencode", 7),
        compute=Compute(),
    )
    assert len(specs) == timeout_run
    assert result.reasons == ("gate_timed_out",)
    assert not result.review_ready
    tests = result.evidence.tests
    assert tests.timed_out
    assert tests.exit_code == 1
    assert tests.counts.failed == 1
    assert tests.failed_tests == (node,)
    assert tests.flaky_tests == ()
    if timeout_run == 1:
        assert tests.rerun is None
    else:
        assert tests.rerun.timed_out
        assert tests.rerun.exit_code == 0
        assert tests.rerun.counts.passed == 1


def test_gate_rerun_preserves_launcher_and_removes_selection():
    from mimir.worklink.evidence import _pytest_rerun_command
    command = "uv run --extra dev --extra bench pytest -q -n 6 -k first -m slow tests/ -x --lf"
    node = "tests/test_one.py::test_one[semi; quoted ' argument]"
    assert shlex.split(_pytest_rerun_command(command, (node,))) == [
        "uv", "run", "--extra", "dev", "--extra", "bench", "pytest", "-q",
        "-n", "0", "-k", "", "-m", "", "--", node,
    ]


def test_evidence_test_command_uses_bare_command_without_model_spec(
    monkeypatch,
) -> None:
    from mimir.worklink.evidence import _run

    captured: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        process = MagicMock()
        process.__enter__.return_value = process
        process.communicate.return_value = ("passed", "")
        process.returncode = 0
        return process

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:agent-model")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    _run("uv run pytest -q", cwd=Path("/tmp/checkout"))

    assert captured["args"] == "uv run pytest -q"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "MIMIR_MODEL_SPEC" not in kwargs["env"]


def test_gate_command_not_found_is_not_tests_failed() -> None:
    """chainlink #820: exit 127 = the gate command itself cannot run — an
    environment error, distinct from failing tests."""
    result = validate_evidence(base_evidence(tests=TestResult("pytest -q", 127, "pytest: not found")))

    assert result.review_ready is False
    assert "gate_command_not_found" in result.reasons
    assert "tests_failed" not in result.reasons
    assert result.evidence.failure_reason == "test gate command was not found (exit 127)"


@pytest.mark.parametrize("exit_code", [0, 1, 124, 127, -15])
def test_gate_timeout_is_configuration_fault_not_test_failure(exit_code):
    result = validate_evidence(base_evidence(
        tests=TestResult("pytest -q", exit_code, timed_out=True),
    ))
    assert result.status == "failed"
    assert not result.review_ready
    assert result.reasons == ("gate_timed_out",)
    assert "timeout configuration" in result.evidence.failure_reason


def test_gate_exit_124_alone_is_not_an_observed_timeout():
    result = validate_evidence(base_evidence(tests=TestResult("pytest -q", 124)))
    assert result.reasons == ("tests_failed",)


def test_gate_timeout_is_persisted_and_counts_as_divergence():
    from dataclasses import asdict
    from mimir.worklink.evidence import _gate_results_diverge

    executor = TestResult("pytest -q", 0)
    measured = TestResult("pytest -q", 0, timed_out=True)
    assert asdict(measured)["timed_out"] is True
    assert _gate_results_diverge(executor, measured) is True


@pytest.mark.asyncio
async def test_controller_gate_yields_to_heartbeat_and_records_timeout(tmp_path):
    from mimir.worklink.compute import WorkSpec

    entered = threading.Event()
    heartbeat = threading.Event()
    calls = []

    def runner(command, **kwargs):
        if not isinstance(command, str):
            return subprocess.CompletedProcess(command, 0, "changed.py\n", "")
        calls.append(kwargs)
        entered.set()
        assert heartbeat.wait(5), "controller gate blocked the event loop"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], b"token=secret", b"last diagnostic")

    async def beat():
        while not entered.is_set():
            await asyncio.sleep(0.001)
        heartbeat.set()

    task = asyncio.create_task(beat())
    try:
        result = await observe_evidence(
            issue=1696, attempt=1, backend="codex", branch="branch",
            checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
            backend_status="completed", test_command="pytest -q", runner=runner,
            work_spec=WorkSpec(1696, 1, "url", "main", "branch", "prompt", None, "pytest -q", "codex", 7),
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert calls == [{"cwd": tmp_path, "timeout": 7}]
    assert result.reasons == ("gate_timed_out",)
    assert result.evidence.tests.timed_out
    assert result.evidence.tests.exit_code == 124
    assert "last diagnostic" in result.evidence.tests.summary
    assert "secret" not in result.evidence.tests.summary
    assert result.evidence.tests.rerun is None


@pytest.mark.parametrize("runner_name", ["evidence", "orchestrator", "home"])
def test_default_controller_runner_enforces_timeout(tmp_path, runner_name):
    from mimir.worklink.evidence import _run
    from mimir.worklink.orchestrator import _run as orchestrator_run, _runner_for_home

    runner = {"evidence": _run, "orchestrator": orchestrator_run, "home": _runner_for_home(tmp_path, "chainlink")}[runner_name]

    with pytest.raises(subprocess.TimeoutExpired):
        runner(
            f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(10)'",
            cwd=tmp_path, timeout=0.05,
        )


@pytest.mark.parametrize("for_home", [False, True])
def test_controller_gate_runner_preserves_binary_output(tmp_path, for_home):
    from mimir.worklink.orchestrator import _run, _runner_for_home

    runner = _runner_for_home(tmp_path, "chainlink") if for_home else _run
    result = runner("printf gate", cwd=tmp_path, text=False, timeout=5)
    assert result.stdout == b"gate"
    assert result.stderr == b""
    assert result.returncode == 0


def test_completed_requires_tests_or_skipped_reason() -> None:
    result = validate_evidence(base_evidence(tests=None))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_missing" in result.reasons


def test_completed_requires_passing_tests() -> None:
    result = validate_evidence(base_evidence(tests=TestResult("pytest", 1, "failed")))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_failed" in result.reasons


def test_completed_requires_observed_diff() -> None:
    result = validate_evidence(base_evidence(diff_observed=False))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "diff_not_observed" in result.reasons


@pytest.mark.asyncio
async def test_observe_evidence_uses_executor_diff_and_test_results(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "a.txt").write_text("new\n")

    result = await observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        checkout=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        test_command="test -f a.txt",
    )

    assert result.review_ready is True
    assert result.evidence.files_changed == ["a.txt"]
    assert result.evidence.tests is not None
    assert result.evidence.tests.exit_code == 0


@pytest.mark.asyncio
async def test_executor_crash_skips_gate_and_scrubs_bounded_failure_reason(tmp_path: Path) -> None:
    commands: list[Sequence[str] | str] = []

    def runner(
        args: Sequence[str] | str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        assert args != "pytest -q"
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    unsafe_reason = "ignored earlier line\nprovider token=top-secret " + ("x" * 1200)
    result = await observe_evidence(
        issue=1063,
        attempt=1,
        backend="opencode",
        branch="issue/1063-a1",
        checkout=tmp_path,
        started_at=datetime(2026, 7, 30, 12, 20, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="failed",
        test_command="pytest -q",
        model="openai/gpt-5.6-sol",
        failure_reason=unsafe_reason,
        skip_test_reason="executor exited nonzero before the test gate",
        runner=runner,
    )

    assert "pytest -q" not in commands
    assert result.status == "failed"
    assert result.evidence.failure_reason is not None
    assert result.reasons == (result.evidence.failure_reason,)
    assert result.evidence.failure_reason.startswith("provider token=[REDACTED] ")
    assert len(result.evidence.failure_reason) == 1000
    assert "top-secret" not in result.evidence.failure_reason
    assert result.evidence.model == "openai/gpt-5.6-sol"
    assert result.evidence.tests == TestResult(
        "pytest -q", skipped_reason="executor exited nonzero before the test gate"
    )


@pytest.mark.asyncio
async def test_observe_evidence_sees_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "new_module.py").write_text("print('new')\n")

    result = await observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        checkout=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        # The bounded gate PATH need not contain the active Python (e.g. macOS).
        test_command=f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(0)'",
    )

    assert result.review_ready is True, result.evidence.tests
    assert result.evidence.files_changed == ["new_module.py"]


@pytest.mark.asyncio
async def test_observe_evidence_sees_committed_backend_work(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    subprocess.run(["git", "switch", "-q", "-c", "issue/439-a1"], cwd=repo, check=True)
    (repo / "new_test.py").write_text("def test_new():\n    assert True\n")
    subprocess.run(["git", "add", "new_test.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "backend work"], cwd=repo, check=True)

    result = await observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        checkout=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        # The bounded gate PATH need not contain the active Python (e.g. macOS).
        test_command=f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(0)'",
    )

    assert result.review_ready is True
    assert result.evidence.files_changed == ["new_test.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_enabled_opencode_gate_uses_authorized_compute(monkeypatch, tmp_path: Path, timed_out) -> None:
    from mimir.worklink.compute import (
        ComputeResult,
        LaunchHandle,
        WorkSpec,
        _enabled_child_env,
    )

    class Compute:
        def __init__(self) -> None:
            self.specs = []
            self.cleaned = []

        async def launch(self, spec):
            _enabled_child_env(spec, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            self.specs.append(spec)
            return LaunchHandle("local_subprocess", "job")

        async def wait(self, handle, timeout_s):
            return ComputeResult(0, "passed", "", handle=handle, timed_out=timed_out)

        async def cleanup(self, handle):
            self.cleaned.append(handle)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "a.txt").write_text("new\n")
    spec = WorkSpec(
        1,
        1,
        "url",
        "main",
        "branch",
        "prompt",
        None,
        "",
        "opencode",
        30,
        env={"OPENCODE_PERMISSION": '{"edit":"allow"}'},
        backend_config={"pass_env": ()},
        local_checkout=repo,
        local_argv=("opencode",),
    )
    compute = Compute()
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")

    class Publication:
        def run(self, *args, check=False):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=check,
            )

    result = await observe_evidence(
        issue=1,
        attempt=1,
        backend="opencode",
        branch="branch",
        checkout=repo,
        started_at=datetime.now(UTC),
        base_ref="main",
        backend_status="completed",
        test_command="pytest -q",
        work_spec=spec,
        compute=compute,
        safe_git=Publication(),
    )

    assert result.review_ready is (not timed_out)
    assert result.evidence.tests.timed_out is timed_out
    if timed_out:
        assert result.reasons == ("gate_timed_out",)
    assert compute.specs[0].local_argv == ("/bin/sh", "-c", "pytest -q")
    assert "PYTEST_ADDOPTS" in compute.specs[0].env
    report_option = next(
        option.split("=", 1)[1]
        for option in shlex.split(compute.specs[0].env["PYTEST_ADDOPTS"])
        if option.startswith("--junitxml=")
    )
    assert not Path(report_option).is_absolute()
    assert report_option.startswith(".worklink-gate-")
    assert compute.specs[0].backend_config["pass_env"] == ("PYTEST_ADDOPTS",)
    assert compute.cleaned == [LaunchHandle("local_subprocess", "job")]
    assert result.evidence.tests is not None
    assert result.evidence.tests.report_error == "junit_missing"


@pytest.mark.asyncio
async def test_enabled_opencode_evidence_uses_controller_git_without_fd_publication(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    calls: list[object] = []

    def runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    result = await observe_evidence(
        issue=1,
        attempt=1,
        backend="opencode",
        branch="branch",
        checkout=tmp_path,
        started_at=datetime.now(UTC),
        base_ref="main",
        backend_status="failed",
        test_command=None,
        runner=runner,
    )

    assert result.review_ready is False
    assert calls
    assert all(call[:3] == ["git", "-C", str(tmp_path)] for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["operator_stop", "restart_cleanup"])
async def test_live_gate_durable_handle_targets_exact_cancellation(
    tmp_path: Path, monkeypatch, recovery: str
) -> None:
    from mimir.worklink.compute import ComputeResult, LaunchHandle, WorkSpec
    from mimir.worklink.control import stop_worklink
    from mimir.worklink.evidence import _run_compute_gate
    from mimir.worklink.orchestrator import WorklinkRunner
    from mimir.worklink.run_state import WorklinkRunState, load_run_state, save_run_state

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    handle = LaunchHandle(
        "local_subprocess",
        "123e4567-e89b-42d3-a456-426614174099",
        process_start_ticks=909,
        shim_pid=808,
    )
    cancelled = []
    recovery_results = []

    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    async def cancel(self, observed):
        cancelled.append(observed)

    monkeypatch.setattr(
        "mimir.worklink.control.LocalSubprocessComputeBackend.cancel", cancel
    )
    monkeypatch.setattr(
        "mimir.worklink.orchestrator.LocalSubprocessComputeBackend.cancel", cancel
    )
    monkeypatch.setattr(
        "mimir.worklink.control.process_is_alive", lambda state: True
    )
    monkeypatch.setattr(
        "mimir.worklink.control.process_identity_verified", lambda state: True
    )
    monkeypatch.setattr(
        "mimir.worklink.orchestrator.process_is_alive", lambda state: True
    )

    class Compute:
        async def launch(self, spec):
            return handle

        async def wait(self, observed, timeout_s):
            state = load_run_state(tmp_path, 1410)
            assert state is not None
            assert (
                state.handle_identifier,
                state.shim_pid,
                state.process_start_ticks,
            ) == (handle.identifier, handle.shim_pid, handle.process_start_ticks)
            if recovery == "operator_stop":
                result = await asyncio.to_thread(
                    stop_worklink, tmp_path, 1410, runner=runner
                )
                recovery_results.append(result.stopped)
            else:
                result = await WorklinkRunner(
                    home=tmp_path, repo=tmp_path, runner=runner
                ).reattach(1410)
                recovery_results.append(result.status == "failed")
            return ComputeResult(1, "", "cancelled", handle=observed)

        async def cleanup(self, observed):
            return None

        async def cancel(self, observed):
            cancelled.append(observed)

    def persist(observed):
        save_run_state(
            tmp_path,
            WorklinkRunState(
                issue_id=1410,
                attempt=1,
                backend="opencode",
                compute_name="local_subprocess",
                handle_substrate=observed.substrate,
                handle_identifier=observed.identifier,
                branch="issue/1410-a1",
                base_ref="main",
                local_base="base-sha",
                repo=str(tmp_path),
                repo_url="git@github.com:example/repo.git",
                test_command="pytest -q",
                started_at="2026-08-10T00:00:00+00:00",
                checkout=str(checkout),
                process_start_ticks=observed.process_start_ticks,
                shim_pid=observed.shim_pid,
            ),
        )

    spec = WorkSpec(
        1410,
        1,
        "git@github.com:example/repo.git",
        "main",
        "issue/1410-a1",
        "prompt",
        None,
        "pytest -q",
        "opencode",
        30,
        local_checkout=checkout,
        local_argv=("opencode", "run"),
    )
    await _run_compute_gate(
        "pytest -q",
        checkout=checkout,
        work_spec=spec,
        compute=Compute(),
        on_launch=persist,
    )

    assert recovery_results == [True]
    assert cancelled == [handle]
    assert load_run_state(tmp_path, 1410) is None
    assert checkout.is_dir()


def test_gate_report_directory_is_writable_by_the_gate_identity(tmp_path):
    """The worker path must not hand the gate a controller-only directory.

    Regression for the defect where `tempfile.TemporaryDirectory` produced 0700
    owned by the controller, so pytest raised PermissionError from
    pytest_sessionfinish while writing --junitxml -- after every test had passed.
    The gate then exited 1 on a green run and reported no structured counts.
    """
    from mimir.worklink.evidence import _gate_report_directory

    checkout = tmp_path / "checkout"
    checkout.mkdir()

    # Controller path keeps the private temp directory.
    with _gate_report_directory(checkout, False) as controller_dir:
        assert controller_dir.is_dir()
        assert checkout not in controller_dir.parents

    # Worker path places the report inside the checkout, whose setgid group the
    # worker shares, and makes it group-usable rather than owner-only.
    with _gate_report_directory(checkout, True) as worker_dir:
        assert worker_dir.is_dir()
        assert checkout in worker_dir.parents
        assert worker_dir.stat().st_mode & 0o070 == 0o070, (
            "gate report directory must be group-accessible; the worker reaches it "
            "by group, not by ownership"
        )
    assert not worker_dir.exists(), "report directory must be cleaned up"
