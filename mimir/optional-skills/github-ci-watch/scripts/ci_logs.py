"""Bounded CI evidence capture; ships with standalone CI-watch and the package."""
from __future__ import annotations

import codecs
import os
import re
import subprocess
import tempfile
import unicodedata

LOG_EXCERPT_BYTES = 32 * 1024


def clean_log_tail(output, limit: int = LOG_EXCERPT_BYTES) -> bytes:
    """Strip controls before truncating, including strings spanning read chunks."""
    output.seek(0)
    tail = bytearray()
    state = "text"
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = output.read(64 * 1024)
        clean = []
        for char in decoder.decode(chunk, final=not chunk):
            byte = ord(char)
            if state == "string":
                if byte in (7, 0x9c):
                    state = "text"
                elif byte == 27:
                    state = "string_escape"
            elif state == "string_escape":
                if byte in (92, 7, 0x9c):
                    state = "text"
                elif byte != 27:
                    state = "string"
            elif state == "csi":
                if 0x40 <= byte <= 0x7e:
                    state = "text"
                elif byte == 27:
                    state = "escape"
            elif state == "escape":
                if byte == 91:
                    state = "csi"
                elif byte in (93, 80, 88, 94, 95):
                    state = "string"
                elif 0x20 <= byte <= 0x2f:
                    state = "escape_intermediate"
                else:
                    state = "escape" if byte == 27 else "text"
                    if char in "\t\n" or (byte >= 0x80 and unicodedata.category(char) not in {"Cc", "Cf"}):
                        clean.append(char)
            elif state == "escape_intermediate":
                if 0x30 <= byte <= 0x7e:
                    state = "text"
                elif byte == 27:
                    state = "escape"
            elif byte == 27:
                state = "escape"
            elif byte == 0x9b:
                state = "csi"
            elif byte in (0x90, 0x98, 0x9d, 0x9e, 0x9f):
                state = "string"
            elif char in "\t\n" or unicodedata.category(char) not in {"Cc", "Cf"}:
                clean.append(char)
        tail.extend("".join(clean).encode("utf-8"))
        del tail[:-limit]
        if not chunk:
            return bytes(tail).decode("utf-8", errors="ignore").encode("utf-8")


def capture_job_log(
    repo: str, job_id: int, *, token: str, timeout: float,
    limit: int = LOG_EXCERPT_BYTES,
) -> tuple[bytes, str]:
    """Capture with gh's authenticated redirect handling; expose no raw stderr.

    Callers must authorize and bind the repository/job before calling. Raw output
    is transiently spooled, never saved as evidence or included in an event.
    """
    if timeout <= 0:
        return b"", "HTTP status unavailable (poller time budget exhausted)"
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["gh", "api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"],
                stdout=output, stderr=subprocess.PIPE, timeout=timeout, env=env,
            )
            if result.returncode:
                stderr = result.stderr or b""
                if b"escape sequences" in stderr or b"--allow-escape-sequences" in stderr:
                    return b"", "HTTP status unavailable (gh escape-sequence refusal)"
                status = re.search(rb"HTTP\s+(\d{3})", stderr)
                return b"", (
                    f"HTTP {status[1].decode()}" if status
                    else "HTTP status unavailable (gh failed)"
                )
            excerpt = clean_log_tail(output, limit)
        return (excerpt, "") if excerpt else (b"", "HTTP status unavailable (empty log response)")
    except subprocess.TimeoutExpired:
        return b"", "HTTP status unavailable (log fetch timed out)"
    except OSError:
        return b"", "HTTP status unavailable (log fetch or state write failed)"
