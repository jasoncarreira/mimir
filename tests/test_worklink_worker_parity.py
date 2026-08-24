from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
from typing import Any
import uuid

import pytest

import mimir.worklink.compute as compute
import mimir.worklink.worker_exec as worker_exec
from mimir.worklink.compute import ComputeLaunchError, LaunchHandle, LocalSubprocessComputeBackend, WorkSpec


SCENARIOS = (
    "normal_completion",
    "timeout",
    "running_cancellation",
    "concurrency",
    "coding_disabled",
)


class Authorization:
    path = Path("/authorized")

    def __init__(self) -> None:
        self.verifications = 0

    def verify(self, path: Path | None) -> None:
        self.verifications += 1
        assert path == self.path

    def duplicate_fd(self) -> int:
        return os.open(os.devnull, os.O_RDONLY)


class WorkerProcess:
    def __init__(
        self,
        identifier: str,
        pid: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
        events: list[str] | None = None,
    ) -> None:
        self.identifier = identifier
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.release = asyncio.Event()
        self.events = events

    async def wait(self) -> int:
        await self.release.wait()
        if self.events is not None:
            self.events.append(f"reap:{self.identifier}")
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class WorkerClient:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        immediate: bool = True,
        outputs: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.immediate = immediate
        self.outputs = outputs
        self.launched: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.processes: dict[str, WorkerProcess] = {}
        self.events: list[str] = []

    async def launch(self, **kwargs: Any) -> WorkerProcess:
        self.launched.append(kwargs)
        stdout, stderr = (
            self.outputs[len(self.launched) - 1]
            if self.outputs is not None
            else (self.stdout, self.stderr)
        )
        kwargs["stdout_sink"].file.write(stdout)
        kwargs["stderr_sink"].file.write(stderr)
        process = WorkerProcess(
            kwargs["identifier"],
            4000 + len(self.launched),
            stdout,
            stderr,
            self.events,
        )
        if self.immediate:
            process.release.set()
        self.processes[process.identifier] = process
        return process

    async def cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)
        process = self.processes[identifier]
        process.returncode = -15
        process.release.set()


def spec(
    *,
    backend: str = "opencode",
    env: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> WorkSpec:
    return WorkSpec(
        issue_id=1,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/1-a1",
        prompt="prompt",
        rules=None,
        test_command="",
        backend=backend,
        timeout_s=2,
        env=env or {},
        backend_config=config or {},
        local_checkout=Path("/authorized"),
        local_argv=("python", "-c", "print('ok')"),
        output_root=Path.cwd() / ".pytest-worklink-output",
    )


@pytest.mark.asyncio
async def test_operator_issued_opencode_uses_fd_anchored_dir_but_direct_keeps_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / ("a" * 64) / "1-1" / "checkout"
    checkout.mkdir(parents=True)
    authorization = Authorization()
    authorization.path = checkout
    client = WorkerClient()
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: True)
    monkeypatch.setattr("mimir.worklink.run_state.process_start_ticks", lambda pid: pid)
    work = spec()
    object.__setattr__(work, "output_root", tmp_path / "output")
    object.__setattr__(work, "local_checkout", checkout)
    object.__setattr__(
        work,
        "local_argv",
        ("opencode", "run", "--dir", str(checkout), "--", "prompt"),
    )

    worker = LocalSubprocessComputeBackend.for_authorized_checkout(
        authorization, worker_client=client
    )
    await worker.launch(work)

    assert client.launched[0]["argv"] == (
        "opencode", "run", "--dir", ".", "--", "prompt"
    )
    assert work.local_argv == (
        "opencode", "run", "--dir", str(checkout), "--", "prompt"
    )

    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> WorkerProcess:
        calls.append(args)
        return WorkerProcess("direct", 5000)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    direct = LocalSubprocessComputeBackend()
    await direct.launch(work)
    assert calls == [work.local_argv]


def test_fd_anchored_opencode_argv_passes_through_non_checkout_commands() -> None:
    checkout = Path("/authorized")
    command = ("python", "-c", "print('ok')")

    assert compute._fd_anchored_opencode_argv(command, checkout) == command


def test_fd_anchored_opencode_argv_accepts_relative_issued_checkout() -> None:
    command = ("opencode", "run", "--dir", ".", "--", "prompt")

    assert compute._fd_anchored_opencode_argv(command, Path("/authorized")) == command


def test_fd_anchored_opencode_argv_rejects_a_different_checkout() -> None:
    with pytest.raises(
        ComputeLaunchError,
        match="enabled OpenCode --dir must name the issued checkout",
    ):
        compute._fd_anchored_opencode_argv(
            ("opencode", "run", "--dir", "/other", "--", "prompt"),
            Path("/authorized"),
        )


async def launch_worker(
    monkeypatch: pytest.MonkeyPatch, client: object
) -> tuple[LocalSubprocessComputeBackend, LaunchHandle]:
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: True)
    monkeypatch.setattr("mimir.worklink.run_state.process_start_ticks", lambda pid: pid)
    backend = LocalSubprocessComputeBackend.for_authorized_checkout(
        getattr(client, "checkout", None) or Authorization(),
        worker_client=client,
    )
    return backend, await backend.launch(spec())


@pytest.mark.asyncio
async def test_enabled_launch_cancellation_waits_for_handshake_then_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SuspendedClient(WorkerClient):
        def __init__(self) -> None:
            super().__init__(immediate=False)
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()

        async def launch(self, **kwargs: Any) -> WorkerProcess:
            self.entered.set()
            await self.resume.wait()
            return await super().launch(**kwargs)

    client = SuspendedClient()
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: True)
    monkeypatch.setattr("mimir.worklink.run_state.process_start_ticks", lambda pid: pid)
    backend = LocalSubprocessComputeBackend.for_authorized_checkout(
        Authorization(), worker_client=client
    )
    launch = asyncio.create_task(backend.launch(spec()))
    await client.entered.wait()
    launch.cancel()
    client.resume.set()

    with pytest.raises(asyncio.CancelledError):
        await launch

    assert len(client.cancelled) == 1
    identifier = client.cancelled[0]
    assert client.processes[identifier].returncode == -15
    assert backend._jobs == {}
    assert backend._handles == {}


@pytest.mark.asyncio
async def test_enabled_launch_failure_after_suspension_is_a_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SuspendedFailingClient:
        async def launch(self, **_kwargs: Any) -> WorkerProcess:
            await asyncio.sleep(0)
            raise OSError("executor unavailable")

    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: True)
    backend = LocalSubprocessComputeBackend.for_authorized_checkout(
        Authorization(), worker_client=SuspendedFailingClient()
    )

    with pytest.raises(ComputeLaunchError, match="executor unavailable"):
        await backend.launch(spec())
    assert backend._jobs == {}
    assert backend._handles == {}


@pytest.mark.asyncio
async def test_job_alive_tracks_contained_task_and_direct_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = WorkerClient(immediate=False)
    backend, handle = await launch_worker(monkeypatch, client)
    assert backend.job_alive(handle) is True

    client.processes[handle.identifier].release.set()
    await backend.wait(handle, 2)
    assert backend.job_alive(handle) is False
    await backend.cleanup(handle)
    assert backend.job_alive(handle) is False

    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    direct = LocalSubprocessComputeBackend()
    direct_handle = await direct.launch(
        direct_spec("import time; time.sleep(30)")
    )
    assert direct.job_alive(direct_handle) is True
    await direct.cancel(direct_handle)
    await direct.wait(direct_handle, 2)
    assert direct.job_alive(direct_handle) is False
    await direct.cleanup(direct_handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_layer", ["cancel", "collection"])
async def test_contained_timeout_bounds_cancel_and_collection_independently(
    monkeypatch: pytest.MonkeyPatch, stuck_layer: str
) -> None:
    job = asyncio.create_task(asyncio.Event().wait())
    handle = LaunchHandle("local_subprocess", str(uuid.uuid4()))

    class Client:
        async def cancel(self, _identifier: str) -> None:
            if stuck_layer == "cancel":
                await asyncio.Event().wait()

    backend = LocalSubprocessComputeBackend()
    backend._jobs[handle.identifier] = (job, spec(), ("worker",))
    backend._handles[handle.identifier] = handle
    backend._worker_clients[handle.identifier] = Client()
    monkeypatch.setattr(compute, "_WORKER_CANCEL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(compute, "_WORKER_COLLECTION_TIMEOUT_S", 0.01)

    message = (
        "worker cancellation exceeded"
        if stuck_layer == "cancel"
        else "worker did not report a terminal result"
    )
    with pytest.raises(RuntimeError, match=message):
        await asyncio.wait_for(backend.wait(handle, 0.001), timeout=0.2)

    if stuck_layer == "collection":
        assert job.done()
        assert job.cancelled()
    else:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_cancel_socket_receive_has_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.worklink.worker_client as worker_client

    observed_timeouts: list[float] = []

    class TimeoutSocket:
        def settimeout(self, timeout_s: float) -> None:
            observed_timeouts.append(timeout_s)

        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *_args: Any) -> bytes:
            return struct.pack("3i", 4321, 0, 4321)

        def send(self, _payload: bytes) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            raise socket.timeout("cancel acknowledgement timed out")

        def close(self) -> None:
            pass

    monkeypatch.setattr(worker_client.socket, "socket", lambda *_args: TimeoutSocket())
    monkeypatch.setattr(worker_client.socket, "SO_PEERCRED", 17, raising=False)

    with pytest.raises(socket.timeout, match="acknowledgement timed out"):
        await worker_client.WorkerClient(Authorization()).cancel(str(uuid.uuid4()))

    assert observed_timeouts == [worker_client.CANCEL_SOCKET_TIMEOUT_S]


@pytest.mark.asyncio
async def test_direct_process_reap_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 4321

        async def wait(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(compute.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", lambda _pgid: None)
    monkeypatch.setattr(compute, "_DIRECT_PROCESS_WAIT_TIMEOUT_S", 0.01)

    with pytest.raises(RuntimeError, match="leader was not reaped after SIGKILL"):
        await asyncio.wait_for(compute._kill_process_group(Process()), timeout=0.2)


def direct_spec(source: str) -> WorkSpec:
    work = spec()
    object.__setattr__(work, "local_checkout", Path.cwd())
    object.__setattr__(work, "local_argv", (sys.executable, "-c", source))
    return work


async def run_direct(
    monkeypatch: pytest.MonkeyPatch, source: str, timeout: float = 3
) -> Any:
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    backend = LocalSubprocessComputeBackend()
    handle = await backend.launch(direct_spec(source))
    result = await backend.wait(handle, timeout)
    await backend.cleanup(handle)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["sigterm", "timeout"])
async def test_direct_terminated_output_is_durable_while_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, termination: str
) -> None:
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    backend = LocalSubprocessComputeBackend()
    work = direct_spec("import time; time.sleep(30)")
    object.__setattr__(
        work,
        "local_argv",
        (
            "/usr/bin/node",
            "-e",
            "process.stdout.write('before termination\\n'); "
            "process.stderr.write('diagnostic stderr\\n'); "
            "setTimeout(() => {}, 30000)",
        ),
    )
    output_root = tmp_path / "state" / "worklink" / "transcripts"
    object.__setattr__(work, "output_root", output_root)
    handle = await backend.launch(work)

    stdout_files: list[Path] = []
    for _ in range(200):
        stdout_files = list(output_root.glob("*.stdout.log"))
        if stdout_files and stdout_files[0].read_bytes() == b"before termination\n":
            break
        await asyncio.sleep(0.01)
    assert len(stdout_files) == 1
    assert stdout_files[0].read_bytes() == b"before termination\n"
    assert stat.S_ISREG(stdout_files[0].stat().st_mode)

    if termination == "sigterm":
        await backend.cancel(handle)
        result = await backend.wait(handle, 2)
        assert result.timed_out is False
    else:
        result = await backend.wait(handle, 0.01)
        assert result.timed_out is True
    await backend.cleanup(handle)

    assert result.stdout_path == stdout_files[0]
    assert result.stdout_path.read_text() == "before termination\n"
    assert result.stderr_path is not None
    assert result.stderr_path.read_text() == "diagnostic stderr\n"
    assert result.stdout_path.stat().st_mode & 0o777 == 0o600
    assert result.stderr_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["timeout", "overflow"])
async def test_direct_termination_kills_pipe_holding_grandchild(
    monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    original_terminate = worker_exec._terminate_process_group_pid
    signals: list[int] = []
    original_killpg = os.killpg

    def expedited_terminate(process_group: int, timeout_s: float = 5.0) -> None:
        original_terminate(process_group, 0.05)

    def observed_killpg(process_group: int, sig: int) -> None:
        signals.append(sig)
        original_killpg(process_group, sig)

    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", expedited_terminate)
    monkeypatch.setattr(worker_exec.os, "killpg", observed_killpg)
    monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
    if trigger == "overflow":
        monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDOUT_BYTES", "1")
    output = "overflow" if trigger == "overflow" else "ready"
    source = (
        "import os,subprocess,sys,time; "
        "ready_r,ready_w=os.pipe(); "
        "child_source=f\"import os,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "os.write({ready_w},b'1'); os.close({ready_w}); time.sleep(30)\"; "
        "subprocess.Popen([sys.executable,'-c',child_source],pass_fds=(ready_w,)); "
        "os.close(ready_w); "
        "assert os.read(ready_r,1)==b'1'; os.close(ready_r); "
        f"print({output!r},flush=True); "
        "time.sleep(30)"
    )
    backend = LocalSubprocessComputeBackend()
    handle = await backend.launch(direct_spec(source))

    result = await asyncio.wait_for(
        # The source prints only after the grandchild has installed its
        # SIGTERM-ignore handler, so timeout termination is deterministic.
        backend.wait(handle, 0.2 if trigger == "timeout" else 2), timeout=3
    )
    await backend.cleanup(handle)

    assert result.timed_out is (trigger == "timeout")
    assert result.output_overflow is (trigger == "overflow")
    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_closed_worker_direct_parity_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    assert SCENARIOS == (
        "normal_completion",
        "timeout",
        "running_cancellation",
        "concurrency",
        "coding_disabled",
    )
    if scenario == "normal_completion":
        import mimir.worklink.worker_client as worker_client

        checkout = tmp_path / "executor-checkout"
        checkout.mkdir()
        checkout_stat = checkout.stat()

        class PipeAuthorization:
            path = checkout
            issue_id = 1
            attempt = 1
            device = checkout_stat.st_dev
            inode = checkout_stat.st_ino

            def verify(self, path: Path | None) -> None:
                assert path == checkout

            def duplicate_fd(self) -> int:
                return os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)

        client_socket, executor_socket = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_DGRAM
        )
        authorization = PipeAuthorization()
        client = worker_client.WorkerClient(authorization)
        monkeypatch.setattr(client, "_connect", lambda: client_socket)
        monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: True)
        monkeypatch.setattr("mimir.worklink.run_state.process_start_ticks", lambda pid: pid)
        monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "worker-homes")
        worker_exec.HOME_ROOT.mkdir()
        monkeypatch.setattr(worker_exec, "_validate_checkout", lambda *_args: None)
        monkeypatch.setattr(worker_exec.os, "chown", lambda *_args, **_kwargs: None)

        def enter_worker(fd: int) -> None:
            os.setsid()
            os.fchdir(fd)

        monkeypatch.setattr(worker_exec, "_drop_worker", enter_worker)

        def serve() -> None:
            try:
                worker_exec.handle_connection(executor_socket)
            finally:
                executor_socket.close()

        server = threading.Thread(target=serve)
        server.start()
        source = (
            "import os,threading; d=b'0123456789abcdef'*(64*1024+1); "
            "ts=[threading.Thread(target=os.write,args=(fd,d)) for fd in (1,2)]; "
            "[t.start() for t in ts]; [t.join() for t in ts]"
        )
        work = spec(env={"OPENCODE_PERMISSION": '{"edit":"allow"}'})
        object.__setattr__(work, "local_checkout", checkout)
        object.__setattr__(work, "local_argv", (sys.executable, "-c", source))
        backend = LocalSubprocessComputeBackend.for_authorized_checkout(
            authorization, worker_client=client
        )
        handle = await backend.launch(work)
        worker = await backend.wait(handle, 5)
        server.join(timeout=5)
        assert not server.is_alive()
        assert handle.identifier not in worker_exec._jobs
        assert not (worker_exec.HOME_ROOT / handle.identifier).exists()
        await backend.cleanup(handle)
        with pytest.raises(KeyError):
            await backend.wait(handle, 1)

        monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
        direct_backend = LocalSubprocessComputeBackend()
        direct_handle = await direct_backend.launch(direct_spec(source))
        direct = await direct_backend.wait(direct_handle, 5)
        await direct_backend.cleanup(direct_handle)
        with pytest.raises(KeyError):
            await direct_backend.wait(direct_handle, 1)
        assert handle.identifier not in backend._jobs
        assert direct_handle.identifier not in direct_backend._jobs

        payload = b"0123456789abcdef" * (64 * 1024 + 1)
        assert len(payload) > 1024 * 1024
        assert (worker.exit_code, worker.stdout, worker.stderr) == (
            direct.exit_code,
            direct.stdout,
            direct.stderr,
        )
        assert worker.stdout.encode() == payload
        assert worker.stderr.encode() == payload
        assert worker.exit_code == 0

    elif scenario == "timeout":
        client = WorkerClient(immediate=False)
        backend, handle = await launch_worker(monkeypatch, client)
        worker = await backend.wait(handle, 0.01)
        await backend.cleanup(handle)
        direct = await run_direct(monkeypatch, "import time; time.sleep(2)", timeout=0.01)
        assert client.cancelled == [handle.identifier]
        assert worker.timed_out is direct.timed_out is True
        assert worker.exit_code == direct.exit_code == -15

    elif scenario == "running_cancellation":
        import mimir.worklink.worker_client as worker_client

        authorization = Authorization()
        socket_events: list[str] = []
        sent: list[dict[str, Any]] = []
        executor_events: list[str] = []
        executor_process = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                "trap '' TERM; while :; do sleep 1; done",
            ],
            start_new_session=True,
        )
        original_executor_terminate = worker_exec._terminate_process_group
        original_executor_killpg = worker_exec.os.killpg

        def observed_executor_killpg(pgid: int, sig: int) -> None:
            executor_events.append("term" if sig == signal.SIGTERM else "kill")
            original_executor_killpg(pgid, sig)

        def expedited_executor_terminate(
            proc: subprocess.Popen[bytes], timeout_s: float = 5.0
        ) -> None:
            original_executor_terminate(proc, 0.05)
            executor_events.append("reap")

        monkeypatch.setattr(worker_exec.os, "killpg", observed_executor_killpg)
        monkeypatch.setattr(worker_exec, "_terminate_process_group", expedited_executor_terminate)

        class CancelSocket:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                socket_events.append("socket")

            def connect(self, path: str) -> None:
                assert path == str(worker_client.DEFAULT_EXECUTOR_SOCKET)
                socket_events.append("connect")

            def settimeout(self, timeout_s: float) -> None:
                assert timeout_s == worker_client.CANCEL_SOCKET_TIMEOUT_S
                socket_events.append("timeout")

            def getsockopt(self, *_args: Any) -> bytes:
                socket_events.append("peer-credentials")
                return struct.pack("3i", 4321, 0, 4321)

            def send(self, payload: bytes) -> None:
                sent.append(json.loads(payload))
                socket_events.append("cancel-request")

            def recv(self, _size: int) -> bytes:
                identifier = sent[-1]["id"]
                worker_exec._cancel(identifier)
                auth_client.processes[identifier].returncode = -signal.SIGKILL
                auth_client.processes[identifier].release.set()
                socket_events.append("cancel-ack")
                return json.dumps(
                    {"id": identifier, "status": "cancelled"}
                ).encode()

            def close(self) -> None:
                socket_events.append("close")

        class AuthenticatedClient(worker_client.WorkerClient):
            def __init__(self) -> None:
                super().__init__(authorization)
                self.processes: dict[str, WorkerProcess] = {}

            async def launch(self, **kwargs: Any) -> WorkerProcess:
                self.checkout.verify(kwargs["local_checkout"])
                process = WorkerProcess(kwargs["identifier"], 4567)
                self.processes[process.identifier] = process
                return process

        monkeypatch.setattr(worker_client.socket, "socket", CancelSocket)
        monkeypatch.setattr(worker_client.socket, "SO_PEERCRED", 17, raising=False)
        auth_client = AuthenticatedClient()
        backend, handle = await launch_worker(monkeypatch, auth_client)
        worker_exec._jobs[handle.identifier] = executor_process
        await asyncio.sleep(0.1)
        await backend.cancel(handle)
        worker = await backend.wait(handle, 2)
        executor_events.append("terminal")
        await backend.cleanup(handle)
        executor_events.append("cleanup")
        worker_exec._jobs.pop(handle.identifier, None)
        assert authorization.verifications == 1
        assert sent == [
            {
                "version": 1,
                "op": "cancel",
                "id": handle.identifier,
                "executor_identity": worker_exec.EXECUTOR_PROTOCOL_IDENTITY,
            }
        ]
        assert socket_events == [
            "socket",
            "timeout",
            "connect",
            "peer-credentials",
            "cancel-request",
            "cancel-ack",
            "close",
        ]
        assert worker.exit_code == -signal.SIGKILL
        assert executor_events == ["term", "kill", "reap", "terminal", "cleanup"]

        direct_events: list[str] = []
        original_create = asyncio.create_subprocess_exec
        original_killpg = os.killpg

        class ObservedProcess:
            def __init__(self, process: asyncio.subprocess.Process) -> None:
                self._process = process
                self.pid = process.pid
                self.stdout = process.stdout
                self.stderr = process.stderr
                self._reap_recorded = False

            @property
            def returncode(self) -> int | None:
                return self._process.returncode

            async def wait(self) -> int:
                result = await self._process.wait()
                if not self._reap_recorded:
                    self._reap_recorded = True
                    direct_events.append("reap")
                return result

        async def observed_create(*args: Any, **kwargs: Any) -> ObservedProcess:
            return ObservedProcess(await original_create(*args, **kwargs))

        def observed_killpg(pgid: int, sig: int) -> None:
            direct_events.append("term" if sig == signal.SIGTERM else "kill")
            original_killpg(pgid, sig)

        monkeypatch.setattr(compute.asyncio, "create_subprocess_exec", observed_create)
        monkeypatch.setattr(compute.os, "killpg", observed_killpg)
        monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
        direct_backend = LocalSubprocessComputeBackend()
        direct_handle = await direct_backend.launch(
            direct_spec(
                "import os,signal,time; "
                "signal.signal(signal.SIGTERM, lambda *_: os.write(1,b'term\\n')); "
                "os.write(1,b'ready\\n'); time.sleep(30)"
            )
        )
        direct = await direct_backend.wait(direct_handle, 0.2)
        direct_events.append("terminal")
        await direct_backend.cleanup(direct_handle)
        direct_events.append("cleanup")
        assert direct_events == ["term", "kill", "reap", "terminal", "cleanup"]
        assert (direct.exit_code, direct.stdout, direct.stderr, direct.timed_out) == (
            -signal.SIGKILL,
            "ready\nterm\n",
            "",
            True,
        )

    elif scenario == "concurrency":
        client = WorkerClient(
            immediate=False,
            outputs=[(b"worker one\n", b"err one\n"), (b"worker two\n", b"err two\n")],
        )
        backend, first = await launch_worker(monkeypatch, client)
        second = await backend.launch(spec())
        assert first.identifier != second.identifier
        first_wait = asyncio.create_task(backend.wait(first, 2))
        second_wait = asyncio.create_task(backend.wait(second, 2))
        mismatched = LaunchHandle(
            first.substrate,
            first.identifier,
            second.process_start_ticks,
            first.shim_pid,
        )
        with pytest.raises(KeyError):
            await backend.wait(mismatched, 1)
        with pytest.raises(RuntimeError, match="identity no longer matches"):
            await backend.cancel(mismatched)
        with pytest.raises(KeyError):
            await backend.cleanup(mismatched)
        assert first.identifier in backend._jobs
        assert second.identifier in backend._jobs
        assert client.cancelled == []
        assert not first_wait.done()
        assert not second_wait.done()
        client.processes[second.identifier].release.set()
        second_result = await second_wait
        assert not first_wait.done()
        await backend.cancel(first)
        first_result = await first_wait
        await backend.cleanup(second)
        await backend.cleanup(first)
        assert client.cancelled == [first.identifier]
        assert (first_result.stdout, first_result.stderr, first_result.exit_code) == (
            "worker one\n",
            "err one\n",
            -15,
        )
        assert (second_result.stdout, second_result.stderr, second_result.exit_code) == (
            "worker two\n",
            "err two\n",
            0,
        )

        monkeypatch.setattr("mimir.worklink.checkout.coding_enabled", lambda: False)
        direct_backend = LocalSubprocessComputeBackend()
        direct_ready = tmp_path / "direct-one-ready"
        direct_first = await direct_backend.launch(
            direct_spec(
                "import pathlib,time; print('direct one',flush=True); "
                f"pathlib.Path({str(direct_ready)!r}).touch(); time.sleep(30)"
            )
        )
        direct_second = await direct_backend.launch(direct_spec("print('direct two')"))
        assert direct_first.identifier != direct_second.identifier
        direct_mismatched = LaunchHandle(
            direct_first.substrate,
            direct_first.identifier,
            direct_first.process_start_ticks,
            999,
        )
        with pytest.raises(KeyError):
            await direct_backend.wait(direct_mismatched, 1)
        with pytest.raises((KeyError, RuntimeError)):
            await direct_backend.cancel(direct_mismatched)
        with pytest.raises(KeyError):
            await direct_backend.cleanup(direct_mismatched)
        assert direct_first.identifier in direct_backend._jobs
        assert direct_second.identifier in direct_backend._jobs
        direct_second_result = await direct_backend.wait(direct_second, 2)
        for _ in range(200):
            if direct_ready.exists():
                break
            await asyncio.sleep(0.01)
        assert direct_ready.exists()
        await direct_backend.cancel(direct_first)
        direct_first_result = await direct_backend.wait(direct_first, 2)
        await direct_backend.cleanup(direct_first)
        await direct_backend.cleanup(direct_second)
        assert (direct_first_result.stdout, direct_first_result.stderr, direct_first_result.exit_code) == (
            "direct one\n",
            "",
            -15,
        )
        assert (direct_second_result.stdout, direct_second_result.stderr, direct_second_result.exit_code) == (
            "direct two\n",
            "",
            0,
        )

    else:
        import mimir.worklink.worker_client as worker_client

        touched = {
            "authorization": 0,
            "projection": 0,
            "client": 0,
            "socket": 0,
            "launch": 0,
        }

        class ForbiddenAuthorization:
            path = Path("/forbidden")

            def verify(self, _path: Path | None) -> None:
                touched["authorization"] += 1
                raise AssertionError("disabled path reached authorization")

            def duplicate_fd(self) -> int:
                touched["authorization"] += 1
                raise AssertionError("disabled path duplicated authorization")

        class ForbiddenClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                touched["client"] += 1
                raise AssertionError("disabled path constructed worker client")

            async def launch(self, **_kwargs: Any) -> None:
                touched["launch"] += 1
                raise AssertionError("disabled path launched worker")

        class ForbiddenProjection:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                touched["projection"] += 1
                raise AssertionError("disabled path constructed projection")

        def forbidden_socket(*_args: Any, **_kwargs: Any) -> None:
            touched["socket"] += 1
            raise AssertionError("disabled path opened worker socket")

        monkeypatch.setattr(worker_client, "WorkerClient", ForbiddenClient)
        monkeypatch.setattr(worker_client, "WorkerProjection", ForbiddenProjection)
        monkeypatch.setattr(worker_client.socket, "socket", forbidden_socket)

        def disabled_spec() -> WorkSpec:
            disabled = direct_spec(
                "import os; print(os.environ['PARITY_DIRECT'])"
            )
            object.__setattr__(disabled, "env", {"PARITY_DIRECT": "unchanged"})
            return disabled

        def result_shape(result: compute.ComputeResult) -> tuple[object, ...]:
            return (
                result.exit_code,
                result.stdout,
                result.stderr,
                result.timed_out,
                result.output_overflow,
                result.launch_error,
                result.command,
            )

        monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
        baseline_backend = LocalSubprocessComputeBackend()
        baseline_handle = await baseline_backend.launch(disabled_spec())
        baseline = await baseline_backend.wait(baseline_handle, 2)
        await baseline_backend.cleanup(baseline_handle)

        for flag in ("false", None):
            if flag is None:
                monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
            else:
                monkeypatch.setenv("MIMIR_CODING_ENABLED", flag)
            backend = LocalSubprocessComputeBackend.for_authorized_checkout(
                ForbiddenAuthorization()
            )
            handle = await backend.launch(disabled_spec())
            result = await backend.wait(handle, 2)
            await backend.cleanup(handle)
            assert result_shape(result) == result_shape(baseline)
        assert touched == {
            "authorization": 0,
            "projection": 0,
            "client": 0,
            "socket": 0,
            "launch": 0,
        }


def test_enabled_environment_is_exact_and_closed() -> None:
    identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    work = spec(
        env={"OPENCODE_PERMISSION": '{"edit":"allow"}', "ANTHROPIC_API_KEY": "selected"},
        config={"pass_env": ["ANTHROPIC_API_KEY"]},
    )
    home = f"/var/lib/mimir-worklink/homes/{identifier}"
    assert compute._enabled_child_env(work, identifier) == {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "USER": "worklink",
        "LOGNAME": "worklink",
        "SHELL": "/bin/sh",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OPENCODE_CONFIG": f"{home}/.config/opencode/opencode.json",
        "OPENCODE_PERMISSION": '{"edit":"allow"}',
        "ANTHROPIC_API_KEY": "selected",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("", "x"),
        ("1BAD", "x"),
        ("A" * 129, "x"),
        ("NON_ASCII_É", "x"),
        ("CONTROL\tNAME", "x"),
        ("CONTROL\nNAME", "x"),
        ("BAD=NAME", "x"),
        *( (name, "x") for name in sorted(compute._ENABLED_SYNTHESIZED_NAMES) ),
        ("MIMIR_SECRET", "x"),
        ("GIT_CONFIG", "x"),
        ("GH_TOKEN", "x"),
        ("GITHUB_TOKEN", "x"),
        ("XDG_STATE_HOME", "x"),
        ("LD_PRELOAD", "x"),
        ("DYLD_INSERT_LIBRARIES", "x"),
        ("PYTHONPATH", "x"),
        ("BASH_ENV", "x"),
        ("MY_GITHUB_TOKEN", "x"),
        ("SAFE", ""),
        ("SAFE", "line\nvalue"),
        ("SAFE", "line\rvalue"),
        ("SAFE", "nul\x00value"),
        ("SAFE", "/home/mimir"),
        ("SAFE", "/home/mimir/"),
        ("SAFE", "/home/mimir/key"),
        ("SAFE", "/home//mimir/key"),
        ("SAFE", "/home/./mimir/key"),
        ("SAFE", "/tmp/../home/mimir/key"),
        ("SAFE", "/home/mimir/../mimir/key"),
        ("SAFE", "/safe:/tmp/../home/mimir/key"),
        ("SAFE", "PATH=/safe:/home/mimir/bin"),
        ("SAFE", "CWD=/home/mimir"),
        ("SAFE", "CWD=/tmp/../home/mimir/key"),
        ("SAFE", "CWD=/tmp/../../home/mimir/key"),
        ("SAFE", "SYMLINK=/home/mimir/key"),
        ("SAFE", "../home/mimir/key"),
        ("SAFE", "home/mimir/key"),
        ("SAFE", "../home/mimir/key"),
        ("SAFE", "file:///home/mimir/key"),
        ("SAFE", "x" * (64 * 1024 + 1)),
    ],
)
def test_enabled_environment_rejection_table(name: str, value: str) -> None:
    with pytest.raises(ComputeLaunchError):
        compute._enabled_child_env(
            spec(env={name: value}, config={"pass_env": [name]}),
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("A" * 128, "x"),
        ("SAFE", "x" * (64 * 1024)),
        ("SAFE", "é" * (32 * 1024)),
        ("SAFE", "/home/mimic/key"),
        ("SAFE", "/tmp/home/mimir/key"),
        ("SAFE", "CWD=/tmp/home/mimir/key"),
        ("SAFE", "CWD=/home/mimic/mimir/key"),
    ],
)
def test_enabled_environment_accepted_boundaries(name: str, value: str) -> None:
    env = compute._enabled_child_env(
        spec(env={name: value}, config={"pass_env": [name]}),
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    assert env[name] == value


@pytest.mark.parametrize(
    ("env", "config"),
    [
        ({"UNKNOWN": "x"}, {}),
        ({"SAFE": "x"}, {"pass_env": []}),
        ({"SAFE": "x"}, {"pass_env": ["SAFE", "SAFE"]}),
        ({"SAFE": "x"}, {"pass_env": ["MISSING"]}),
        ({"SAFE": "x"}, {"pass_env": "SAFE"}),
        ({"SAFE": "x"}, {"pass_env": b"SAFE"}),
        ({"SAFE": "x"}, {"pass_env": None}),
        ({"SAFE": "x"}, {"pass_env": 1}),
        ({"SAFE": "x"}, {"pass_env": {"SAFE": True}}),
        ({"SAFE": "x"}, {"pass_env": [1]}),
        ({"SAFE": "x"}, {"pass_env": [None]}),
        ({"SAFE": "x"}, {"pass_env": [b"SAFE"]}),
        ({"OPENCODE_PERMISSION": "[]"}, {}),
        ({"OPENCODE_PERMISSION": "not-json"}, {}),
        ({"OPENCODE_PERMISSION": 1}, {}),
    ],
)
def test_enabled_environment_closed_input_rejections(
    env: dict[str, Any], config: dict[str, Any]
) -> None:
    with pytest.raises(ComputeLaunchError):
        compute._enabled_child_env(
            spec(env=env, config=config),
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
