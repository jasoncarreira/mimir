"""Per-run Linux subreaper, invoked by absolute filename after identity drop.

CLI: python -I factory_supervisor.py SOCKET_FD PAYLOAD [ARG ...]
The inherited AF_UNIX/SOCK_SEQPACKET socket carries JSON packets (<=4096 bytes).
No factory workflow or identity management belongs in this process.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

MAX_PACKET = 4096
TERM_GRACE = 0.2
REAP_TIMEOUT = 2.0
INTERVAL = 0.01


class FactoryReapRefused(RuntimeError):
    """The run's children could not be reaped within the teardown bound."""


class _Adoptions:
    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.lost = 0


def _enable_subreaper() -> None:
    if sys.platform != "linux":
        raise FactoryReapRefused("PR_SET_CHILD_SUBREAPER requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        errno = ctypes.get_errno()
        raise FactoryReapRefused(f"PR_SET_CHILD_SUBREAPER: {os.strerror(errno)}")


def _send(channel: socket.socket, packet: dict, *, report: bool = False) -> bool:
    data = json.dumps(packet, ensure_ascii=True).encode("ascii")
    if len(data) > MAX_PACKET:
        return False
    try:
        # Events must never stall teardown. Final/ready reports get a small,
        # separate bound, including when the executor stops reading entirely.
        if report and not select.select([], [channel], [], 0.1)[1]:
            return False
        return channel.send(data, socket.MSG_DONTWAIT) == len(data)
    except (OSError, ValueError):
        return False


def _children() -> list[int]:
    pid = os.getpid()
    with open(f"/proc/self/task/{pid}/children", encoding="ascii") as stream:
        return [int(value) for value in stream.read().split()]


def _signal(pid: int, sig: int, *, group: bool = False) -> None:
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        # Permission denial is not success: the bounded reap loop decides.
        pass


def _signal_owned(pid: int) -> None:
    try:
        group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return
    except PermissionError:
        group = False
    # Caller has not waited on this direct child, so its PID (and, for a
    # group leader, PGID) cannot have been recycled between lookup and kill.
    _signal(pid, signal.SIGKILL, group=group)


def _observe(channel: socket.socket, payload: int, adoptions: _Adoptions) -> list[int]:
    children = _children()
    for pid in children:
        if pid != payload and pid not in adoptions.seen:
            adoptions.seen.add(pid)
            if not _send(channel, {"kind": "event", "event": "worklink_factory_orphan_adopted", "pid": pid}):
                # Fail the run, not the reap loop. This sticky count survives
                # reaping/PID reuse and cannot grow into an unbounded log queue.
                adoptions.lost += 1
    return children


def _teardown(channel: socket.socket, payload: subprocess.Popen, adoptions: _Adoptions) -> int:
    deadline = time.monotonic() + TERM_GRACE + REAP_TIMEOUT
    _signal(payload.pid, signal.SIGTERM, group=True)
    grace_end = time.monotonic() + TERM_GRACE
    while time.monotonic() < grace_end:
        _observe(channel, payload.pid, adoptions)
        time.sleep(min(INTERVAL, max(0, grace_end - time.monotonic())))
    # Do not poll/wait/reap the payload before this kill: its zombie is the
    # identity anchor even if it exited before cleanup began.
    _signal(payload.pid, signal.SIGKILL, group=True)
    exit_code = None
    while True:
        if time.monotonic() >= deadline:
            raise FactoryReapRefused("children survived the bounded teardown deadline")
        children = _observe(channel, payload.pid, adoptions)
        for pid in children:
            if time.monotonic() >= deadline:
                raise FactoryReapRefused("children survived the bounded teardown deadline")
            _signal_owned(pid)
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                adoptions.seen.discard(pid)
                if pid == payload.pid:
                    exit_code = os.waitstatus_to_exitcode(status)
                    payload.returncode = exit_code
        # WNOWAIT avoids silently reaping an adoption that raced the snapshot.
        # Only ECHILD proves that no live children can produce another cascade.
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            if exit_code is None:
                raise FactoryReapRefused("payload status was not collected")
            return exit_code
        time.sleep(INTERVAL)


def supervise(channel: socket.socket, argv: list[str]) -> int:
    """Own one run; return 0 only after delivering its successful terminal report.

    Payload status uses subprocess conventions (negative signal numbers).
    Any received packet, including malformed JSON, or EOF requests teardown.
    Adoption-event delivery failure also requests teardown and fails the run,
    even if all children are subsequently reaped and the socket recovers.
    """
    payload = None
    adoptions = _Adoptions()
    error = None
    exit_code = None
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        # Do not raise here: Popen must finish assigning the owned payload, and
        # repeated signals during teardown must not interrupt/reset its budget.
        stop_requested = True

    previous_handlers = {}
    try:
        # SIGKILL cannot be handled; the controller's
        # worklink_factory_supervisor_lost path covers that residual case.
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        channel.set_inheritable(False)
        channel.setblocking(False)
        _enable_subreaper()
        if not _send(channel, {"kind": "ready"}, report=True):
            raise RuntimeError("could not report supervisor readiness")
        payload = subprocess.Popen(argv, start_new_session=True, close_fds=True)
        while not stop_requested:
            children = _observe(channel, payload.pid, adoptions)
            if adoptions.lost:
                break
            for pid in children:
                # Report before reaping, and never release the payload anchor.
                # Live adoptees remain pinned for later teardown signalling.
                if pid != payload.pid and os.waitpid(pid, os.WNOHANG)[0]:
                    adoptions.seen.discard(pid)
            if os.waitid(os.P_PID, payload.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            if select.select([channel], [], [], INTERVAL)[0]:
                channel.recv(MAX_PACKET)
                break
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            if payload is not None:
                try:
                    exit_code = _teardown(channel, payload, adoptions)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
            if adoptions.lost:
                loss = f"adoption event delivery failed: {adoptions.lost} event(s) lost"
                error = f"{loss}; {error}" if error else loss
            if error is not None:
                _send(channel, {"kind": "event", "event": "worklink_factory_reap_refused", "error": error}, report=True)
                _send(channel, {"kind": "terminal", "error": error}, report=True)
            else:
                if _send(channel, {"kind": "terminal", "exit_code": exit_code}, report=True):
                    return 0
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run with an inherited control socket descriptor followed by payload argv."""
    args = sys.argv[1:] if argv is None else argv
    with socket.socket(fileno=int(args[0])) as channel:
        return supervise(channel, args[1:])


if __name__ == "__main__":
    sys.exit(main())
