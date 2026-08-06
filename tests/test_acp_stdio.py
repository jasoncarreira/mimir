from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
BANNER_PREFIX = b"acp-test-banner-before-exec\n"
FRAME = b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
READ_SIZE = 4096
OUTPUT_CAP = 1024 * 1024
TIMEOUT = 10.0


def _child_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    mimir_home = tmp_path / "mimir-home"
    home.mkdir()
    mimir_home.mkdir()
    removed = {
        "SAGA_CONFIG",
        "SAGA_DATA_DIR",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("MIMIR_") and name not in removed
    }
    environment.update(
        HOME=str(home),
        MIMIR_HOME=str(mimir_home),
        MIMIR_MODEL_SPEC="anthropic:test",
        PYTHONUNBUFFERED="1",
    )
    return environment


def _entrypoint_command(entrypoint: str) -> list[str]:
    if entrypoint == "module":
        return [sys.executable, "-m", "mimir.acp"]
    executable = Path(sys.executable).parent / "mimir"
    assert executable.is_file(), f"console entrypoint is missing: {executable}"
    return [str(executable), "acp"]


def _diagnostics(stdout: bytearray, stderr: bytearray) -> str:
    return f"\nstdout={bytes(stdout)!r}\nstderr={bytes(stderr)!r}"


def _bounded_diagnostic_drain(
    streams: dict[Any, tuple[str, bytearray]],
) -> None:
    for stream, (_, output) in streams.items():
        while len(output) < OUTPUT_CAP:
            try:
                chunk = os.read(
                    stream.fileno(),
                    min(READ_SIZE, OUTPUT_CAP - len(output)),
                )
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            output.extend(chunk)


def _run_acp_child(
    command: Sequence[str],
    proposal: int,
    environment: dict[str, str],
) -> tuple[bytes, bytes]:
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": proposal},
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    process = subprocess.Popen(
        list(command),
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    streams = {
        process.stdout: ("stdout", stdout),
        process.stderr: ("stderr", stderr),
    }
    selector = selectors.DefaultSelector()
    failure: Exception | None = None
    returncode: int | None = None
    deadline = time.monotonic() + TIMEOUT
    try:
        process.stdin.write(request)
        process.stdin.flush()
        process.stdin.close()
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ACP child did not close stdout and stderr")
            ready = selector.select(remaining)
            if not ready:
                raise TimeoutError("ACP child did not close stdout and stderr")
            for key, _ in ready:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), READ_SIZE)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                name, output = streams[stream]
                if len(output) + len(chunk) > OUTPUT_CAP:
                    output.extend(chunk[: OUTPUT_CAP - len(output)])
                    raise AssertionError(f"ACP child {name} exceeded {OUTPUT_CAP} bytes")
                output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ACP child did not exit")
        returncode = process.wait(timeout=remaining)
    except Exception as exc:
        failure = exc
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            if failure is None:
                failure = exc
        _bounded_diagnostic_drain(streams)
        selector.close()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    if failure is not None:
        raise AssertionError(
            f"ACP child failed: {failure!r}{_diagnostics(stdout, stderr)}"
        ) from failure
    assert returncode == 0, (
        f"ACP child exited with {returncode}{_diagnostics(stdout, stderr)}"
    )
    return bytes(stdout), bytes(stderr)


def _parse_jsonl(output: bytes, stderr: bytes) -> list[dict[str, Any]]:
    diagnostics = f"\nstdout={output!r}\nstderr={stderr!r}"
    assert output, f"ACP child produced no frames{diagnostics}"
    assert output.endswith(b"\n"), f"ACP child left a partial frame{diagnostics}"
    records = output[:-1].split(b"\n")
    assert all(records), f"ACP child produced a blank frame{diagnostics}"
    frames: list[dict[str, Any]] = []
    for record in records:
        try:
            text = record.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"ACP frame is not UTF-8{diagnostics}") from exc
        try:
            frame = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"ACP frame is not valid JSON{diagnostics}") from exc
        assert isinstance(frame, dict), f"ACP frame is not an object{diagnostics}"
        frames.append(frame)
    return frames


def _assert_initialize_response(output: bytes, stderr: bytes) -> None:
    frames = _parse_jsonl(output, stderr)
    diagnostics = f"\nstdout={output!r}\nstderr={stderr!r}"
    assert len(frames) == 1, (
        f"expected one initialize response, got {len(frames)}{diagnostics}"
    )
    response = frames[0]
    assert response.get("jsonrpc") == "2.0", diagnostics
    assert response.get("id") == 1, diagnostics
    assert "error" not in response, diagnostics
    result = response.get("result")
    assert isinstance(result, dict), diagnostics
    assert result.get("protocolVersion") == 1, diagnostics


@pytest.mark.parametrize(
    ("entrypoint", "proposal"),
    [
        pytest.param("module", 1, id="module-v1"),
        pytest.param("module", 2, id="module-v2"),
        pytest.param("console", 1, id="console-v1"),
        pytest.param("console", 2, id="console-v2"),
    ],
)
def test_initialize_over_real_stdio(
    entrypoint: str,
    proposal: int,
    tmp_path: Path,
) -> None:
    stdout, stderr = _run_acp_child(
        _entrypoint_command(entrypoint),
        proposal,
        _child_environment(tmp_path),
    )
    _assert_initialize_response(stdout, stderr)


def _synthetic_host_source(contamination: str) -> str:
    return f'''
import importlib.abc
import importlib.util
import os
import sys

class Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        {contamination}

        def run(frame_file):
            frame_file.write({FRAME!r})
            return 0

        module.run = run

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "mimir.acp.host":
            return importlib.util.spec_from_loader(fullname, Loader())
        return None

sys.meta_path.insert(0, Finder())
from mimir.acp.bootstrap import main
raise SystemExit(main([]))
'''


def _run_synthetic_host(
    source: str,
    tmp_path: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=_child_environment(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


def test_print_after_stdout_reservation_is_redirected_to_stderr(
    tmp_path: Path,
) -> None:
    completed = _run_synthetic_host(
        _synthetic_host_source('print("garbage")'),
        tmp_path,
    )
    assert completed.returncode == 0
    assert _parse_jsonl(completed.stdout, completed.stderr) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}}
    ]
    assert completed.stdout == FRAME
    assert completed.stderr == b"garbage\n"


def test_os_write_to_fd_1_after_reservation_is_redirected_to_stderr(
    tmp_path: Path,
) -> None:
    completed = _run_synthetic_host(
        _synthetic_host_source('os.write(1, b"garbage\\n")'),
        tmp_path,
    )
    assert completed.returncode == 0
    assert _parse_jsonl(completed.stdout, completed.stderr) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}}
    ]
    assert completed.stdout == FRAME
    assert completed.stderr == b"garbage\n"


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_preprocess_banner_boundary(entrypoint: str, tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(ROOT / "tests" / "fixtures" / "acp_banner.py"),
        *_entrypoint_command(entrypoint),
    ]
    stdout, stderr = _run_acp_child(command, 1, _child_environment(tmp_path))
    assert stdout.startswith(BANNER_PREFIX), (
        f"banner prefix mismatch\nstdout={stdout!r}\nstderr={stderr!r}"
    )
    remainder = stdout.removeprefix(BANNER_PREFIX)
    _assert_initialize_response(remainder, stderr)
