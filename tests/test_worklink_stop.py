from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

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
    process_is_alive,
    process_start_ticks,
    save_run_state,
)


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
