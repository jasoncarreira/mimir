from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
from typing import Any

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


def direct_spec(source: str) -> WorkSpec:
    work = spec()
    object.__setattr__(work, "local_checkout", Path.cwd())
    object.__setattr__(work, "local_argv", ("python", "-c", source))
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
        client = WorkerClient(stdout=b"worker stdout\n", stderr=b"worker stderr\n")
        backend, handle = await launch_worker(monkeypatch, client)
        worker = await backend.wait(handle, 2)
        assert worker == compute.ComputeResult(
            exit_code=0,
            stdout="worker stdout\n",
            stderr="worker stderr\n",
            handle=handle,
            command=("python", "-c", "print('ok')"),
        )
        assert client.processes[handle.identifier].returncode == 0
        await backend.cleanup(handle)
        with pytest.raises(KeyError):
            await backend.wait(handle, 1)

        checkout = tmp_path / "executor-checkout"
        checkout.mkdir()
        checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        executor_id = "11111111-1111-4111-8111-111111111111"
        responses: list[dict[str, Any]] = []

        class ExecutorConnection:
            def send(self, payload: bytes) -> None:
                responses.append(json.loads(payload))

        monkeypatch.setattr(worker_exec, "HOME_ROOT", tmp_path / "worker-homes")
        worker_exec.HOME_ROOT.mkdir()
        monkeypatch.setattr(worker_exec, "_validate_checkout", lambda *_args: None)
        monkeypatch.setattr(worker_exec.os, "chown", lambda *_args, **_kwargs: None)

        def enter_worker(fd: int) -> None:
            os.setsid()
            os.fchdir(fd)

        monkeypatch.setattr(worker_exec, "_drop_worker", enter_worker)
        request = {
            "version": 1,
            "op": "launch",
            "id": executor_id,
            "issue": 1,
            "attempt": 1,
            "device": 0,
            "inode": 0,
            "argv": ["/bin/sh", "-c", "printf executor-output"],
            "env": {"PATH": "/usr/bin:/bin"},
            "projections": [],
        }
        executor_fds = [checkout_fd, stdout_write, stderr_write]
        try:
            worker_exec._handle_launch(ExecutorConnection(), request, executor_fds)
            assert [response["status"] for response in responses] == ["started", "terminal"]
            assert responses[-1]["exit_code"] == 0
            assert os.read(stdout_read, 4096) == b"executor-output"
            assert os.read(stderr_read, 4096) == b""
            assert executor_id not in worker_exec._jobs
            assert not (worker_exec.HOME_ROOT / executor_id).exists()
        finally:
            for fd in (*executor_fds, stdout_read, stderr_read):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        direct = await run_direct(
            monkeypatch,
            "import os; os.write(1,b'direct stdout\\n'); os.write(2,b'direct stderr\\n')",
        )
        assert (direct.exit_code, direct.stdout, direct.stderr) == (
            0,
            "direct stdout\n",
            "direct stderr\n",
        )

        payload = b"0123456789abcdef" * (64 * 1024 + 1)
        large_client = WorkerClient(stdout=payload, stderr=payload)
        large_backend, large_handle = await launch_worker(monkeypatch, large_client)
        large_worker = await large_backend.wait(large_handle, 2)
        await large_backend.cleanup(large_handle)
        large_direct = await run_direct(
            monkeypatch,
            "import os; d=b'0123456789abcdef'*(64*1024+1); os.write(1,d); os.write(2,d)",
        )
        assert large_worker.stdout.encode() == payload
        assert large_worker.stderr.encode() == payload
        assert large_direct.stdout.encode() == payload
        assert large_direct.stderr.encode() == payload
        assert large_worker.exit_code == large_direct.exit_code == 0

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
            {"version": 1, "op": "cancel", "id": handle.identifier}
        ]
        assert socket_events == [
            "socket",
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
        with pytest.raises(RuntimeError, match="identity no longer matches"):
            await backend.cancel(mismatched)
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
        direct_first = await direct_backend.launch(
            direct_spec("import time; print('direct one',flush=True); time.sleep(30)")
        )
        direct_second = await direct_backend.launch(direct_spec("print('direct two')"))
        direct_second_result = await direct_backend.wait(direct_second, 2)
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
        for flag in ("false", None):
            if flag is None:
                monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
            else:
                monkeypatch.setenv("MIMIR_CODING_ENABLED", flag)
            backend = LocalSubprocessComputeBackend.for_authorized_checkout(
                ForbiddenAuthorization()
            )
            disabled = direct_spec(
                "import os; print(os.environ['PARITY_DIRECT'])"
            )
            object.__setattr__(disabled, "env", {"PARITY_DIRECT": "unchanged"})
            handle = await backend.launch(disabled)
            result = await backend.wait(handle, 2)
            await backend.cleanup(handle)
            assert result.stdout == "unchanged\n"
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
