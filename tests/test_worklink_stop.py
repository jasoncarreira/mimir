from __future__ import annotations

import ctypes
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
import time
import uuid
from unittest.mock import AsyncMock, Mock, call

import pytest

from mimir.worklink import worker_client, worker_exec
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.control import stop_worklink
from mimir.worklink.factory_state import (
    FactoryRunRecord,
    factory_process_is_alive,
    load_factory_record,
    save_factory_record,
)
from mimir.worklink.run_state import (
    WorklinkRunState,
    load_run_state,
    process_is_alive,
    process_start_ticks,
    save_run_state,
)


@pytest.mark.asyncio
async def test_operator_stop_records_terminal_evidence_without_incident(tmp_path, monkeypatch):
    from mimir.worklink import orchestrator
    from mimir.worklink.checkout import CheckoutLease
    from mimir.worklink.claims import ChainlinkClaims, ClaimRecord, OPERATOR_STOP_PREFIX
    from mimir.worklink.compute import ComputeResult
    from mimir.worklink.dispatch_failures import (
        autonomous_dispatch_block_reason, dispatch_failure_state_dir, load_failure_state,
    )

    issue_id = 700
    attempt = 3
    state = WorklinkRunState(
        issue_id=issue_id, attempt=attempt, backend="opencode", compute_name="local_subprocess",
        handle_substrate="local_subprocess", handle_identifier="1234", process_start_ticks=42,
        branch="issue/700-a3", base_ref="main", local_base="main", repo=str(tmp_path),
        repo_url="", test_command=None, started_at=datetime.now(UTC).isoformat(),
    )
    save_run_state(tmp_path, state)
    labels = {"worklink:ready", "worklink:in-progress"}
    comments = []
    locks = {issue_id}
    claim = ClaimRecord(issue_id, attempt, "agent", datetime.now(UTC))
    comments.append(claim.to_comment())

    def runner(args):
        action = list(args[1:])
        if action[:2] == ["issue", "comment"]:
            comments.append(action[-1])
        elif action[:2] == ["issue", "unlabel"]:
            labels.discard(action[-1])
        elif action[:2] == ["issue", "label"]:
            labels.add(action[-1])
        elif action[:2] == ["locks", "release"]:
            locks.discard(issue_id)
        else:
            raise AssertionError(action)
        return subprocess.CompletedProcess(args, 0, "", "")

    from mimir.worklink import control
    monkeypatch.setattr(control, "process_is_alive", lambda state: True)
    monkeypatch.setattr(control, "process_identity_verified", lambda state: True)

    async def cancel(self, handle):
        assert control.operator_stop_requested(tmp_path, issue_id, attempt)

    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    result = await asyncio.to_thread(stop_worklink, tmp_path, issue_id, runner=runner)
    assert result.stopped

    claims = ChainlinkClaims(agent_id="agent", home_path=tmp_path, runner=runner, max_attempts=5)
    assert orchestrator._record_run_failure(
        home=tmp_path, issue_id=issue_id, attempt=attempt,
        error="worker SIGTERM", exit_status=-signal.SIGTERM, autonomous=True,
    ) is None
    lease = CheckoutLease(issue_id, attempt, tmp_path, tmp_path, state.branch, "main")
    terminal = orchestrator._TerminalClaimRelease(claims, home=tmp_path, issue_id=issue_id, attempt=attempt)
    backend = SimpleNamespace(name="opencode", interpret=Mock(side_effect=AssertionError("stop must bypass backend")))
    outcome = await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)._finalize(
        issue=orchestrator.IssueContext(issue_id, "title", "body", set()),
        claims=claims, claim_record=claim, attempt=attempt, config=None,
        backend=backend, compute=None, compute_result=ComputeResult(-signal.SIGTERM, "", ""),
        order=None, lease=lease, spec=None, started=datetime.now(UTC), test_cmd=None,
        root_dirty_before=(), runner=runner, terminal_release=terminal, autonomous=True,
    )
    assert outcome.status == "stopped" and outcome.reason == "stopped by operator"
    assert json.loads(outcome.evidence_path.read_text())["status"] == "stopped"
    assert len([comment for comment in comments if comment.startswith(OPERATOR_STOP_PREFIX)]) == 1
    assert "WORKLINK_EVIDENCE issue=700 attempt=3 status=stopped" in comments[-1]
    assert claims.attempts_used(comments) == 0
    assert claims.next_attempt(comments) == attempt + 1
    assert not labels and not locks
    assert autonomous_dispatch_block_reason(dispatch_failure_state_dir(tmp_path), issue_id) is None
    assert load_failure_state(dispatch_failure_state_dir(tmp_path))["issues"] == {}

    # The marker is inert for the next claim, even though it remains durable.
    orchestrator._record_run_failure(
        home=tmp_path, issue_id=issue_id, attempt=attempt + 1,
        error="external SIGTERM", exit_status=-signal.SIGTERM, autonomous=True,
    )
    assert load_failure_state(dispatch_failure_state_dir(tmp_path))["issues"][str(issue_id)]["failure_kind"] == "operator_required"
    assert autonomous_dispatch_block_reason(dispatch_failure_state_dir(tmp_path), issue_id) is not None
    claims.transition_issue(issue_id, status="failed", review_ready=False, attempt=attempt + 1)
    assert labels == {"worklink:ready"}


@pytest.mark.asyncio
async def test_external_sigterm_still_fails_and_rearms(tmp_path, monkeypatch):
    from mimir.worklink import orchestrator
    from mimir.worklink.backends.base import RawResult
    from mimir.worklink.checkout import CheckoutLease
    from mimir.worklink.claims import ChainlinkClaims, ClaimRecord
    from mimir.worklink.compute import ComputeResult
    from mimir.worklink.dispatch_failures import dispatch_failure_state_dir, load_failure_state
    from mimir.worklink.evidence import EvidenceValidation, WorklinkEvidence

    labels = {"worklink:in-progress"}
    comments = []

    def runner(args):
        action = list(args[1:])
        if action[:2] == ["issue", "unlabel"]:
            labels.discard(action[-1])
        elif action[:2] == ["issue", "label"]:
            labels.add(action[-1])
        elif action[:2] == ["issue", "comment"]:
            comments.append(action[-1])
        elif action[:2] != ["locks", "release"]:
            raise AssertionError(action)
        return subprocess.CompletedProcess(args, 0, "", "")

    async def interpret(order, result):
        return RawResult(-signal.SIGTERM, None, "failed", "worker terminated")

    async def observe_evidence(**kwargs):
        evidence = WorklinkEvidence(
            issue=700, attempt=1, backend="opencode", branch="issue/700-a1",
            checkout=str(tmp_path), started_at=datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(), files_changed=[], diff_stat="",
            commands=[], tests=None, pr_url=None, status="failed",
            failure_reason="worker terminated",
        )
        return EvidenceValidation("failed", False, ("worker terminated",), evidence)

    monkeypatch.setattr(orchestrator, "observe_evidence", observe_evidence)
    monkeypatch.setattr(orchestrator, "_with_outside_checkout_detection", lambda validation, **kwargs: validation)
    monkeypatch.setattr(orchestrator, "_cleanup_checkout_after_transition", lambda *args, **kwargs: None)
    claims = ChainlinkClaims(agent_id="agent", home_path=tmp_path, runner=runner)
    terminal = orchestrator._TerminalClaimRelease(claims, tmp_path, 700, 1)
    result = await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)._finalize(
        issue=orchestrator.IssueContext(700, "title", "body", set()),
        claims=claims, claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
        attempt=1, config=SimpleNamespace(defaults=SimpleNamespace(gate_rerun_max_failures=0)),
        backend=SimpleNamespace(name="opencode", interpret=interpret), compute=None,
        compute_result=ComputeResult(-signal.SIGTERM, "", ""), order=None,
        lease=CheckoutLease(700, 1, tmp_path, tmp_path, "issue/700-a1", "main"),
        spec=SimpleNamespace(backend_config={}), started=datetime.now(UTC), test_cmd=None,
        root_dirty_before=(), runner=runner, terminal_release=terminal, autonomous=True,
    )
    assert result.status == "failed"
    assert json.loads(result.evidence_path.read_text())["status"] == "failed"
    assert any("WORKLINK_EVIDENCE issue=700 attempt=1 status=failed" in comment for comment in comments)
    assert labels == {"worklink:ready"}
    assert load_failure_state(dispatch_failure_state_dir(tmp_path))["issues"]["700"]["failure_kind"] == "operator_required"


def _factory(home: Path, run_id: str = "chainlink-700") -> FactoryRunRecord:
    record = FactoryRunRecord(
        run_id=run_id, issue_id=700, attempt=2, repository="owner/repo",
        base_ref="main", branch="epic/700", launcher="/opt/factory.js",
        sandbox=str(home / run_id), session="session-1",
        handle=LaunchHandle("local_subprocess", run_id, 99, 4321),
        status=None, observed_at=None, controller_phase="running",
    )
    save_factory_record(home, record)
    return record


def test_operator_stop_marker_requires_matching_issue_and_attempt(tmp_path):
    from mimir.worklink.control import _mark_operator_stop, _operator_stop_path, operator_stop_requested

    _mark_operator_stop(tmp_path, 700, 2)
    assert operator_stop_requested(tmp_path, 700, 2)
    assert not operator_stop_requested(tmp_path, 700, 3)
    assert not operator_stop_requested(tmp_path, 701, 2)
    # A file at the requested issue path with somebody else's issue id is inert.
    _operator_stop_path(tmp_path, 700).write_text('{"issue_id": 701, "attempt": 2}')
    assert not operator_stop_requested(tmp_path, 700, 2)


@pytest.fixture
def claim_runner():
    locks = {700}
    labels = {"worklink:epic", "worklink:in-progress"}

    def run(args):
        if args == ["chainlink", "locks", "release", "700"]:
            locks.remove(700)
        else:
            assert args == ["chainlink", "issue", "unlabel", "700", "worklink:in-progress"]
            labels.remove("worklink:in-progress")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return Mock(side_effect=run), locks, labels


@pytest.mark.parametrize(
    "ticks,observed,zombie,kill_error,dead",
    [
        pytest.param(99, 99, True, None, True, id="zombie"),
        pytest.param(99, 100, False, None, True, id="mismatch"),
        pytest.param(None, 99, False, None, False, id="null-ticks-live"),
        pytest.param(None, None, False, ProcessLookupError(errno.ESRCH, "gone"), True,
                     id="null-ticks-dead-esrch"),
        pytest.param(99, None, False, None, False, id="unreadable-observed-ticks"),
        pytest.param(99, None, False, ProcessLookupError(errno.ESRCH, "gone"), True,
                     id="dead"),
        pytest.param(99, 99, False, PermissionError(errno.EPERM, "denied"), False,
                     id="permission-error"),
    ],
)
def test_stop_rejected_factory_identity(
    tmp_path, monkeypatch, claim_runner, ticks, observed, zombie, kill_error, dead,
):
    import mimir.worklink.control as control
    import mimir.worklink.factory_state as factory_state

    record = _factory(tmp_path)
    record = replace(record, handle=replace(record.handle, process_start_ticks=ticks))
    save_factory_record(tmp_path, record)
    save_run_state(tmp_path, WorklinkRunState(
        issue_id=700, attempt=1, backend="feature_factory", compute_name="local_subprocess",
        handle_substrate="local_subprocess", handle_identifier="1234", process_start_ticks=1,
        branch="epic/700", base_ref="main", local_base="main", repo=str(tmp_path),
        repo_url="", test_command=None, started_at="2026-09-11T00:00:00+00:00",
    ))
    monkeypatch.setattr(control, "process_is_alive", lambda state: False)
    kill = Mock(side_effect=kill_error)
    monkeypatch.setattr(factory_state.os, "kill", kill)
    monkeypatch.setattr(factory_state, "process_is_zombie", lambda pid: zombie)
    monkeypatch.setattr(factory_state, "process_start_ticks", lambda pid: observed)
    cancel = AsyncMock()
    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    runner, locks, labels = claim_runner

    result = stop_worklink(tmp_path, 700, runner=runner)

    cancel.assert_not_called()
    assert kill.call_args_list == [call(4321, 0), call(4321, 0)]
    assert not result.stopped
    assert result.reason and result.reason != "no live run"
    assert f"factory {record.run_id} handle={record.handle}" in result.reason
    assert "refusing to signal it" in result.reason
    assert result.state_cleared and load_run_state(tmp_path, 700) is None
    assert result.claim_released is dead and result.label_cleared is dead
    saved = load_factory_record(tmp_path, record.run_id)
    if dead:
        assert "recorded process has exited; cleaning stale state" in result.reason
        assert saved == replace(record, controller_phase="stopped", controller_error=(
            result.reason.split("; recorded process has exited")[0]
        ))
        assert locks == set() and labels == {"worklink:epic"}
    else:
        assert "exit unverified; state and claim retained" in result.reason
        assert saved == record
        runner.assert_not_called()
        assert locks == {700} and labels == {"worklink:epic", "worklink:in-progress"}


@pytest.mark.parametrize("dead", [False, True], ids=["still-alive", "exited"])
@pytest.mark.parametrize("error", [KeyError("missing job"), RuntimeError("offline"), OSError("failed")])
def test_stop_factory_cancel_failure(tmp_path, monkeypatch, claim_runner, dead, error):
    import mimir.worklink.control as control
    import mimir.worklink.factory_state as factory_state

    record = _factory(tmp_path)
    kill = Mock(side_effect=[None, ProcessLookupError(errno.ESRCH, "gone") if dead else None])
    monkeypatch.setattr(factory_state.os, "kill", kill)
    monkeypatch.setattr(factory_state, "process_is_zombie", lambda pid: False)
    monkeypatch.setattr(factory_state, "process_start_ticks", lambda pid: 99)
    cancel = AsyncMock(side_effect=error)
    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    runner, locks, labels = claim_runner

    result = stop_worklink(tmp_path, 700, runner=runner)

    cancel.assert_awaited_once_with(record.handle)
    assert kill.call_args_list == [call(4321, 0), call(4321, 0)]
    assert not result.stopped
    problem = f"factory {record.run_id}: cancellation failed: {error}"
    assert problem in result.reason and result.reason != "no live run"
    assert result.claim_released is dead and result.label_cleared is dead
    if dead:
        assert "cleaning stale state" in result.reason
        assert load_factory_record(tmp_path, record.run_id) == replace(
            record, controller_phase="stopped", controller_error=problem,
        )
        assert locks == set() and labels == {"worklink:epic"}
    else:
        assert "exit unverified; state and claim retained" in result.reason
        assert load_factory_record(tmp_path, record.run_id) == record
        runner.assert_not_called()
        assert locks == {700} and labels == {"worklink:epic", "worklink:in-progress"}


@pytest.mark.parametrize("live_id", ["chainlink-700", "700"])
@pytest.mark.parametrize("other", ["dead", "uncertain", "live"])
def test_stop_checks_both_factory_records(tmp_path, monkeypatch, claim_runner, live_id, other):
    import mimir.worklink.control as control
    import mimir.worklink.factory_state as factory_state

    live = _factory(tmp_path, live_id)
    other_id = "700" if live_id == "chainlink-700" else "chainlink-700"
    second = replace(_factory(tmp_path, other_id), handle=LaunchHandle(
        "local_subprocess", other_id, None if other == "uncertain" else 99, 4322,
    ))
    save_factory_record(tmp_path, second)
    monkeypatch.setattr(factory_state.os, "kill", Mock())
    monkeypatch.setattr(factory_state, "process_is_zombie", lambda pid: False)
    monkeypatch.setattr(factory_state, "process_start_ticks",
                        lambda pid: 100 if pid == 4322 and other == "dead" else 99)
    cancel = AsyncMock()
    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    runner, locks, labels = claim_runner

    result = stop_worklink(tmp_path, 700, runner=runner)

    expected = [call(live.handle)] + ([call(second.handle)] if other == "live" else [])
    cancel.assert_has_awaits(expected, any_order=True)
    assert cancel.await_count == len(expected)
    assert load_factory_record(tmp_path, live_id) == replace(live, controller_phase="stopped")
    saved = load_factory_record(tmp_path, other_id)
    assert saved.controller_phase == ("running" if other == "uncertain" else "stopped")
    assert result.stopped is (other == "live")
    assert result.claim_released is (other != "uncertain")
    assert result.label_cleared is (other != "uncertain")
    if other == "uncertain":
        assert saved == second
        assert "exit unverified; state and claim retained" in result.reason
        runner.assert_not_called()
        assert locks == {700} and labels == {"worklink:epic", "worklink:in-progress"}
    else:
        assert locks == set() and labels == {"worklink:epic"}
        if other == "dead":
            assert f"factory {other_id}" in result.reason
            assert "cleaning stale state" in result.reason
        else:
            assert result.reason is None


# Like the supervisor fixtures, register every generation and acknowledge
# readiness only after the escaped grandchild has installed its TERM handler.
PAYLOAD = r'''
import os, signal, sys, time
from pathlib import Path
registry = Path(sys.argv[1])
def record():
    fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, (str(os.getpid()) + '\n').encode())
    os.close(fd)
record()
reader, writer = os.pipe()
middle = os.fork()
if middle == 0:
    record()
    os.setsid()
    if os.fork() == 0:
        record()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(writer, b'R')
    while True:
        signal.pause()
os.close(writer)
assert os.read(reader, 1) == b'R'
registry.with_suffix('.ready').touch()
while True:
    signal.pause()
'''


def _exercise_stop(home: Path, *, stale_leaf: bool = True) -> None:
    # This fresh interpreter owns all children, including failure-path orphans;
    # never change pytest's subreaper state or wait on its unrelated children.
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    registry = home / 'pids'
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    rpc_client, rpc_server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    supervisor = Path(worker_exec.__file__).with_name('factory_supervisor.py')
    events: list[dict[str, object]] = []
    identifier = str(uuid.uuid4())
    pool = ThreadPoolExecutor(max_workers=2)
    with (home / 'stdout').open('w+b') as stdout, (home / 'stderr').open('w+b') as stderr:
        process = subprocess.Popen(
            [sys.executable, '-I', str(supervisor), str(child.fileno()),
             sys.executable, '-I', '-c', PAYLOAD, str(registry)],
            pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr,
        )
        child.close()
        proc = worker_exec._FactoryProcess(process, parent, events.append)
        worker_exec._jobs[identifier] = proc
        monitor = pool.submit(
            worker_exec._wait_factory, proc, 180,
            stdout.fileno(), 65536, stderr.fileno(), 65536,
        )
        rpc = pool.submit(worker_exec.handle_connection, rpc_server)
        try:
            while not registry.with_suffix('.ready').exists():
                assert process.poll() is None, stderr.name
                time.sleep(.01)
            pids = [int(line) for line in registry.read_text().splitlines()]
            assert len(pids) == len(set(pids)) == 3
            assert all(Path(f'/proc/{pid}').exists() for pid in pids)
            assert os.getpgid(pids[-1]) != os.getpgid(pids[0])
            ticks = process_start_ticks(process.pid)
            assert ticks is not None
            handle = LaunchHandle('local_subprocess', identifier, ticks, process.pid)
            sandbox = home / 'chainlink-700'
            sandbox.mkdir()
            record = FactoryRunRecord(
                run_id='chainlink-700', issue_id=700, attempt=2,
                repository='owner/repo', base_ref='main', branch='epic/700',
                launcher=str(supervisor), sandbox=str(sandbox), session='session-1',
                handle=handle, status=None, observed_at=None, controller_phase='running',
            )
            save_factory_record(home, record)
            assert load_factory_record(home, record.run_id) == record
            assert factory_process_is_alive(record)
            if stale_leaf:
                # A reaped fixture PID, not an assumed-unused machine-wide PID.
                dead = subprocess.Popen([sys.executable, '-I', '-c', 'pass'])
                dead.wait()
                state = WorklinkRunState(
                    issue_id=700, attempt=1, backend='feature_factory',
                    compute_name='local_subprocess', handle_substrate='local_subprocess',
                    handle_identifier=str(dead.pid), process_start_ticks=0,
                    branch='epic/700', base_ref='main', local_base='main',
                    repo=str(home), repo_url='https://example.invalid/owner/repo',
                    test_command=None, started_at='2026-09-11T00:00:00+00:00',
                )
                save_run_state(home, state)
                assert not process_is_alive(state)

            commands: list[list[str]] = []
            labels = {'worklink:epic', 'worklink:in-progress'}

            def runner(args):
                commands.append(list(args))
                if list(args) == ['chainlink', 'issue', 'unlabel', '700', 'worklink:in-progress']:
                    labels.remove('worklink:in-progress')
                else:
                    assert list(args) == ['chainlink', 'locks', 'release', '700']
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

            # Substitute only transport discovery/authentication. Cancellation
            # serialization, executor dispatch, monitor and supervisor are real.
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(worker_client.WorkerClient, '_connect', lambda self, timeout_s=None: rpc_client)
                result = stop_worklink(home, 700, runner=runner)
            stopped = load_factory_record(home, record.run_id)
            remaining = [pid for pid in [process.pid, *pids] if Path(f'/proc/{pid}').exists()]
            evidence = {
                'result': asdict(result), 'remaining_pids': remaining,
                'controller_phase': stopped.controller_phase, 'commands': commands,
            }
            assert result.stopped, evidence
            assert result.claim_released and result.label_cleared, evidence
            assert stopped.controller_phase == 'stopped', evidence
            assert stopped.handle == handle
            assert labels == {'worklink:epic'}
            assert commands == [
                ['chainlink', 'locks', 'release', '700'],
                ['chainlink', 'issue', 'unlabel', '700', 'worklink:in-progress'],
            ]
            assert remaining == [], evidence  # Zombies count as unreaped too.
            assert monitor.result() == (-signal.SIGTERM, False, False)
            rpc.result()
            assert proc.done.is_set() and proc.error is None
            assert process.returncode == 0
            print(json.dumps(evidence))
        finally:
            # Assertions above precede fixture cleanup, so cleanup cannot turn a
            # broken stop into a passing whole-tree reaping assertion.
            rpc_client.close()
            proc.request_stop()
            try:
                monitor.result(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                deadline = time.monotonic() + 5
                while True:
                    children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
                    for pid in map(int, children):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, os.WNOHANG)
                    try:
                        os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    except ChildProcessError:
                        break
                    assert time.monotonic() < deadline, 'fixture descendants were not reaped'
                    time.sleep(.01)
                parent.close()
                pool.shutdown(wait=True)
                worker_exec._jobs.pop(identifier)


@pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux subreaper and procfs')
def test_stop_live_factory_reaps_tree_despite_stale_leaf(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, '-c',
         'import runpy, sys; from pathlib import Path; '
         'runpy.run_path(sys.argv[1])["_exercise_stop"](Path(sys.argv[2]))',
         str(Path(__file__).resolve()), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=240,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
