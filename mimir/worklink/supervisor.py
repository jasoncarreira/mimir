"""The root-supervised half of Worklink containment.

Runs as root under s6 and is the only thing in the system that drops privilege.
It takes requests from the spool, spawns each as the contained user, and
publishes what it OBSERVED -- exit status, captured output, and the commit oid it
read from the checkout afterwards.

Why the observation matters
---------------------------
The verdict that gates a push must not come from the thing being judged. If the
build wrote its own result file, a bad generation could report success it did not
earn, and the controller would push it. So the supervisor never reads a
build-authored result: it reports the exit status of the process it spawned and
the oid it read itself.

Why this is Python and not shell
--------------------------------
The service needs JSON, atomic publication and correct spool modes. In shell
those are three subtle bugs waiting to happen, and none of it would be testable.
Here ``handle_one_request`` is a pure function of the spool state, so the tests
drive the real code path rather than a mock of it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from mimir.worklink.containment import (
    DEFAULT_CONTAINED_USER,
    DEFAULT_SPOOL_ROOT,
    ContainmentPolicy,
    WorkerRequest,
    WorkerResult,
    cancel_path,
    containment_required,
    observe_head,
    request_dir,
    result_dir,
    spawn_argv,
)

__all__ = ["prepare_spool", "handle_one_request", "serve_forever", "main"]

#: Controller writes, contained user reads only. The contained user is neither
#: the owner nor in the owning group, so the absent "other" write bit is what
#: stops it rewriting its own request.
_REQUEST_DIR_MODE = 0o750
#: Root publishes, controller reads, contained user has no access at all.
_RESULT_DIR_MODE = 0o750


def _lookup_ids(user: str) -> tuple[int, int] | None:
    try:
        import pwd

        entry = pwd.getpwnam(user)
    except (ImportError, KeyError):
        return None
    return entry.pw_uid, entry.pw_gid


def prepare_spool(
    spool_root: Path,
    *,
    controller_user: str = "mimir",
    contained_user: str = DEFAULT_CONTAINED_USER,
) -> None:
    """Create the spool with the ownership that carries the identity split.

    Called by the supervisor at start, as root, before any request is accepted.
    Getting these modes wrong is the one way this design fails silently -- the
    service would run, builds would succeed, and the contained user would be able
    to forge its own verdict.
    """
    requests = request_dir(spool_root)
    results = result_dir(spool_root)
    requests.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    controller = _lookup_ids(controller_user)
    contained = _lookup_ids(contained_user)
    if controller is not None and contained is not None and os.geteuid() == 0:
        # requests: owned by the controller so it can publish, group-readable by
        # the contained user so the supervisor's spawned step can read its own
        # request. Not group-writable -- that is the whole point.
        os.chown(requests, controller[0], contained[1])
        # results: root-owned. The contained user is not the owner and not in the
        # group, so it cannot read or write a result at all.
        os.chown(results, 0, controller[1])
    requests.chmod(_REQUEST_DIR_MODE)
    results.chmod(_RESULT_DIR_MODE)


def _run_supervised(
    argv: tuple[str, ...],
    request: WorkerRequest,
    policy: ContainmentPolicy,
    request_id: str,
) -> tuple[int, str, str, bool, bool]:
    """Spawn the step, enforcing the deadline and honouring cancellation.

    ``subprocess.run(timeout=...)`` alone cannot do this: a cancellation that
    arrives after the step is claimed has to terminate a process owned by the
    CONTAINED user, and only this service is root. The controller can publish
    the request; it cannot signal the process.

    Killed by process GROUP, because the step is started in its own session --
    signalling only the direct child would leave a coding CLI's descendants
    running past the deadline.
    """
    deadline = None if not request.timeout_seconds else time.monotonic() + request.timeout_seconds
    cancel_marker = cancel_path(policy.spool_root, request_id)
    proc = subprocess.Popen(  # noqa: S603 - argv is controller-constructed
        list(argv),
        cwd=str(request.cwd),
        env=dict(request.env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = cancelled = False
    while proc.poll() is None:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        if cancel_marker.exists():
            cancelled = True
            break
        time.sleep(0.1)
    if timed_out or cancelled:
        _terminate_group(proc)
    stdout, stderr = proc.communicate()
    cancel_marker.unlink(missing_ok=True)
    if timed_out:
        stderr = f"{stderr}\nworklink: step timed out after {request.timeout_seconds}s"
    if cancelled:
        stderr = f"{stderr}\nworklink: step cancelled by the controller"
    exit_status = proc.returncode if proc.returncode is not None else 124
    if timed_out or cancelled:
        exit_status = 124
    return exit_status, stdout or "", stderr or "", timed_out, cancelled


def _terminate_group(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the step's process group, then SIGKILL what survives."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):  # pragma: no cover - already gone
        return
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):  # pragma: no cover
            return
        waited = 0.0
        while waited < grace:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
            waited += 0.1


def _publish(results: Path, request_id: str, result: WorkerResult) -> None:
    """Write a result atomically so the controller never reads a partial one."""
    tmp = results / f".{request_id}.tmp"
    tmp.write_text(json.dumps(result.to_json()), encoding="utf-8")
    tmp.rename(results / f"{request_id}.json")


def handle_one_request(
    policy: ContainmentPolicy,
    request_path: Path,
    *,
    spawn: object = None,
) -> WorkerResult:
    """Execute one spooled request and publish the observed result.

    ``spawn`` is injected only so tests can drive this without root. It defaults
    to the real ``subprocess.run``; production never passes it.
    """
    raw = json.loads(request_path.read_text(encoding="utf-8"))
    request_id = str(raw.get("request_id") or request_path.stem)
    request = WorkerRequest.from_json(raw)
    runner = spawn if spawn is not None else subprocess.run

    argv = spawn_argv(policy, request.argv)
    timed_out = False
    cancelled = False
    try:
        if spawn is not None:
            # Injected runner (tests): no cancellation polling to do.
            proc = runner(  # type: ignore[operator]
                list(argv),
                cwd=str(request.cwd),
                env=dict(request.env),
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
            exit_status = int(proc.returncode)
            stdout, stderr = proc.stdout or "", proc.stderr or ""
        else:
            exit_status, stdout, stderr, timed_out, cancelled = _run_supervised(
                argv, request, policy, request_id,
            )
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_status, stdout, stderr = 124, "", (
            f"worklink: step timed out after {request.timeout_seconds}s"
        )
    except OSError as exc:
        exit_status, stdout, stderr = 127, "", f"worklink: could not spawn step: {exc}"

    # Read HEAD OURSELVES rather than accepting one the step reported. The
    # controller pushes this oid, so this read is the anchor for the whole
    # post-verdict race.
    head = observe_head(request.cwd) if request.report_head and not timed_out else None

    result = WorkerResult(
        attempt_id=request.attempt_id,
        exit_status=exit_status,
        stdout=stdout,
        stderr=stderr,
        head_oid=head,
        timed_out=timed_out,
    )
    _publish(result_dir(policy.spool_root), request_id, result)
    request_path.unlink(missing_ok=True)
    return result


def serve_forever(
    policy: ContainmentPolicy,
    *,
    poll_seconds: float = 0.2,
    max_iterations: int | None = None,
) -> None:
    """Consume spooled requests until stopped.

    ``max_iterations`` exists so a test can run the real loop and have it return.
    """
    requests = request_dir(policy.spool_root)
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        for entry in sorted(requests.glob("*.json")):
            try:
                handle_one_request(policy, entry)
            except Exception as exc:  # noqa: BLE001 - one bad request must not
                # take down the service; the controller times out and reports it.
                print(f"worklink: request {entry.name} failed: {exc}", flush=True)
                entry.unlink(missing_ok=True)
        time.sleep(poll_seconds)


def main() -> int:
    """Entry point for the s6 service. Runs as root."""
    if not containment_required():
        print(
            "worklink: MIMIR_CODING_ENABLED is not set, so no build will run and "
            "there is nothing to contain; idling",
            flush=True,
        )
        return 0
    spool_root = Path(os.environ.get("MIMIR_WORKLINK_SPOOL", str(DEFAULT_SPOOL_ROOT)))
    user = os.environ.get("MIMIR_WORKLINK_USER", DEFAULT_CONTAINED_USER)
    prepare_spool(spool_root, contained_user=user)
    policy = ContainmentPolicy(user=user, spool_root=spool_root, verified=True)
    print(f"worklink: supervising {spool_root}, spawning steps as {user}", flush=True)
    serve_forever(policy)
    return 0


if __name__ == "__main__":  # pragma: no cover - service entry point
    raise SystemExit(main())
