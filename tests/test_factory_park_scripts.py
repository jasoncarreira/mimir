"""Tests for the hand-park and resume operator scripts.

Both failures these guard against were observed on chainlink #1783 attempt 9:
the snapshot published under the sandbox instead of the checkout, and mimir's
retained record left reading ``running`` so cleanup pruned a parked run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from mimir.worklink import orchestrator
from mimir.worklink.backends.feature_factory import FactoryStatus
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.factory_state import (
    FactoryRunRecord,
    factory_process_is_alive,
    save_factory_record,
)
from mimir.worklink.run_state import process_start_ticks

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


park = _load("factory_park")
resume = _load("factory_resume")


def _parked_dir(tmp_path: Path) -> Path:
    """The snapshot root, under a checkout that exists as it would in reality."""
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    return checkout / ".factory" / ".parked"


def _plane(root: Path) -> Path:
    """Build a control plane resembling a live run's."""
    plane = root
    plane.mkdir(parents=True)
    (plane / "run.json").write_text('{"status": "running"}')
    (plane / "WORKFLOW.md").write_text("contract")
    (plane / "factory.lock").write_text("heartbeat-1")
    (plane / "artifacts").mkdir()
    (plane / "artifacts" / "technical-brief.md").write_text("brief")
    (plane / "reviews").mkdir()
    (plane / "reviews" / "spec-writer.json").write_text('{"verdict": "APPROVE"}')
    return plane


class TestOperatorRoot:
    def test_snapshot_root_is_the_checkout_not_the_sandbox(self, tmp_path: Path) -> None:
        """The factory reads <checkout>/.factory/.parked, one level above the sandbox.

        Publishing under the sandbox's own .factory produces a byte-correct
        snapshot that ``observedParkSnapshot`` never acknowledges.
        """
        checkout = tmp_path / "checkout"
        sandbox = checkout / ".factory-sandboxes" / "chainlink-1"
        sandbox.mkdir(parents=True)

        assert park.operator_root(sandbox) == checkout.resolve()
        assert park.operator_root(sandbox) != sandbox

    def test_sandbox_of_another_shape_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        stray = tmp_path / "somewhere" / "chainlink-1"
        stray.mkdir(parents=True)
        with pytest.raises(park.ParkError, match="refusing to guess"):
            park.operator_root(stray)


class TestPublishSnapshot:
    def test_publishes_a_verified_copy_excluding_only_the_plane_root_lock(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        published = park.publish_snapshot(plane, parked, "chainlink-1")

        assert published == parked / "chainlink-1"
        assert (published / "artifacts" / "technical-brief.md").read_text() == "brief"
        assert (published / "reviews" / "spec-writer.json").read_text() == '{"verdict": "APPROVE"}'
        # The lock is copied, but excluded from the comparison that gates publication.
        assert (published / "factory.lock").exists()

    def test_a_heartbeat_landing_mid_copy_does_not_fail_publication(self, tmp_path: Path, monkeypatch) -> None:
        """Only the plane-root factory.lock is excluded, and that is why."""
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        real_inventory = park.plane_inventory
        calls = {"n": 0}

        def ticking_inventory(root: Path):
            calls["n"] += 1
            if calls["n"] == 1:
                (plane / "factory.lock").write_text("heartbeat-2")
            return real_inventory(root)

        monkeypatch.setattr(park, "plane_inventory", ticking_inventory)
        published = park.publish_snapshot(plane, parked, "chainlink-1")
        assert published.is_dir()

    def test_a_nested_factory_lock_is_run_state_and_must_match(self, tmp_path: Path, monkeypatch) -> None:
        plane = _plane(tmp_path / "plane")
        (plane / "artifacts" / "factory.lock").write_text("nested-run-state")
        parked = _parked_dir(tmp_path)

        real_inventory = park.plane_inventory
        calls = {"n": 0}

        def corrupting_inventory(root: Path):
            calls["n"] += 1
            if calls["n"] == 1:
                (plane / "artifacts" / "factory.lock").write_text("changed-after-copy")
            return real_inventory(root)

        monkeypatch.setattr(park, "plane_inventory", corrupting_inventory)
        with pytest.raises(park.ParkError, match="inventory mismatch"):
            park.publish_snapshot(plane, parked, "chainlink-1")
        assert not (parked / "chainlink-1").exists()
        assert not (parked / ".staging-chainlink-1").exists()

    def test_nothing_is_published_when_verification_fails(self, tmp_path: Path, monkeypatch) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        monkeypatch.setattr(park, "plane_inventory", lambda root: [("differs", "file", 0o644, str(root))])
        with pytest.raises(park.ParkError, match="nothing published"):
            park.publish_snapshot(plane, parked, "chainlink-1")
        assert not (parked / "chainlink-1").exists()

    def test_a_residual_staging_tree_refuses_rather_than_overwriting(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)
        (parked / ".staging-chainlink-1").mkdir(parents=True)

        with pytest.raises(park.ParkError, match="residual staging"):
            park.publish_snapshot(plane, parked, "chainlink-1")

    def test_republishing_replaces_the_previous_snapshot(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)
        park.publish_snapshot(plane, parked, "chainlink-1")

        (plane / "artifacts" / "technical-brief.md").write_text("revised brief")
        published = park.publish_snapshot(plane, parked, "chainlink-1")

        assert (published / "artifacts" / "technical-brief.md").read_text() == "revised brief"
        assert not (parked / ".prior-chainlink-1").exists()

    def test_symlinks_are_preserved_as_symlinks(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        (plane / "artifacts" / "latest.md").symlink_to("technical-brief.md")
        parked = _parked_dir(tmp_path)

        published = park.publish_snapshot(plane, parked, "chainlink-1")
        assert (published / "artifacts" / "latest.md").is_symlink()
        assert os.readlink(published / "artifacts" / "latest.md") == "technical-brief.md"

    def test_refuses_to_write_through_a_symlinked_parent(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        control = tmp_path / "checkout" / ".factory"
        control.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        control.symlink_to(elsewhere)

        with pytest.raises(park.ParkError, match="symlinked parent"):
            park.publish_snapshot(plane, control / ".parked", "chainlink-1")


class TestPlaneInventory:
    def test_records_mode_so_a_permission_change_is_a_mismatch(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        before = park.plane_inventory(plane)
        (plane / "run.json").chmod(0o600)
        after = park.plane_inventory(plane)
        assert before != after

    def test_records_content_so_an_edit_is_a_mismatch(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        before = park.plane_inventory(plane)
        (plane / "run.json").write_text('{"status": "needs-human"}')
        assert park.plane_inventory(plane) != before


def _dead_pid() -> int:
    """A pid that is provably gone: spawned, waited on, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _handle(pid: int) -> LaunchHandle:
    """A launch handle with a birth marker, which verified death requires."""
    return LaunchHandle(
        substrate="local_subprocess", identifier=str(pid),
        process_start_ticks=1, shim_pid=pid,
    )


def _record(
    tmp_path: Path, *, phase: str = "parked", status: str | None = "needs-human",
    session: str | None = "ses_recorded", pid: int | None = None,
) -> FactoryRunRecord:
    """A retained record shaped as a real parked run's."""
    sandbox = tmp_path / ".factory-sandboxes" / "chainlink-1783"
    sandbox.mkdir(parents=True, exist_ok=True)
    return FactoryRunRecord(
        run_id="chainlink-1783", issue_id=1783, attempt=10,
        repository="jasoncarreira/mimir", base_ref="base/chainlink-1783",
        branch="feature/chainlink-1783", launcher="/opt/factory/factory.js",
        sandbox=str(sandbox), session=session,
        handle=_handle(_dead_pid() if pid is None else pid),
        status=None if status is None else FactoryStatus(
            run_id="chainlink-1783", valid=True, sandbox_path=str(sandbox), status=status,
        ),
        observed_at="2026-09-21T00:00:00+00:00", controller_phase=phase,
    )


def _status(
    status: str = "needs-human",
    *,
    snapshot: str | None = "/c/.factory/.parked/x",
    sandbox_path: str | None = None,
) -> dict:
    """A status payload that satisfies the real contract parser.

    ``run_id``, ``valid`` and ``sandbox_path`` are required, and the record
    rejects a status whose sandbox disagrees with its own. Callers that reconcile
    a record must therefore pass ``sandbox_path``; the preflight never reads it,
    so its callers leave the default in place.
    """
    return {
        "run_id": "chainlink-1783",
        "valid": True,
        "sandbox_path": sandbox_path or "/unused-by-this-caller",
        "status": status,
        "lock": "fresh",
        "dead_lock": False,
        "park_snapshot": snapshot,
    }


class TestResumePreflight:
    """The preflight reports mimir's own recovery preconditions.

    The bug these exist for: unparking out of band spends the ``needs-human``
    transition ``_verify_factory_recovery_target`` is gated on, after which the
    supported recovery path refuses the run and the attempt is lost.
    """

    def test_a_swept_sandbox_is_refused_with_the_reason(self, tmp_path: Path) -> None:
        argv = [
            "--run-id", "chainlink-1", "--sandbox", str(tmp_path / "gone"),
            "--home", str(tmp_path), "--launcher", str(tmp_path / "factory.js"),
        ]
        with pytest.raises(resume.ResumeError, match="not resumable"):
            resume.main(argv)

    def test_a_parked_run_passes_every_precondition(self, tmp_path: Path) -> None:
        record = _record(tmp_path)
        assert resume.preflight(record, _status(), Path(record.sandbox)) == []

    def test_an_already_unparked_run_is_refused_naming_the_cause(self, tmp_path: Path) -> None:
        """The exact failure that cost an attempt: status no longer needs-human."""
        record = _record(tmp_path, phase="running", status="running")
        problems = resume.preflight(record, _status("running"), Path(record.sandbox))
        assert any("requires 'needs-human'" in problem for problem in problems)
        assert any("factory resume" in problem for problem in problems)

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="liveness needs the /proc/PID/stat birth marker, which only Linux has",
    )
    def test_a_live_process_is_refused_rather_than_resumed_over(self, tmp_path: Path) -> None:
        """Recovery refuses a live retained process, so the preflight must too."""
        record = _record(tmp_path, pid=os.getpid())
        record = replace(
            record,
            handle=LaunchHandle(
                substrate="local_subprocess", identifier=str(os.getpid()),
                process_start_ticks=process_start_ticks(os.getpid()), shim_pid=os.getpid(),
            ),
        )
        assert factory_process_is_alive(record), "fixture does not model a live run"
        problems = resume.preflight(record, _status(), Path(record.sandbox))
        assert any("still alive" in problem for problem in problems)

    @pytest.mark.skipif(
        sys.platform != "linux", reason="proving pid reuse needs a readable /proc birth marker",
    )
    def test_a_disagreeing_birth_marker_proves_pid_reuse_on_linux(self, tmp_path: Path) -> None:
        """The recorded identity is pid *plus* birth marker, not the pid alone.

        A live pid whose marker disagrees is a reused pid, so the recorded
        process really is gone and the run is recoverable.
        """
        record = _record(tmp_path, pid=os.getpid())
        assert not factory_process_is_alive(record)
        assert resume.preflight(record, _status(), Path(record.sandbox)) == []

    @pytest.mark.skipif(
        sys.platform == "linux", reason="asserts the fallback where no birth marker is readable",
    )
    def test_without_a_readable_marker_a_live_pid_is_never_assumed_dead(
        self, tmp_path: Path
    ) -> None:
        """Where the marker cannot be read, death cannot be proven, so this fails closed.

        The conservative direction is the safe one: refusing a recoverable run
        costs a dispatch, while resuming over a live driver corrupts the run.
        """
        record = _record(tmp_path, pid=os.getpid())
        problems = resume.preflight(record, _status(), Path(record.sandbox))
        assert any("verified dead" in problem for problem in problems)

    def test_an_unverifiable_death_is_refused_separately_from_a_live_run(
        self, tmp_path: Path
    ) -> None:
        """No birth marker means pid reuse cannot be ruled out, which is not the same
        as the process being alive, and recovery rejects it for its own reason."""
        record = _record(tmp_path)
        record = replace(
            record,
            handle=LaunchHandle(
                substrate="local_subprocess", identifier=str(_dead_pid()),
                process_start_ticks=None, shim_pid=None,
            ),
        )
        problems = resume.preflight(record, _status(), Path(record.sandbox))
        assert any("verified dead" in problem for problem in problems)
        assert not any("still alive" in problem for problem in problems)

    def test_a_missing_session_is_refused_because_recovery_reuses_it(
        self, tmp_path: Path
    ) -> None:
        record = _record(tmp_path, session=None)
        problems = resume.preflight(record, _status(), Path(record.sandbox))
        assert any("recorded session" in problem for problem in problems)

    def test_an_unacknowledged_park_is_refused(self, tmp_path: Path) -> None:
        record = _record(tmp_path)
        problems = resume.preflight(record, _status(snapshot=None), Path(record.sandbox))
        assert any("park_snapshot is null" in problem for problem in problems)

    def test_recoverable_phases_come_from_the_gate_not_a_restatement(self) -> None:
        """A local copy of this set would silently drift from the code it reports on."""
        assert resume._RECOVERABLE_FACTORY_PHASES is orchestrator._RECOVERABLE_FACTORY_PHASES

    def test_an_unrecoverable_phase_names_the_recoverable_ones(self, tmp_path: Path) -> None:
        record = _record(tmp_path, phase="archived")
        problems = resume.preflight(record, _status(), Path(record.sandbox))
        assert any("recoverable phases are" in problem for problem in problems)


class TestResumeDoesNotUnpark:
    def test_the_script_never_invokes_the_factory_resume_command(self) -> None:
        """Calling 'factory resume' here is the defect, not an implementation detail.

        Recovery resumes, relaunches and supervises as one operation; unparking
        first consumes the transition it validates against.
        """
        source = (REPO_ROOT / "scripts" / "factory_resume.py").read_text()
        code = source.split('"""', 2)[2]
        assert '"resume"' not in code
        assert '"steal"' not in code

    def test_dispatch_targets_the_supported_entry_point(self, tmp_path: Path) -> None:
        record = _record(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        save_factory_record(home, record)
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["node", str(record.launcher)]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(_status()), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(resume, "_run", fake_run):
            rc = resume.main([
                "--run-id", record.run_id, "--sandbox", record.sandbox,
                "--home", str(home), "--launcher", record.launcher,
                "--repo", str(tmp_path / "controller"), "--dispatch",
            ])

        assert rc == 0
        dispatched = [cmd for cmd in calls if cmd[0] == "mimir"]
        assert dispatched == [[
            "mimir", "worklink", "run-epic", "1783",
            "--home", str(home), "--repo", str(tmp_path / "controller"), "--autonomous",
        ]]


class TestParkStopsTheControllerFirst:
    """Killing compute under a live controller makes it race the park's writes.

    The controller's own budget park is race-free because it cancels a handle it
    owns. An external park has to remove the controller before it touches
    anything the controller is awaiting.
    """

    def test_a_pid_that_is_not_this_runs_controller_is_refused(self) -> None:
        with pytest.raises(park.ParkError, match="does not look like"):
            park.verify_controller(os.getpid(), 1783)

    def test_a_dead_pid_is_refused_with_the_none_alternative(self) -> None:
        with pytest.raises(park.ParkError, match="--controller-pid none"):
            park.verify_controller(_dead_pid(), 1783)

    def test_candidate_discovery_never_matches_the_script_itself(self, monkeypatch) -> None:
        """Every token searched for appears in this script's own argv."""
        listing = subprocess.CompletedProcess(
            [], 0,
            f"{os.getpid() + 1} python scripts/factory_park.py --run-id chainlink-1783 "
            "worklink run-epic 1783\n"
            f"{os.getpid() + 2} mimir worklink run-epic 1783 --autonomous\n",
            "",
        )
        monkeypatch.setattr(park, "_run", lambda *a, **k: listing)
        assert park.find_controllers(1783) == [os.getpid() + 2]

    def test_discovery_reports_candidates_and_does_not_choose(self, monkeypatch) -> None:
        listing = subprocess.CompletedProcess(
            [], 0,
            f"{os.getpid() + 1} mimir worklink run-epic 1783 --autonomous\n"
            f"{os.getpid() + 2} mimir worklink run-epic 1783 --autonomous\n",
            "",
        )
        monkeypatch.setattr(park, "_run", lambda *a, **k: listing)
        assert len(park.find_controllers(1783)) == 2

    def test_a_none_assertion_is_refused_when_a_controller_is_live(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        record = _record(tmp_path, phase="running", status="running")
        home = tmp_path / "home"
        home.mkdir()
        save_factory_record(home, record)
        plane = Path(record.sandbox) / ".factory" / record.run_id
        _plane(plane)
        monkeypatch.setattr(
            park, "factory_status",
            lambda *a, **k: _status("running", sandbox_path=record.sandbox),
        )
        monkeypatch.setattr(park, "find_controllers", lambda issue_id: [4242])

        with pytest.raises(park.ParkError, match="still look like this run's controller"):
            park.main([
                "--run-id", record.run_id, "--sandbox", record.sandbox,
                "--home", str(home), "--launcher", record.launcher,
                "--reason", "budget", "--controller-pid", "none", "--dry-run",
            ])

    def test_the_controller_is_stopped_before_anything_it_owns(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The ordering *is* the fix, so it is asserted as an order, not a fact.

        A controller still running when its driver dies executes its failure
        path, writes ``failed`` to the retained record and races this script's
        reconcile. Stopping compute first is therefore not a slower version of
        the same park; it is the bug.
        """
        record = _record(tmp_path, phase="running", status="running")
        home = tmp_path / "home"
        home.mkdir()
        save_factory_record(home, record)
        _plane(Path(record.sandbox) / ".factory" / record.run_id)

        order: list[str] = []
        sandbox_path = record.sandbox
        statuses = iter([
            _status("running", sandbox_path=sandbox_path),
            _status(sandbox_path=sandbox_path),
            _status(sandbox_path=sandbox_path),
        ])
        monkeypatch.setattr(park, "factory_status", lambda *a, **k: next(statuses))
        monkeypatch.setattr(park, "verify_controller", lambda pid, issue: "mimir worklink run-epic")
        monkeypatch.setattr(park, "factory_process_is_alive", lambda rec: True)
        monkeypatch.setattr(park, "factory_process_is_verified_dead", lambda rec: True)
        monkeypatch.setattr(park, "publish_snapshot", lambda *a, **k: tmp_path / "snap")
        monkeypatch.setattr(
            park, "stop_pid", lambda pid, label, **k: order.append(label),
        )
        monkeypatch.setattr(
            park, "stop_residual_compute", lambda run_id, **k: order.append("residual") or [],
        )
        monkeypatch.setattr(
            park, "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "{}", ""),
        )

        park.main([
            "--run-id", record.run_id, "--sandbox", record.sandbox,
            "--home", str(home), "--launcher", record.launcher,
            "--reason", "budget", "--controller-pid", "4242",
        ])

        assert order == ["controller", "recorded driver", "residual"]

    def test_a_park_refuses_when_the_recorded_process_cannot_be_proven_dead(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Publishing a park resume will reject is worse than refusing to park.

        ``factory_process_is_verified_dead`` is the predicate mimir's recovery
        path applies, so a park that cannot satisfy it produces an unresumable
        run and must stop before it terminalizes.
        """
        record = _record(tmp_path, phase="running", status="running")
        home = tmp_path / "home"
        home.mkdir()
        save_factory_record(home, record)
        _plane(Path(record.sandbox) / ".factory" / record.run_id)
        terminalized: list[list[str]] = []

        monkeypatch.setattr(
            park, "factory_status",
            lambda *a, **k: _status("running", sandbox_path=record.sandbox),
        )
        monkeypatch.setattr(park, "verify_controller", lambda pid, issue: "mimir worklink run-epic")
        monkeypatch.setattr(park, "stop_pid", lambda pid, label, **k: None)
        monkeypatch.setattr(park, "stop_residual_compute", lambda run_id, **k: [])
        monkeypatch.setattr(park, "factory_process_is_alive", lambda rec: False)
        monkeypatch.setattr(park, "factory_process_is_verified_dead", lambda rec: False)

        def record_run(cmd, **kwargs):
            terminalized.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        monkeypatch.setattr(park, "_run", record_run)

        with pytest.raises(park.ParkError, match="cannot verify the recorded factory process"):
            park.main([
                "--run-id", record.run_id, "--sandbox", record.sandbox,
                "--home", str(home), "--launcher", record.launcher,
                "--reason", "budget", "--controller-pid", "4242",
            ])
        assert not any("terminal" in cmd for cmd in terminalized)

    def test_stop_pid_verifies_death_rather_than_assuming_the_signal_worked(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(park, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(park.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(park.time, "sleep", lambda seconds: None)
        with pytest.raises(park.ParkError, match="survived SIGKILL"):
            park.stop_pid(4242, "controller", timeout=0.01)
