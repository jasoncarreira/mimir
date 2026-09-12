from __future__ import annotations

import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux only: darwin has no PR_SET_CHILD_SUBREAPER"
)
SOURCE = Path(__file__).resolve().parents[1] / "mimir/worklink/factory_supervisor.py"
spec = importlib.util.spec_from_file_location("factory_supervisor_under_test", SOURCE)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


# The outer process owns all fixture descendants, even with prctl mutated out
# in the supervisor. No subreaper state or process-wide waits touch pytest.
HARNESS = r'''
import ctypes, importlib.util, json, os, signal, socket, subprocess, sys, time
source, registry, mode, mutate, term_ready_pids, payload = sys.argv[1:]
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
if mode == "backpressure":
    child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
command = [sys.executable, "-I", source, str(child.fileno()), sys.executable, "-I", "-c", payload, registry]
if mutate == "yes":
    wrapper = "import importlib.util,sys; s=importlib.util.spec_from_file_location('s',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m._enable_subreaper=lambda:None; sys.exit(m.main(sys.argv[2:]))"
    command = [sys.executable, "-I", "-c", wrapper] + command[2:]
if mode == "live_reap":
    wrapper = """import importlib.util, sys
s = importlib.util.spec_from_file_location('s', sys.argv[1])
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
observe = m._observe
pending = set()
def observed(channel, primary, adoptions):
    # _observe only enumerates; the reap runs after it, over the pids it
    # returned. An adoptee can be re-parented while still ALIVE, and then that
    # iteration's waitpid(WNOHANG) reaps nothing. Signal on a pid we saw having
    # LEFT the child set, which is what reaping it means, rather than on merely
    # having seen it.
    children = observe(channel, primary, adoptions)
    live = {pid for pid in children if pid != primary}
    if pending and not (pending & live):
        open(sys.argv[-1] + '.checked', 'w').close()
    pending.update(live)
    return children
m._observe = observed
sys.exit(m.main(sys.argv[2:]))
"""
    command = [sys.executable, '-I', '-c', wrapper] + command[2:]
if mode in ("SIGTERM", "SIGINT"):
    wrapper = """import importlib.util, os, signal, sys
s = importlib.util.spec_from_file_location('s', sys.argv[1])
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
waitid = m.os.waitid
def checked_waitid(*args):
    try:
        return waitid(*args)
    except ChildProcessError:
        assert args == (os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        open(sys.argv[-1] + '.reaped', 'w').close()
        raise
m.os.waitid = checked_waitid
sys.exit(m.main(sys.argv[2:]))
"""
    if mutate == "signals":
        wrapper = wrapper.replace("sys.exit(m.main", "m.signal.signal = lambda sig, handler: signal.SIG_DFL\nsys.exit(m.main")
    command = [sys.executable, '-I', '-c', wrapper] + command[2:]
process = subprocess.Popen(command, pass_fds=(child.fileno(),))
child.close()
events = []
try:
    ready = json.loads(parent.recv(4096))
    assert ready == {"kind": "ready"}, ready
    if mode in ("stop", "eof", "SIGTERM", "SIGINT"):
        while not os.path.exists(registry + ".ready"):
            time.sleep(.01)
        if int(term_ready_pids):
            # Establish the adversarial population before cancellation without
            # changing the supervisor's TERM/KILL/reap path or its deadlines.
            # A busy runner need not schedule three forks within TERM_GRACE.
            with open(registry) as stream:
                primary = int(stream.readline())
            os.killpg(primary, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while True:
                with open(registry) as stream:
                    if len(stream.readlines()) >= int(term_ready_pids):
                        break
                assert time.monotonic() < deadline, 'fixture TERM handlers did not record descendants'
                time.sleep(.01)
        if mode in ("SIGTERM", "SIGINT"):
            process.send_signal(getattr(signal, mode))
        elif mode == "eof":
            parent.close()
        else:
            parent.send(b"stop, not necessarily JSON")
    if mode == "backpressure":
        # Do not drain even one event until cleanup and reporting have ended.
        process.wait()
    if mode != "eof":
        while True:
            packet = parent.recv(4096)
            if not packet and mode in ("backpressure", "SIGTERM", "SIGINT"):
                break
            assert packet, events
            event = json.loads(packet)
            events.append(event)
            if event["kind"] == "terminal":
                break
    result = process.wait()
    with open(registry) as stream:
        pids = [int(line) for line in stream]
    leaked = [pid for pid in pids if os.path.exists('/proc/' + str(pid))]
    live = []
    for pid in leaked:
        with open('/proc/%s/stat' % pid) as stream:
            if stream.read().split(')')[1].split()[0] != 'Z':
                live.append(pid)
    print(json.dumps(dict(events=events, result=result, pids=pids, leaked=leaked,
                         live=live,
                         reaped=os.path.exists(registry + '.reaped'),
                         supervisor_gone=not os.path.exists('/proc/' + str(process.pid)))))
finally:
    parent.close()
    # Killing the fixture supervisor reparents everything to this harness.
    if process.poll() is None:
        process.kill()
        process.wait()
    deadline = time.monotonic() + 5
    while True:
        with open('/proc/self/task/%s/children' % os.getpid()) as stream:
            children = [int(pid) for pid in stream.read().split()]
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, os.WNOHANG)
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            break
        assert time.monotonic() < deadline, "fixture could not reap leaked descendants"
        time.sleep(.01)
'''

PRELUDE = r'''
import os, signal, sys, time
registry = sys.argv[1]
def record():
    fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, (str(os.getpid()) + '\n').encode())
    os.close(fd)
record()
'''

ESCAPED = PRELUDE + r'''
read_fd, write_fd = os.pipe()
middle = os.fork()
if middle == 0:
    record()
    os.setsid()
    intermediate = os.getpid()
    if os.fork() == 0:
        record()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while os.getppid() == intermediate:
            time.sleep(.001)
        os.write(write_fd, b'R')
        while True:
            time.sleep(1)
    os._exit(0)
os.close(write_fd)
assert os.read(read_fd, 1) == b'R'
os.waitpid(middle, 0)
open(registry + '.ready', 'w').close()
'''

RESPAWN = PRELUDE + r'''
def spawn_on_term(sig, frame):
    if os.fork() == 0:
        record()
        while True:
            time.sleep(1)
signal.signal(signal.SIGTERM, spawn_on_term)
read_fd, write_fd = os.pipe()
if os.fork() == 0:
    record()
    if os.fork() == 0:
        record()
        os.write(write_fd, b'R')
    else:
        os.write(write_fd, b'R')
    while True:
        time.sleep(1)
assert os.read(read_fd, 1) == b'R'
assert os.read(read_fd, 1) == b'R'
open(registry + '.ready', 'w').close()
while True:
    time.sleep(1)
'''


def run_isolated(tmp_path, payload, *, mode="normal", mutate=False, term_ready_pids=0):
    completed = subprocess.run(
        [sys.executable, "-I", "-c", HARNESS, str(SOURCE), str(tmp_path / "pids"),
         mode, mutate if isinstance(mutate, str) else "yes" if mutate else "no",
         str(term_ready_pids), payload],
        # One ceiling covers harness startup, descendants, reporting and reaping.
        capture_output=True, text=True, timeout=240,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def assert_clean(result):
    assert result["supervisor_gone"]
    assert result["leaked"] == [], result


def test_escaped_double_fork_is_adopted_and_reaped(tmp_path):
    result = run_isolated(tmp_path, ESCAPED)
    assert_clean(result)
    assert result["result"] == 0
    assert result["events"][-1] == {"kind": "terminal", "exit_code": 0}
    assert any(e.get("event") == "worklink_factory_orphan_adopted"
               and e["pid"] == result["pids"][-1] for e in result["events"])


def test_disabled_prctl_mutation_is_detected_and_fixture_reaps_leak(tmp_path):
    result = run_isolated(tmp_path, ESCAPED, mutate=True)
    assert result["leaked"], "mutation must leave the escaped descendant unreaped"
    with pytest.raises(AssertionError):
        assert_clean(result)


@pytest.mark.parametrize("mode", ["stop", "eof"])
def test_cancellation_reaps_multigeneration_respawning_group(tmp_path, mode):
    result = run_isolated(tmp_path, RESPAWN, mode=mode, term_ready_pids=6)
    assert_clean(result)
    assert len(result["pids"]) >= 6  # TERM handlers really forked another generation.
    if mode == "stop":
        assert result["result"] == 0
        assert result["events"][-1] == {"kind": "terminal", "exit_code": -signal.SIGKILL}


def test_cancellation_reaps_escaped_descendant(tmp_path):
    result = run_isolated(tmp_path, ESCAPED + "\nwhile True: time.sleep(1)\n", mode="stop")
    assert_clean(result)
    assert result["result"] == 0
    assert result["events"][-1]["exit_code"] == -signal.SIGTERM


@pytest.mark.parametrize("mode", ["SIGTERM", "SIGINT"])
@pytest.mark.parametrize("payload", [ESCAPED + "\nwhile True: time.sleep(1)\n", RESPAWN],
                         ids=["escaped", "respawning"])
def test_signalled_supervisor_reaps_every_descendant(tmp_path, mode, payload):
    result = run_isolated(tmp_path, payload, mode=mode)
    assert_clean(result)
    assert result["reaped"], "supervisor must prove ECHILD, not just signal descendants"
    assert result["result"] == 0
    assert "exit_code" in result["events"][-1]
    assert any(e.get("event") == "worklink_factory_orphan_adopted" for e in result["events"])


def test_disabled_signal_handlers_leave_surviving_descendant(tmp_path):
    result = run_isolated(tmp_path, ESCAPED + "\nwhile True: time.sleep(1)\n",
                          mode="SIGTERM", mutate="signals")
    assert result["live"], "negative control must observe live surviving descendants"
    assert not result["reaped"]
    with pytest.raises(AssertionError):
        assert_clean(result)


def test_failure_exit_is_reported_not_supervisor_exit(tmp_path):
    result = run_isolated(tmp_path, PRELUDE + "\nsys.exit(37)\n")
    assert_clean(result)
    assert result["result"] == 0
    assert result["events"][-1] == {"kind": "terminal", "exit_code": 37}


def test_zombie_adoptee_is_reported(tmp_path):
    payload = PRELUDE + r'''
read_fd, write_fd = os.pipe()
middle = os.fork()
if middle == 0:
    record()
    zombie = os.fork()
    if zombie == 0:
        record()
        os._exit(0)
    os.waitid(os.P_PID, zombie, os.WEXITED | os.WNOWAIT)
    os.write(write_fd, b'R')
    os._exit(0)
assert os.read(read_fd, 1) == b'R'
os.waitid(os.P_PID, middle, os.WEXITED | os.WNOWAIT)
'''
    result = run_isolated(tmp_path, payload)
    assert_clean(result)
    adopted = {e["pid"] for e in result["events"] if "pid" in e}
    assert set(result["pids"][1:]) <= adopted


def test_adopted_zombie_reaped_while_primary_still_running(tmp_path):
    payload = PRELUDE + r'''
read_fd, write_fd = os.pipe()
middle = os.fork()
if middle == 0:
    record()
    zombie = os.fork()
    if zombie == 0:
        record()
        os._exit(0)
    os.write(write_fd, str(zombie).encode())
    os._exit(0)
os.close(write_fd)
zombie = int(os.read(read_fd, 100))
os.waitpid(middle, 0)
while not os.path.exists(registry + '.checked'):
    time.sleep(.01)
assert not os.path.exists('/proc/' + str(zombie)), 'adopted zombie retained after reap iteration'
'''
    result = run_isolated(tmp_path, payload, mode="live_reap")
    assert_clean(result)
    assert result["result"] == 0
    assert result["events"][-1] == {"kind": "terminal", "exit_code": 0}
    assert any(e.get("pid") == result["pids"][-1] for e in result["events"])


def test_full_socket_fails_run_without_leaking_live_adoptees(tmp_path):
    payload = PRELUDE + r'''
middle = os.fork()
if middle == 0:
    record()
    os.setsid()
    read_fd, write_fd = os.pipe()
    for _ in range(40):
        if os.fork() == 0:
            record()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(write_fd, b'R')
            while True:
                time.sleep(1)
    for _ in range(40):
        assert os.read(read_fd, 1) == b'R'
    os._exit(0)
os.waitpid(middle, 0)
while True:
    time.sleep(1)
'''
    result = run_isolated(tmp_path, payload, mode="backpressure")
    assert_clean(result)
    assert result["result"] == 1
    assert not any(e["kind"] == "terminal" and "exit_code" in e for e in result["events"])
    assert len(result["pids"]) > 10


def test_event_loss_survives_reaping_and_socket_recovery(monkeypatch):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    calls = []
    snapshots = 0
    monkeypatch.setattr(supervisor, "TERM_GRACE", 0)
    monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=42))
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig, group=False: calls.append((pid, sig)))

    def children():
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            return [42]
        # Fill the actual socket after ready/active observation, so loss first
        # occurs inside teardown and must not short-circuit either child's kill.
        while supervisor._send(child, {"kind": "event", "padding": "x" * 1000}):
            pass
        return [42, 43]

    def waitpid(pid, flags):
        calls.append(("wait", pid))
        if pid == 43:
            while True:
                try:
                    parent.recv(4096)
                except BlockingIOError:
                    break
        return pid, 0

    def waitid(kind, *args):
        if kind == os.P_ALL:
            raise ChildProcessError()
        return object()

    monkeypatch.setattr(supervisor, "_children", children)
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(supervisor.os, "waitid", waitid)
    with parent, child:
        parent.setblocking(False)
        child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        assert supervisor.supervise(child, ["payload"]) == 1
        packets = [json.loads(parent.recv(4096)) for _ in range(2)]
    assert (42, signal.SIGKILL) in calls
    assert (43, signal.SIGKILL) in calls
    assert ("wait", 42) in calls and ("wait", 43) in calls
    assert packets[0]["event"] == "worklink_factory_reap_refused"
    assert packets[1]["kind"] == "terminal"
    assert "adoption event delivery failed: 1 event(s) lost" in packets[1]["error"]
    assert "exit_code" not in packets[1]


def test_failed_adoption_send_is_sticky_and_counted_once(monkeypatch):
    monkeypatch.setattr(supervisor, "_children", lambda: [42, 43])
    monkeypatch.setattr(supervisor, "_send", lambda *a, **kw: False)
    adoptions = supervisor._Adoptions()
    assert supervisor._observe(None, 42, adoptions) == [42, 43]
    assert supervisor._observe(None, 42, adoptions) == [42, 43]
    assert adoptions.lost == 1
    adoptions.seen.discard(43)  # Reaped PID later reused for a new adoption.
    supervisor._observe(None, 42, adoptions)
    assert adoptions.lost == 2


@pytest.mark.parametrize("group_id,expected", [(42, "group"), (17, "pid")])
def test_owned_signal_only_uses_pinned_group_leader(monkeypatch, group_id, expected):
    calls = []
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: group_id)
    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: calls.append(("group", pid, sig)))
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: calls.append(("pid", pid, sig)))
    supervisor._signal_owned(42)
    assert calls == [(expected, 42, signal.SIGKILL)]


def test_teardown_keeps_payload_anchor_and_never_reuses_reaped_pgid(monkeypatch):
    calls = []
    snapshots = iter([[42, 43], [43]])
    monkeypatch.setattr(supervisor, "TERM_GRACE", 0)
    monkeypatch.setattr(supervisor, "_observe", lambda *args: next(snapshots))
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: 42)
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig, group=False: calls.append((pid, sig, group)))
    def waitpid(pid, flags):
        calls.append(("wait", pid))
        return (pid, 0) if pid == 42 or len(calls) > 7 else (0, 0)
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    checks = iter([None, ChildProcessError()])
    def waitid(*args):
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(supervisor.os, "waitid", waitid)
    payload = SimpleNamespace(pid=42, returncode=None)
    assert supervisor._teardown(None, payload, supervisor._Adoptions()) == 0
    assert calls[:2] == [(42, signal.SIGTERM, True), (42, signal.SIGKILL, True)]
    first_wait = calls.index(("wait", 42))
    assert not any(c == (42, signal.SIGKILL, True) for c in calls[first_wait:])
    assert (43, signal.SIGKILL, False) in calls[first_wait:]


def test_permission_denied_survivor_hits_overall_bound(monkeypatch):
    clock = iter(i * .01 for i in range(100))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _: None)
    monkeypatch.setattr(supervisor, "TERM_GRACE", 0)
    monkeypatch.setattr(supervisor, "REAP_TIMEOUT", .1)
    monkeypatch.setattr(supervisor, "_observe", lambda *args: [42])
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: 42)
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(supervisor.os, "killpg", denied)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(supervisor.os, "waitid", lambda *args: None)
    with pytest.raises(supervisor.FactoryReapRefused, match="deadline"):
        supervisor._teardown(None, SimpleNamespace(pid=42), supervisor._Adoptions())


@pytest.mark.parametrize("signalled", [False, True])
def test_signals_during_spawn_and_teardown_do_not_extend_budget(monkeypatch, signalled):
    now = 0.0
    handlers = {}
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    packets = []

    def install(sig, handler):
        previous = handlers.get(sig, originals[sig])
        handlers[sig] = handler
        return previous

    def request_signals():
        if signalled:
            for sig, handler in handlers.items():
                handler(sig, None)

    def spawn(*args, **kwargs):
        request_signals()  # Before Popen has returned the ownership anchor.
        return SimpleNamespace(pid=42)

    def sleep(delay):
        nonlocal now
        request_signals()  # Repeated throughout TERM grace and the reap loop.
        now += delay

    monkeypatch.setattr(supervisor.signal, "signal", install)
    monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now)
    monkeypatch.setattr(supervisor.time, "sleep", sleep)
    monkeypatch.setattr(supervisor, "_observe", lambda *args: [42])
    monkeypatch.setattr(supervisor, "_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(supervisor.os, "waitid", lambda kind, *args: object() if kind == os.P_PID else None)
    monkeypatch.setattr(supervisor, "_send", lambda channel, packet, **kw: packets.append(packet) or True)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        assert supervisor.supervise(child, ["payload"]) == 1
    budget = supervisor.TERM_GRACE + supervisor.REAP_TIMEOUT
    assert budget <= now <= budget + supervisor.INTERVAL
    assert handlers == originals
    assert packets[-2]["event"] == "worklink_factory_reap_refused"
    assert "deadline" in packets[-1]["error"]


@pytest.mark.parametrize("failure", ["platform", "prctl", "spawn"])
def test_fail_closed_reports_error(monkeypatch, failure):
    spawned = []
    def spawn(*args, **kwargs):
        spawned.append(True)
        raise OSError("spawn failed")
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    if failure == "platform":
        monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    elif failure == "prctl":
        ctypes.set_errno(errno.EPERM)
        monkeypatch.setattr(supervisor.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(prctl=lambda *a: -1))
    else:
        monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: None)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        assert supervisor.supervise(child, ["payload"]) == 1
        parent.settimeout(1)
        packets = [json.loads(parent.recv(4096)) for _ in range(3 if failure == "spawn" else 2)]
    assert bool(spawned) == (failure == "spawn")
    assert packets[-2]["event"] == "worklink_factory_reap_refused"
    assert packets[-1]["kind"] == "terminal"
    assert "error" in packets[-1]


def test_sends_are_bounded_on_full_or_broken_socket():
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        while supervisor._send(child, {"kind": "event", "error": "x" * 1000}):
            pass
        assert not supervisor._send(child, {"kind": "terminal"}, report=True)
        parent.close()
        assert not supervisor._send(child, {"kind": "event"})


def test_fd_closed_in_payload_and_ready_precedes_spawn(monkeypatch):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        child.set_inheritable(True)
        monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: None)
        def spawn(argv, **kwargs):
            assert not child.get_inheritable()
            assert kwargs == {"start_new_session": True, "close_fds": True}
            assert json.loads(parent.recv(4096)) == {"kind": "ready"}
            raise OSError("tested spawn boundary")
        monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
        assert supervisor.supervise(child, ["payload"]) == 1
