from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from mimir import output_capture
from mimir.worklink import evidence as ev
from mimir.worklink.compute import ComputeResult, LaunchHandle, WorkSpec


def spec(tmp_path: Path) -> WorkSpec:
    return WorkSpec(
        1754, 4, "url", "main", "branch", "prompt", None, "pytest -q",
        "opencode", 30, output_root=tmp_path / "artifacts",
    )


def report(directory: Path, failed: bool = True) -> bytes:
    directory.mkdir(exist_ok=True)
    body = "failure start\n" + "complete traceback\n" * 900 + "failure end"
    document = (
        f'<testsuite tests="1" failures="{int(failed)}" errors="0" skipped="0">'
        '<testcase classname="test_sample" name="test_one">'
        + (f"<failure>{body}</failure>" if failed else "")
        + "</testcase></testsuite>"
    ).encode()
    (directory / "junit.xml").write_bytes(document)
    cache = directory / "cache/v/cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text(json.dumps({"test_sample.py::test_one": True} if failed else {}))
    (cache / "nodeids").write_text(json.dumps(["test_sample.py::test_one"]))
    return document


async def retain(tmp_path: Path, **kwargs) -> ev.TestResult:
    directory = tmp_path / "reports"
    directory.mkdir(exist_ok=True)
    if not (directory / "junit.xml").exists():
        report(directory)
    return await ev._retain_gate_failure(
        ev.TestResult(kwargs.pop("command", "pytest -q"), 1, "summary", counts=ev.TestCounts(1, 0, 1, 0, 0)),
        subprocess.CompletedProcess("pytest", 1, "full stdout\n" * 900, "full stderr"),
        report_dir=directory, issue=1754, attempt=4, run_id="run123", phase="initial",
        checkout=tmp_path, work_spec=spec(tmp_path), compute=kwargs.pop("compute", None),
        compute_result=kwargs.pop("compute_result", None),
        worker_uid_drop=kwargs.pop("worker_uid_drop", False), **kwargs,
    )


def retained(result: ev.TestResult) -> dict[str, bytes]:
    manifest = json.loads(Path(result.retention_manifest).read_text())
    return {item["source"]: Path(item["path"]).read_bytes() for item in manifest["artifacts"]}


@pytest.mark.asyncio
async def test_full_bodies_output_sidecars_and_identity_survive_report_cleanup(tmp_path):
    reports = tmp_path / "reports"
    original = report(reports)
    sidecars = ev._gate_tmp_directory(reports) / "test_one"
    sidecars.mkdir(parents=True)
    for suffix in ("", ".wakeup", ".diagnostics", ".stacks"):
        (sidecars / ("child-progress" + suffix)).write_text("start\n" + "trace\n" * 2000 + "end\n")
    result = await retain(tmp_path)
    assert result.exit_code == 1
    assert result.counts == ev.TestCounts(1, 0, 1, 0, 0)
    assert not result.retention_truncated
    assert result.retention_error is None
    data = retained(result)
    assert data["junit.xml"] == original
    assert data["stdout"] == b"full stdout\n" * 900
    assert data["stderr"] == b"full stderr"
    for suffix in ("", ".wakeup", ".diagnostics", ".stacks"):
        assert data["tmp/test_one/child-progress" + suffix].endswith(b"end\n")
    manifest = json.loads(Path(result.retention_manifest).read_text())
    assert (manifest["issue"], manifest["attempt"], manifest["run_id"], manifest["phase"]) == (1754, 4, "run123", "initial")
    ev.shutil.rmtree(reports)
    assert all(Path(path).is_file() and reports not in Path(path).parents for path in result.retained_artifacts)
    assert all(Path(path).stat().st_mode & 0o777 == 0o600 for path in result.retained_artifacts)


@pytest.mark.asyncio
async def test_missing_tmp_is_known_empty(tmp_path):
    result = await retain(tmp_path)
    assert not result.retention_truncated
    assert set(retained(result)) == {"junit.xml", "stdout", "stderr"}


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [PermissionError, OSError, RuntimeError])
async def test_tmp_probe_failure_does_not_change_verdict(tmp_path, monkeypatch, exception, caplog):
    original = Path.lstat

    def probe(path):
        if path == tmp_path / "reports-tmp":
            raise exception("injected probe failure")
        return original(path)

    monkeypatch.setattr(Path, "lstat", probe)
    result = await retain(tmp_path)
    assert result.exit_code == 1
    assert result.counts.failed == 1
    assert result.retention_truncated
    assert ("tmp_unavailable" if issubclass(exception, OSError) else "retention_failed:RuntimeError") in result.retention_error
    assert "retention incomplete" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["export", "artifact_open", "artifact_write", "json"])
async def test_retention_exceptions_are_diagnostic_only(tmp_path, monkeypatch, target):
    directory = tmp_path / "reports"
    report(directory)
    ev._gate_tmp_directory(directory).mkdir()

    def fail(*args, **kwargs):
        raise RuntimeError("injected retention failure")

    if target == "export":
        monkeypatch.setattr(ev, "_export_gate_tmp", fail)
    elif target == "artifact_open":
        monkeypatch.setattr(output_capture, "open_output_sink", fail)
    elif target == "artifact_write":
        monkeypatch.setattr(output_capture.OutputSink, "file", property(fail))
    else:
        monkeypatch.setattr(ev.json, "dumps", fail)
    result = await retain(tmp_path)
    assert result.exit_code == 1
    assert result.counts.failed == 1
    assert result.retention_truncated
    assert result.retention_error == "retention_failed:RuntimeError"
    assert result.retention_manifest is None


@pytest.mark.asyncio
async def test_shared_byte_budget_omits_entire_files(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    report(reports)
    (reports / "junit.xml").write_bytes(b"j" * 100)
    tree = ev._gate_tmp_directory(reports)
    tree.mkdir()
    (tree / "large").write_bytes(b"z" * 20_000)
    (tree / "small").write_bytes(b"small")
    monkeypatch.setattr(ev, "_GATE_RETENTION_BYTES", 15_000)
    result = await retain(tmp_path)
    assert result.retention_truncated
    assert "byte_limit" in result.retention_error
    data = retained(result)
    assert data["junit.xml"] == b"j" * 100
    assert data["stdout"] == b"full stdout\n" * 900
    assert data["tmp/small"] == b"small"
    assert "tmp/large" not in data
    assert sum(Path(p).stat().st_size for p in (*result.retained_artifacts, result.retention_manifest)) <= 15_000


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_export_entry_budget_includes_directories_and_empty_files(tmp_path, limit):
    tree = tmp_path / "tree"
    tree.mkdir()
    for index in range(5):
        (tree / f"file{index}").write_bytes(b"")
    result = ev._export_gate_tmp(str(tree), 100, limit, 32)
    assert result["entries"] == limit
    assert len(result["files"]) == limit
    assert result["reasons"] == ["entry_limit"]


def test_export_depth_boundary_and_unsafe_entries(tmp_path):
    tree = tmp_path / "tree"
    (tree / "one/two").mkdir(parents=True)
    (tree / "one/file").write_bytes(b"at boundary")
    (tree / "one/two/file").write_bytes(b"too deep")
    (tree / "link").symlink_to(tmp_path / "outside")
    os.mkfifo(tree / "pipe")
    result = ev._export_gate_tmp(str(tree), 100, 20, 2)
    assert result["files"] == [{"name": "one/file", "data": base64.b64encode(b"at boundary").decode()}]
    assert set(result["reasons"]) == {"depth_limit", "unsafe_entry"}


@pytest.mark.asyncio
async def test_compute_output_uses_durable_files_not_excerpts(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(b"complete output\n" * 2000)
    result = await retain(tmp_path, compute_result=ComputeResult(1, "excerpt", "", stdout_path=path))
    assert retained(result)["stdout"] == path.read_bytes()


@pytest.mark.asyncio
async def test_redacts_retained_text(tmp_path):
    tree = tmp_path / "reports-tmp"
    tree.mkdir()
    (tree / "diagnostics").write_text("token=top-secret\n")
    result = await retain(tmp_path)
    assert b"top-secret" not in retained(result)["tmp/diagnostics"]


def test_pytest_temp_is_sibling_and_options_keep_sidecars(tmp_path):
    directory = tmp_path / "report space"
    env = ev.pytest_report_environment("pytest", directory, existing="-q")
    options = shlex.split(env["PYTEST_ADDOPTS"])
    basetemp = Path(next(item.split("=", 1)[1] for item in options if item.startswith("--basetemp=")))
    assert basetemp == ev._gate_tmp_directory(directory)
    assert directory not in basetemp.parents
    assert not basetemp.exists(), "pytest, not the controller, must own basetemp"
    assert "tmp_path_retention_policy=all" in options
    assert options[0] == "-q"


async def observe(tmp_path, runner, **kwargs):
    return await ev.observe_evidence(
        issue=1754, attempt=4, backend=kwargs.pop("backend", "codex"), branch="branch",
        checkout=tmp_path, started_at=datetime.now(UTC), base_ref="main",
        backend_status="completed", test_command="pytest -q", runner=runner,
        work_spec=spec(tmp_path), **kwargs,
    )


def git_result(command):
    return subprocess.CompletedProcess(command, 0, "changed.py\n" if "--name-only" in command else "", "")


@pytest.mark.asyncio
async def test_run_gate_retains_initial_and_passing_rerun_before_cleanup(tmp_path):
    reports = []

    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        options = shlex.split(shlex.split(command)[0].split("=", 1)[1])
        directory = Path(next(item.split("=", 1)[1] for item in options if item.startswith("--junitxml="))).parent
        reports.append(directory)
        report(directory, failed=len(reports) == 1)
        return subprocess.CompletedProcess(command, int(len(reports) == 1), "complete gate output", "")

    result = await observe(tmp_path, runner)
    test = result.evidence.tests
    assert result.reasons == ("tests_failed",)
    assert test.exit_code == 1
    assert test.rerun.exit_code == 0
    assert test.initial_run.gate_run_id == test.rerun.gate_run_id
    assert test.initial_run.gate_phase == "initial"
    assert test.rerun.gate_phase == "rerun"
    assert test.initial_run.retention_manifest != test.rerun.retention_manifest
    assert retained(test.initial_run)["stdout"] == retained(test.rerun)["stdout"] == b"complete gate output"
    assert all(not directory.exists() for directory in reports)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("gate crashed"), asyncio.CancelledError()])
async def test_true_gate_exceptions_propagate(tmp_path, error):
    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        raise error

    with pytest.raises(type(error)):
        await observe(tmp_path, runner)
    assert not (tmp_path / "artifacts").exists()


class ExportCompute:
    def __init__(self, checkout, failure=None):
        self.checkout = checkout
        self.failure = failure
        self.specs = []

    async def launch(self, work):
        self.specs.append(work)
        return LaunchHandle("test", str(len(self.specs)))

    async def wait(self, handle, timeout):
        if self.failure:
            raise self.failure
        work = self.specs[-1]
        process = subprocess.run(work.local_argv, cwd=self.checkout, capture_output=True, text=True, timeout=timeout)
        path = work.output_root / "export.stdout.log"
        sink = output_capture.open_output_sink(path, 2 * ev._GATE_RETENTION_BYTES)
        try:
            sink.file.write(process.stdout.encode())
        finally:
            sink.close()
        return ComputeResult(process.returncode, process.stdout[:100], process.stderr, stdout_path=path)

    async def cleanup(self, handle):
        pass


@pytest.mark.asyncio
async def test_worker_export_executes_isolated_python_and_removes_private_tree(tmp_path):
    tree = tmp_path / "reports-tmp"
    tree.mkdir(mode=0o700)
    (tree / "private").mkdir(mode=0o700)
    (tree / "private/child-progress.stacks").write_bytes(b"whole stack")
    compute = ExportCompute(tmp_path)
    result = await retain(tmp_path, compute=compute, worker_uid_drop=True)
    assert not result.retention_truncated
    assert retained(result)["tmp/private/child-progress.stacks"] == b"whole stack"
    assert not tree.exists()
    assert "python3 -I -c" in compute.specs[0].local_argv[-1]
    assert "PYTEST_ADDOPTS" not in compute.specs[0].env


@pytest.mark.asyncio
async def test_large_worker_export_reads_full_private_sink_before_cleanup(tmp_path):
    tree = tmp_path / "reports-tmp"
    tree.mkdir(mode=0o700)
    body = b"complete stack frame\n" * 100_000
    (tree / "child-progress.stacks").write_bytes(body)

    class Compute(ExportCompute):
        async def wait(self, handle, timeout):
            root = self.specs[-1].output_root
            assert root.is_absolute() and root.resolve(strict=True) == root
            assert root.stat().st_uid == os.geteuid()
            assert root.stat().st_mode & 0o777 == 0o700
            assert not root.is_relative_to(tmp_path)
            result = await super().wait(handle, timeout)
            assert result.stdout_path.stat().st_size > len(body)
            # A plausible but false empty excerpt must not replace the full file.
            return replace(result, stdout='{"files": [], "reasons": [], "entries": 0}')

    compute = Compute(tmp_path)
    result = await retain(tmp_path, compute=compute, worker_uid_drop=True)
    assert result.exit_code == 1
    assert not result.retention_truncated
    assert retained(result)["tmp/child-progress.stacks"] == body
    try:
        assert not compute.specs[0].output_root.exists()
    finally:
        ev.shutil.rmtree(compute.specs[0].output_root, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["missing", "outside", "symlink", "oversize"])
async def test_export_transport_refuses_untrusted_or_unavailable_files(tmp_path, path_kind):
    (tmp_path / "reports-tmp").mkdir()

    class Compute(ExportCompute):
        async def wait(self, handle, timeout):
            result = await super().wait(handle, timeout)
            path = result.stdout_path
            if path_kind == "missing":
                path = None
            elif path_kind in {"outside", "symlink"}:
                outside = tmp_path / "outside.json"
                outside.write_text('{"files": [], "reasons": [], "entries": 0}')
                if path_kind == "outside":
                    path = outside
                else:
                    path.unlink()
                    path.symlink_to(outside)
            else:
                with path.open("wb") as destination:
                    destination.truncate(2 * ev._GATE_RETENTION_BYTES + 1)
            return replace(result, stdout_path=path, stdout='{"files": [], "reasons": [], "entries": 0}')

    compute = Compute(tmp_path)
    result = await retain(tmp_path, compute=compute, worker_uid_drop=True)
    assert result.exit_code == 1
    assert result.retention_truncated
    assert result.retention_error.startswith("retention_failed:")
    assert len(result.retained_artifacts) == 3
    assert not compute.specs[0].output_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["reports/artifacts", "reports-tmp/artifacts", "elsewhere/../reports/artifacts", "relative"])
async def test_retention_rejects_destinations_deleted_during_cleanup(tmp_path, destination):
    directory = tmp_path / "reports"
    report(directory)
    directory.chmod(0o700)
    result = await ev._retain_gate_failure(
        ev.TestResult("pytest", 1), subprocess.CompletedProcess("pytest", 1, "output", ""),
        report_dir=directory, issue=1754, attempt=4, run_id="run123", phase="initial",
        checkout=tmp_path, work_spec=replace(spec(tmp_path), output_root=Path(destination) if destination == "relative" else tmp_path / destination),
        compute=None, compute_result=None, worker_uid_drop=False,
    )
    assert result.exit_code == 1
    assert result.retention_truncated
    assert not result.retained_artifacts
    assert result.retention_manifest is None


@pytest.mark.asyncio
async def test_worker_export_failure_leaves_verdict_and_primary_artifacts(tmp_path):
    (tmp_path / "reports-tmp").mkdir()
    result = await retain(tmp_path, compute=ExportCompute(tmp_path, RuntimeError("export failed")), worker_uid_drop=True)
    assert result.exit_code == 1
    assert result.retention_truncated
    assert result.retention_error == "retention_failed:RuntimeError"
    assert len(result.retained_artifacts) == 3


@pytest.mark.asyncio
async def test_nonpytest_worker_retention_uses_worker_export_and_controller_sinks(tmp_path):
    tree = tmp_path / "reports-tmp"
    tree.mkdir(mode=0o700)
    (tree / "diagnostics").write_bytes(b"complete sidecar")
    result = await retain(tmp_path, command="false", compute=ExportCompute(tmp_path), worker_uid_drop=True)
    assert not result.retention_truncated
    assert retained(result) == {
        "stdout": b"full stdout\n" * 900,
        "stderr": b"full stderr",
        "tmp/diagnostics": b"complete sidecar",
    }
    assert not tree.exists()


@pytest.mark.asyncio
async def test_successful_initial_gate_does_not_retain_artifacts(tmp_path):
    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        return subprocess.CompletedProcess(command, 0, "passed", "")

    result = await observe(tmp_path, runner)
    assert result.review_ready
    assert not result.evidence.tests.retained_artifacts
    assert result.evidence.tests.retention_manifest is None


@pytest.mark.asyncio
async def test_retention_entrypoint_exception_cannot_escape_run_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(ev.log, "warning", _fail_log)
    async def fail(*args, **kwargs):
        raise RuntimeError("retention entrypoint failed")

    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        return subprocess.CompletedProcess(command, 1, "failed", "")

    monkeypatch.setattr(ev, "_retain_gate_failure", fail)
    result = await observe(tmp_path, runner)
    assert result.reasons == ("tests_failed",)
    assert result.evidence.tests.exit_code == 1
    assert result.evidence.tests.retention_error == "retention_failed:RuntimeError"


@pytest.mark.asyncio
async def test_true_compute_exception_is_not_a_retention_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="gate execution"):
        await observe(
            tmp_path, lambda command, **kwargs: git_result(command), backend="opencode",
            compute=ExportCompute(tmp_path, RuntimeError("gate execution")),
        )


@pytest.mark.asyncio
async def test_report_cleanup_failure_is_recorded_without_changing_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(ev.log, "warning", _fail_log)
    cleanup = ev.tempfile.TemporaryDirectory.cleanup

    def fail(directory):
        cleanup(directory)
        raise PermissionError("cleanup failed")

    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        return subprocess.CompletedProcess(command, 0, "passed", "")

    monkeypatch.setattr(ev.tempfile.TemporaryDirectory, "cleanup", fail)
    result = await observe(tmp_path, runner)
    assert result.review_ready
    assert result.evidence.tests.retention_truncated
    assert result.evidence.tests.retention_error == "report_cleanup_failed:PermissionError"


def test_report_cleanup_failure_does_not_mask_true_gate_exception(tmp_path, monkeypatch):
    cleanup = ev.tempfile.TemporaryDirectory.cleanup

    def fail(directory):
        cleanup(directory)
        raise PermissionError("cleanup failed")

    monkeypatch.setattr(ev.tempfile.TemporaryDirectory, "cleanup", fail)
    with pytest.raises(RuntimeError, match="true gate error"):
        with ev._gate_report_directory(tmp_path, False):
            raise RuntimeError("true gate error")


@pytest.mark.asyncio
async def test_manifest_write_failure_preserves_complete_artifacts_only(tmp_path, monkeypatch):
    original = output_capture.OutputSink.file.fget

    def writer(sink):
        if sink.path.suffix == ".json":
            raise RuntimeError("manifest write failed")
        return original(sink)

    monkeypatch.setattr(output_capture.OutputSink, "file", property(writer))
    result = await retain(tmp_path)
    assert result.retention_truncated
    assert result.retention_manifest is None
    assert len(result.retained_artifacts) == 3
    assert not list((tmp_path / "artifacts").glob("*.json"))


@pytest.mark.asyncio
async def test_real_pytest_sidecars_are_retained_and_temp_directory_removed(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text(
        "def test_one(tmp_path):\n"
        "    (tmp_path / 'child-progress.stacks').write_text('whole stack')\n"
        "    assert False, 'complete assertion body'\n"
    )
    directories = []

    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        options = shlex.split(shlex.split(command)[0].split("=", 1)[1])
        directories.append(Path(next(arg.split("=", 1)[1] for arg in options if arg.startswith("--basetemp="))))
        return ev._run(command, **kwargs)

    result = await ev.observe_evidence(
        issue=1754, attempt=4, backend="codex", branch="branch", checkout=tmp_path,
        started_at=datetime.now(UTC), base_ref="main", backend_status="completed",
        test_command=shlex.join([sys.executable, "-m", "pytest", "-q", "test_sample.py"]),
        runner=runner, work_spec=spec(tmp_path), gate_rerun_max_failures=0,
    )
    test = result.evidence.tests
    assert test.exit_code == 1
    assert test.counts.failed == 1
    # Pytest also creates test_onecurrent, a symlink alias of the retained tree.
    assert test.retention_error == "unsafe_entry"
    data = retained(test)
    assert b"complete assertion body" in data["junit.xml"]
    assert any(name.endswith("child-progress.stacks") and body == b"whole stack" for name, body in data.items())
    assert all(not directory.exists() for directory in directories)


def test_default_caps():
    assert ev._GATE_RETENTION_BYTES == 32 * 1024 * 1024
    assert ev._GATE_RETENTION_ENTRIES == 2048
    assert ev._GATE_RETENTION_DEPTH == 32


@pytest.mark.parametrize("component", ["ancestor", "root", "directory", "file"])
def test_export_rejects_symlink_swaps_after_stat(tmp_path, monkeypatch, component):
    anchor = tmp_path / "anchor"
    tree = anchor / "tree"
    (tree / "branch").mkdir(parents=True)
    (tree / "branch/payload").write_bytes(b"original")
    outside = tmp_path / "outside"
    (outside / "tree/branch").mkdir(parents=True)
    (outside / "tree/branch/payload").write_bytes(b"must not export")
    target, replacement = {
        "ancestor": (anchor, outside),
        "root": (tree, outside / "tree"),
        "directory": (tree / "branch", outside / "tree/branch"),
        "file": (tree / "branch/payload", outside / "tree/branch/payload"),
    }[component]
    original_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == target.name:
            swapped = True
            target.rename(target.with_name(target.name + "-saved"))
            target.symlink_to(replacement)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    try:
        result = ev._export_gate_tmp(str(tree), 1000, 20, 32)
    except OSError:
        assert component in {"ancestor", "root"}
    else:
        assert result["files"] == []
        assert "tmp_read_error" in result["reasons"]
    assert swapped


@pytest.mark.timeout(3)
def test_export_fifo_swap_is_nonblocking_and_rejected_by_fstat(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tree / "payload"
    target.write_bytes(b"")
    original_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "payload":
            swapped = True
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    result = ev._export_gate_tmp(str(tree), 100, 10, 32)
    assert swapped
    assert result["files"] == []
    assert result["reasons"] == ["unsafe_entry"]


@pytest.mark.parametrize("change", ["grow", "shrink", "postgrow", "oversize"])
def test_export_rejects_changed_files_and_bounds_reads(tmp_path, monkeypatch, change):
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tree / "payload"
    target.write_bytes(b"original")
    original_fdopen = os.fdopen
    reads = []

    class Source:
        def __init__(self, fd, *args, **kwargs):
            self.source = original_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, size=-1):
            reads.append(size)
            assert size == 17, "export reads must be bounded, including growth"
            if change == "grow":
                target.write_bytes(b"grown payload")
            elif change == "shrink":
                target.write_bytes(b"short")
            elif change == "oversize":
                target.write_bytes(b"x" * 100)
            data = self.source.read(size)
            if change == "postgrow":
                target.write_bytes(b"longer after reading")
            return data

    monkeypatch.setattr(os, "fdopen", Source)
    result = ev._export_gate_tmp(str(tree), 16, 10, 32)
    assert reads == [17]
    assert result["files"] == []
    assert result["reasons"] == ["file_changed_or_oversize"]


def test_export_oversize_file_is_not_read(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "payload").write_bytes(b"x" * 101)
    result = ev._export_gate_tmp(str(tree), 100, 10, 32)
    assert result["files"] == []
    assert result["reasons"] == ["byte_limit"]


@pytest.mark.parametrize("root_kind", ["missing", "symlink", "file", "unavailable", "dotdot", "dot", "empty"])
def test_export_root_probe_and_components(tmp_path, monkeypatch, root_kind):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "payload").write_bytes(b"data")
    path = str(tree)
    if root_kind == "missing":
        path = str(tmp_path / "missing")
    elif root_kind == "symlink":
        (tmp_path / "link").symlink_to(tree)
        path = str(tmp_path / "link")
    elif root_kind == "file":
        path = str(tree / "payload")
    elif root_kind == "unavailable":
        original_stat = os.stat

        def fail(path, *args, **kwargs):
            if path == str(tree):
                raise PermissionError("probe unavailable")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", fail)
    else:
        path += {"dotdot": "/../tree", "dot": "/.", "empty": "/"}[root_kind]
        with pytest.raises(ValueError, match="invalid export root"):
            ev._export_gate_tmp(path, 100, 10, 32)
        return
    result = ev._export_gate_tmp(path, 100, 10, 32)
    assert result["files"] == []
    assert result["reasons"] == {
        "missing": [], "symlink": ["tmp_not_directory"], "file": ["tmp_not_directory"],
        "unavailable": ["tmp_unavailable"],
    }[root_kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("exit_code", 1), ("timed_out", True), ("output_overflow", True)])
async def test_export_subprocess_failure_flags_are_not_accepted(tmp_path, field, value):
    (tmp_path / "reports-tmp").mkdir()

    class Compute(ExportCompute):
        async def wait(self, handle, timeout):
            result = await super().wait(handle, timeout)
            return replace(result, **{field: value})

    compute = Compute(tmp_path)
    result = await retain(tmp_path, compute=compute, worker_uid_drop=True)
    assert result.exit_code == 1
    assert result.retention_truncated
    assert result.retention_manifest is None
    assert not compute.specs[0].output_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["byte", "entry", "depth", "base64"])
async def test_controller_rechecks_export_limits(tmp_path, monkeypatch, kind):
    (tmp_path / "reports-tmp").mkdir()
    record = {"name": "payload", "data": base64.b64encode(b"x" * 200_000).decode()}
    if kind == "byte":
        monkeypatch.setattr(ev, "_GATE_RETENTION_BYTES", 50_000)
    elif kind == "entry":
        monkeypatch.setattr(ev, "_GATE_RETENTION_ENTRIES", 4)
    elif kind == "depth":
        record["name"] = "/".join(["deep"] * 33)
    else:
        record["data"] = "!!!"
    monkeypatch.setattr(ev, "_export_gate_tmp", lambda *args: {"files": [record], "reasons": [], "entries": 1})
    result = await retain(tmp_path)
    assert result.retention_truncated
    assert len(result.retained_artifacts) == 3
    if kind != "base64":
        assert kind + "_limit" in result.retention_error
    else:
        assert "retention_failed" in result.retention_error


@pytest.mark.asyncio
async def test_primary_output_and_manifest_share_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_GATE_RETENTION_BYTES", 100)
    result = await retain(tmp_path, command="false")
    assert result.retention_truncated
    assert "byte_limit" in result.retention_error
    assert "manifest_byte_limit" in result.retention_error
    assert sum(Path(path).stat().st_size for path in result.retained_artifacts) <= 100
    assert result.retention_manifest is None


@pytest.mark.asyncio
async def test_primary_output_entry_limit_reserves_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_GATE_RETENTION_ENTRIES", 1)
    result = await retain(tmp_path, command="false")
    assert result.retention_truncated
    assert result.retention_error == "entry_limit"
    assert result.retained_artifacts == ()
    assert result.retention_manifest is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".artifact", ".json"])
async def test_partial_writes_are_unlinked_and_owned_sinks_closed(tmp_path, monkeypatch, suffix):
    opened = []
    original_open = output_capture.open_output_sink
    original_writer = output_capture.OutputSink.file.fget

    def opened_sink(*args):
        sink = original_open(*args)
        opened.append(sink)
        return sink

    def writer(sink):
        underlying = original_writer(sink)
        if sink.path.suffix != suffix:
            return underlying

        class Writer:
            def write(self, data):
                underlying.write(data[:1])
                raise OSError("partial write")

        return Writer()

    monkeypatch.setattr(output_capture, "open_output_sink", opened_sink)
    monkeypatch.setattr(output_capture.OutputSink, "file", property(writer))
    result = await retain(tmp_path)
    assert result.exit_code == 1
    assert result.retention_truncated
    assert opened and all(sink.fd == -1 for sink in opened)
    assert not list((tmp_path / "artifacts").glob("*" + suffix))


@pytest.mark.asyncio
async def test_primary_read_error_keeps_other_artifacts(tmp_path):
    directory = tmp_path / "reports"
    report(directory)
    (directory / "junit.xml").unlink()
    (directory / "junit.xml").symlink_to(directory / "cache/v/cache/nodeids")
    result = await retain(tmp_path)
    assert result.retention_truncated
    assert result.retention_error.startswith("junit.xml:")
    assert set(retained(result)) == {"stdout", "stderr"}


@pytest.mark.asyncio
async def test_output_overflow_is_incomplete_even_with_readable_output(tmp_path):
    result = await retain(tmp_path, compute_result=ComputeResult(1, "output", "", output_overflow=True))
    assert result.retention_truncated
    assert result.retention_error == "output_overflow"


@pytest.mark.asyncio
async def test_retention_logging_failure_cannot_escape(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("logger failed")

    monkeypatch.setattr(ev.log, "warning", fail)
    result = await retain(tmp_path, compute_result=ComputeResult(1, "output", "", output_overflow=True))
    assert result.exit_code == 1
    assert result.retention_truncated


@pytest.mark.timeout(3)
@pytest.mark.parametrize("component", ["root", "directory"])
def test_export_directory_fifo_swap_requires_directory_open(tmp_path, monkeypatch, component):
    tree = tmp_path / "tree"
    (tree / "branch").mkdir(parents=True)
    target = tree if component == "root" else tree / "branch"
    original_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == target.name:
            swapped = True
            target.rename(target.with_name(target.name + "-saved"))
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    try:
        result = ev._export_gate_tmp(str(tree), 100, 10, 32)
    except OSError:
        assert component == "root"
    else:
        assert result["files"] == []
        assert result["reasons"] == ["tmp_read_error"]
    assert swapped


def test_export_budget_is_shared_between_sibling_files_and_directories(tmp_path):
    tree = tmp_path / "tree"
    (tree / "branch").mkdir(parents=True)
    for name in ("one", "two"):
        (tree / "branch" / name).write_bytes(b"x" * 60)
    result = ev._export_gate_tmp(str(tree), 100, 10, 32)
    assert result["entries"] == 3
    assert len(result["files"]) == 1
    assert result["reasons"] == ["byte_limit"]
    result = ev._export_gate_tmp(str(tree), 100, 1, 32)
    assert result["files"] == []
    assert result["entries"] == 1
    assert result["reasons"] == ["entry_limit"]


@pytest.mark.asyncio
async def test_controller_counts_primary_bytes_before_next_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_GATE_RETENTION_BYTES", 20_000)
    result = await retain(tmp_path)
    assert result.retention_error == "byte_limit"
    assert set(retained(result)) == {"junit.xml", "stderr"}
    assert sum(Path(p).stat().st_size for p in (*result.retained_artifacts, result.retention_manifest)) <= 20_000


@pytest.mark.asyncio
async def test_controller_stops_before_decoding_entries_over_budget(tmp_path, monkeypatch):
    (tmp_path / "reports-tmp").mkdir()
    monkeypatch.setattr(ev, "_GATE_RETENTION_ENTRIES", 4)
    monkeypatch.setattr(ev, "_export_gate_tmp", lambda *args: {
        "files": [{"name": "payload", "data": "!invalid!"}], "reasons": [], "entries": 1,
    })
    result = await retain(tmp_path)
    assert result.retention_error == "entry_limit"
    assert result.retention_manifest is not None


@pytest.mark.asyncio
async def test_transport_byte_limit_rejects_oversize_valid_json(tmp_path, monkeypatch):
    (tmp_path / "reports-tmp").mkdir()
    monkeypatch.setattr(ev, "_GATE_RETENTION_BYTES", 50_000)

    class Compute(ExportCompute):
        async def wait(self, handle, timeout):
            result = await super().wait(handle, timeout)
            result.stdout_path.write_bytes(b" " * 100_000 + b'{"files":[],"reasons":[],"entries":0}')
            return result

    result = await retain(tmp_path, compute=Compute(tmp_path), worker_uid_drop=True)
    assert result.retention_truncated
    assert result.retention_manifest is None


@pytest.mark.asyncio
async def test_manifest_source_names_are_redacted(tmp_path):
    (tmp_path / "reports-tmp").mkdir()
    (tmp_path / "reports-tmp/token=top-secret").write_text("body")
    result = await retain(tmp_path)
    assert "top-secret" not in Path(result.retention_manifest).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_tmp_cleanup_failure_preserves_passing_gate(tmp_path, monkeypatch, worker):
    monkeypatch.setattr(ev.log, "warning", _fail_log)
    def runner(command, **kwargs):
        if not isinstance(command, str):
            return git_result(command)
        return subprocess.CompletedProcess(command, 0, "passed", "")

    if worker:
        monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")

        class Compute:
            async def launch(self, work):
                self.work = work
                return LaunchHandle("test", "cleanup")

            async def wait(self, handle, timeout):
                if "import shutil" in self.work.local_argv[-1]:
                    return ComputeResult(1, "", "cleanup failed")
                options = shlex.split(self.work.env["PYTEST_ADDOPTS"])
                path = next(word.split("=", 1)[1] for word in options if word.startswith("--basetemp="))
                (tmp_path / path).mkdir()
                return ComputeResult(0, "passed", "")

            async def cleanup(self, handle):
                pass

        kwargs = {"backend": "opencode", "compute": Compute()}
    else:
        rmtree = ev.shutil.rmtree

        def fail(path, *args, **kwargs):
            if str(path).endswith("-tmp"):
                raise PermissionError("private tmp cleanup failed")
            return rmtree(path, *args, **kwargs)

        monkeypatch.setattr(ev.shutil, "rmtree", fail)
        kwargs = {}
    result = await observe(tmp_path, runner, **kwargs)
    assert result.review_ready
    assert result.evidence.tests.retention_truncated
    assert result.evidence.tests.retention_error == "tmp_cleanup_failed"


def _fail_log(*args, **kwargs):
    raise RuntimeError("logging unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_export_capture_root_is_canonical_before_compute(tmp_path, monkeypatch, missing):
    (tmp_path / "reports-tmp").mkdir()
    temporary_directory = ev.tempfile.TemporaryDirectory

    @contextlib.contextmanager
    def aliased_directory(*args, **kwargs):
        with temporary_directory(*args, **kwargs) as text:
            path = Path(text)
            if missing:
                path.rmdir()
                yield text
                return
            alias = tmp_path / "controller-alias"
            alias.symlink_to(path.parent)
            yield str(alias / path.name)

    monkeypatch.setattr(ev.tempfile, "TemporaryDirectory", aliased_directory)
    compute = ExportCompute(tmp_path)
    result = await retain(tmp_path, compute=compute, worker_uid_drop=True)
    if missing:
        assert result.retention_truncated
        assert compute.specs == [], "a missing controller capture root must not be reprovisioned by compute"
        return
    assert not result.retention_truncated
    assert "controller-alias" not in str(compute.specs[0].output_root)
    assert not compute.specs[0].output_root.exists()
