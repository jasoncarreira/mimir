from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import sys
from collections.abc import Sequence
from typing import BinaryIO


ACP_VERSION = "0.12.0"
INSTALL_INSTRUCTION = "Install ACP support with: pip install 'mimir-agent[acp]'"
USAGE_ERROR = "mimir acp accepts no arguments; authenticate in-band over JSON-RPC."
RUNTIME_ERROR = "ACP host failed."


def _write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _dependencies_available() -> bool:
    try:
        installed_version = importlib.metadata.version("agent-client-protocol")
        module_spec = importlib.util.find_spec("acp")
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError):
        return False
    return installed_version == ACP_VERSION and module_spec is not None


def _reserve_stdout() -> tuple[int, BinaryIO]:
    frame_fd = os.dup(1)
    try:
        os.set_inheritable(frame_fd, False)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        frame_file = os.fdopen(frame_fd, "wb", buffering=0)
    except BaseException:
        os.close(frame_fd)
        raise
    return frame_fd, frame_file


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(argv or ())
    try:
        _, frame_file = _reserve_stdout()
    except Exception:
        _write_stderr(RUNTIME_ERROR)
        return 1

    status = 1
    error_reported = False
    try:
        if not _dependencies_available():
            _write_stderr(INSTALL_INSTRUCTION)
            status = 2
        elif arguments:
            _write_stderr(USAGE_ERROR)
            status = 2
        else:
            try:
                host = importlib.import_module("mimir.acp.host")
            except (ImportError, ModuleNotFoundError):
                _write_stderr(INSTALL_INSTRUCTION)
                status = 2
            else:
                result = host.run(frame_file)
                status = 0 if result is None else int(result)
                if status not in (0, 1):
                    status = 1
    except (BrokenPipeError, ConnectionResetError):
        status = 0
    except BaseException:
        _write_stderr(RUNTIME_ERROR)
        error_reported = True
        status = 1

    close_failed = False
    try:
        frame_file.flush()
    except Exception:
        close_failed = True
    try:
        frame_file.close()
    except Exception:
        close_failed = True
    if close_failed:
        if status != 2:
            if not error_reported:
                _write_stderr(RUNTIME_ERROR)
            status = 1
    return status
