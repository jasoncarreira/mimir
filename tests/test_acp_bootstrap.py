from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

from mimir.acp import bootstrap
from mimir.acp.stdio import (
    ProtocolStreams,
    _DrainProtocol,
    _ReservedFrameTransport,
)


ROOT = Path(__file__).resolve().parents[1]


def _run_script(source: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


def test_acp_package_import_is_dependency_safe() -> None:
    completed = _run_script(
        "import sys; import mimir.acp; "
        "assert 'acp' not in sys.modules; "
        "assert 'mimir.runtime' not in sys.modules; "
        "assert 'mimir.config' not in sys.modules"
    )
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_acp_dependency_is_optional_exact_and_does_not_change_mcp_ranges() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    optional = project["project"]["optional-dependencies"]
    dependency_group = project["dependency-groups"]
    assert optional["acp"] == ["agent-client-protocol==0.12.0"]
    assert "agent-client-protocol==0.12.0" not in project["project"]["dependencies"]
    assert optional["dev"].count("agent-client-protocol==0.12.0") == 1
    assert dependency_group["dev"].count("agent-client-protocol==0.12.0") == 1
    assert optional["mcp"] == ["mcp>=1.27"]
    assert optional["dev"].count("mcp>=1.27") == 1
    assert dependency_group["dev"].count("mcp>=1.27") == 1


def test_lock_is_consistent_and_contains_exact_acp_version() -> None:
    completed = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    acp_packages = [
        package
        for package in lock["package"]
        if package["name"] == "agent-client-protocol"
    ]
    assert len(acp_packages) == 1
    assert acp_packages[0]["version"] == "0.12.0"


def test_entrypoint_dispatches_acp_without_importing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.entrypoint as entrypoint

    fake_bootstrap = types.ModuleType("mimir.acp.bootstrap")
    setattr(fake_bootstrap, "main", lambda argv: 17)
    monkeypatch.setitem(sys.modules, "mimir.acp.bootstrap", fake_bootstrap)
    monkeypatch.delitem(sys.modules, "mimir.cli", raising=False)
    monkeypatch.setattr(sys, "argv", ["mimir", "acp", "extra"])
    assert entrypoint.main() == 17
    assert "mimir.cli" not in sys.modules


def test_entrypoint_delegates_non_acp_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.entrypoint as entrypoint

    calls: list[list[str]] = []
    fake_cli = types.ModuleType("mimir.cli")
    setattr(fake_cli, "main", lambda: calls.append(list(sys.argv)))
    monkeypatch.setitem(sys.modules, "mimir.cli", fake_cli)
    monkeypatch.setattr(sys, "argv", ["mimir", "setup", "--home", "/tmp/home"])
    assert entrypoint.main() is None
    assert calls == [["mimir", "setup", "--home", "/tmp/home"]]


def test_dependency_probe_checks_only_exact_acp_metadata_and_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_calls: list[str] = []
    module_calls: list[str] = []

    def fake_version(name: str) -> str:
        metadata_calls.append(name)
        return "0.12.0"

    def fake_find_spec(name: str) -> object:
        module_calls.append(name)
        return object()

    monkeypatch.setattr(bootstrap.importlib.metadata, "version", fake_version)
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", fake_find_spec)
    assert bootstrap._dependencies_available() is True
    assert metadata_calls == ["agent-client-protocol"]
    assert module_calls == ["acp"]
    monkeypatch.setattr(bootstrap.importlib.metadata, "version", lambda name: "0.12.1")
    assert bootstrap._dependencies_available() is False


def test_missing_dependency_emits_only_install_instruction() -> None:
    source = r'''
import importlib.abc
import importlib.metadata
import sys

real_version = importlib.metadata.version

def blocked_version(name):
    if name == "agent-client-protocol":
        raise importlib.metadata.PackageNotFoundError(name)
    return real_version(name)

class BlockAcp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "acp" or fullname.startswith("acp."):
            raise ModuleNotFoundError(fullname)
        return None

importlib.metadata.version = blocked_version
for name in tuple(sys.modules):
    if name == "acp" or name.startswith("acp."):
        del sys.modules[name]
sys.meta_path.insert(0, BlockAcp())
from mimir.acp.bootstrap import main
raise SystemExit(main([]))
'''
    completed = _run_script(source)
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == (
        b"Install ACP support with: pip install 'mimir-agent[acp]'\n"
    )


def test_arguments_are_rejected_after_dependency_check() -> None:
    completed = _run_script(
        "from mimir.acp.bootstrap import main; raise SystemExit(main(['secret']))"
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == (
        b"mimir acp accepts no arguments; authenticate in-band over JSON-RPC.\n"
    )


def test_host_import_error_is_sanitized_runtime_failure() -> None:
    source = r'''
import importlib.abc
import importlib.util
import sys

class Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise ImportError("internal startup detail")

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "mimir.acp.host":
            return importlib.util.spec_from_loader(fullname, Loader())
        return None

sys.meta_path.insert(0, Finder())
from mimir.acp.bootstrap import main
raise SystemExit(main([]))
'''
    completed = _run_script(source)
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"ACP host failed.\n"


def test_stdout_is_reserved_before_host_import_and_closed_after_run() -> None:
    source = r'''
import importlib.abc
import importlib.util
import os
import sys

class Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        print("import-print")
        os.write(1, b"import-fd\n")

        def run(frame_file):
            module.frame_fd = frame_file.fileno()
            assert os.get_inheritable(module.frame_fd) is False
            assert sys.stdout is sys.stderr
            print("runtime-print")
            os.write(1, b"runtime-fd\n")
            frame_file.write(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
            return 0

        module.run = run

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "mimir.acp.host":
            return importlib.util.spec_from_loader(fullname, Loader())
        return None

sys.meta_path.insert(0, Finder())
from mimir.acp.bootstrap import main
status = main([])
fd = sys.modules["mimir.acp.host"].frame_fd
try:
    os.fstat(fd)
except OSError:
    pass
else:
    raise AssertionError("reserved descriptor remained open")
raise SystemExit(status)
'''
    completed = _run_script(source)
    assert completed.returncode == 0
    assert completed.stdout == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
    assert completed.stderr == (
        b"import-print\nimport-fd\nruntime-print\nruntime-fd\n"
    )


def test_unexpected_host_failure_is_sanitized() -> None:
    source = r'''
import sys
import types
host = types.ModuleType("mimir.acp.host")
def run(frame_file):
    raise RuntimeError("credential material")
host.run = run
sys.modules["mimir.acp.host"] = host
from mimir.acp.bootstrap import main
raise SystemExit(main([]))
'''
    completed = _run_script(source)
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"ACP host failed.\n"


class _PartialFrameFile:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        count = min(2, len(data))
        self.data.extend(data[:count])
        return count

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_reserved_transport_completes_partial_writes_without_owning_file() -> None:
    async def exercise() -> None:
        frame_file = _PartialFrameFile()
        protocol = _DrainProtocol()
        transport = _ReservedFrameTransport(frame_file, protocol)
        transport.write(b"abcdef")
        transport.close()
        assert bytes(frame_file.data) == b"abcdef"
        assert frame_file.closed is False
        await protocol._get_close_waiter(object())

    asyncio.run(exercise())


class _ReadTransport(asyncio.ReadTransport):
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_protocol_streams_stop_intake_and_close_without_closing_duplicate() -> None:
    reader = asyncio.StreamReader()
    read_transport = _ReadTransport()
    frame_file = io.BytesIO()
    protocol = _DrainProtocol()
    response_transport = _ReservedFrameTransport(frame_file, protocol)
    writer = asyncio.StreamWriter(
        response_transport,
        protocol,
        None,
        asyncio.get_running_loop(),
    )
    streams = ProtocolStreams(
        request_reader=reader,
        response_writer=writer,
        stdin_read_transport=read_transport,
        _response_transport=response_transport,
    )
    streams.stop_request_intake()
    streams.stop_request_intake()
    assert read_transport.closed is True
    assert reader.at_eof() is True
    assert await streams.drain_response_writer() is True
    assert await streams.close_response_writer() is True
    assert frame_file.closed is False
    assert len(streams.writer_helper_tasks()) == 2


@pytest.mark.asyncio
async def test_drain_timeout_aborts_writer_and_marks_failure() -> None:
    reader = asyncio.StreamReader()
    read_transport = _ReadTransport()
    frame_file = io.BytesIO()
    protocol = _DrainProtocol()
    response_transport = _ReservedFrameTransport(frame_file, protocol)
    writer = asyncio.StreamWriter(
        response_transport,
        protocol,
        None,
        asyncio.get_running_loop(),
    )
    streams = ProtocolStreams(
        request_reader=reader,
        response_writer=writer,
        stdin_read_transport=read_transport,
        _response_transport=response_transport,
    )
    protocol.pause_writing()
    assert await streams.drain_response_writer(timeout=0) is False
    assert streams.writer_failed is True
    assert response_transport.is_closing() is True
    assert frame_file.closed is False
