"""Secret-safe ACP diagnostics shared by the CLI and shutdown watchdog.

This module must remain stdlib-only; the proxy must not import its CLI.
"""
from __future__ import annotations

import os


def failure_origin(error: BaseException) -> str:
    site = None
    frame = error.__traceback__
    while frame is not None:
        site = f"{os.path.basename(frame.tb_frame.f_code.co_filename)}:{frame.tb_lineno}"
        frame = frame.tb_next
    return f"{type(error).__name__} at {site or 'unknown'}"


def failure_detail(error: BaseException) -> bytes:
    return f"detail: {failure_origin(error)}\nerror: acp-failed\n".encode()
