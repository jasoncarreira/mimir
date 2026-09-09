"""Bounded, redacted CI evidence capture shared by tools and pollers."""
from __future__ import annotations

import codecs
import os
import re
import subprocess
import tempfile
import unicodedata
from collections import deque

from mimir.redaction import redact_text

LOG_EXCERPT_BYTES = 32 * 1024
TRUNCATION_MARKER = b"\n[truncated]\n"
_CHUNK_BYTES = 64 * 1024


def _clean_chunks(output):
    """Strip controls with constant memory, including across chunk boundaries."""
    output.seek(0)
    state = "text"
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = output.read(_CHUNK_BYTES)
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
        yield "".join(clean).encode("utf-8")
        if not chunk:
            return


def clean_log_tail(output, limit: int = LOG_EXCERPT_BYTES) -> bytes:
    """Control-only sanitizer retained for existing metadata consumers."""
    if limit <= 0:
        return b""
    tail = bytearray()
    for chunk in _clean_chunks(output):
        tail.extend(chunk)
        del tail[:-limit]
    return bytes(tail).decode("utf-8", errors="ignore").encode("utf-8")


def _select_excerpt(output, limit: int, token: str) -> bytes:
    # Scan bounded physical lines on disk. Never split an oversized line into
    # apparently independent credentials: omit it rather than leak a secret tail.
    with tempfile.TemporaryFile() as clean:
        for chunk in _clean_chunks(output):
            clean.write(chunk)
        size = clean.tell()
        clean.seek(0)
        recent = deque(maxlen=8)
        summary = None
        fallback = None
        following = 0
        in_summary = False
        oversized = False
        while True:
            start = clean.tell()
            line = clean.readline(_CHUNK_BYTES + 1)
            if not line:
                break
            if len(line) > _CHUNK_BYTES or oversized:
                oversized = not line.endswith(b"\n")
                recent.clear()
                continue
            end = clean.tell()
            if b"short test summary info" in line:
                summary = (start, end)
                in_summary = True
            elif in_summary:
                failure = re.search(rb"\b(?:FAILED|ERROR)\b", line)
                counts = re.search(
                    rb"\b\d+ (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b",
                    line,
                )
                if counts or failure:
                    summary = (summary[0], end)
                else:
                    in_summary = False
                if counts and not failure:
                    in_summary = False
            if re.search(rb"\b(?:FAILED|ERROR|Error|Exception|Traceback|fatal)\b", line):
                fallback = (recent[0] if recent else start, end)
                following = 8
            elif following:
                fallback = (fallback[0], end)
                following -= 1
            recent.append(start)
        start, end = summary or fallback or (0, size)
        clean.seek(start)
        selected = bytearray()
        omitted = start > 0 or end < size
        while clean.tell() < end:
            line = clean.readline(_CHUNK_BYTES + 1)
            if len(line) > _CHUNK_BYTES or len(selected) + len(line) > LOG_EXCERPT_BYTES:
                omitted = True
                break
            selected.extend(line)
        text = selected.decode("utf-8", errors="replace")
        if token:
            text = text.replace(token, "[REDACTED]")
        data = redact_text(text).encode("utf-8")
        if omitted or len(data) > limit:
            budget = limit - len(TRUNCATION_MARKER)
            data = data[:budget].decode("utf-8", errors="ignore").encode("utf-8")
            data += TRUNCATION_MARKER
        return data


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
    if (
        not isinstance(repo, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) is None
        or repo.split("/")[-1] in {".", ".."}
        or type(job_id) is not int or job_id <= 0
    ):
        return b"", "HTTP status unavailable (invalid repository or job ID)"
    if type(limit) is not int or limit < len(TRUNCATION_MARKER):
        return b"", "HTTP status unavailable (invalid excerpt limit)"
    limit = min(limit, LOG_EXCERPT_BYTES)
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
            result = subprocess.run(
                ["gh", "api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"],
                stdout=output, stderr=diagnostic, timeout=timeout, env=env,
            )
            if result.returncode:
                diagnostic.seek(0)
                stderr = diagnostic.read(_CHUNK_BYTES)
                if b"escape sequences" in stderr or b"--allow-escape-sequences" in stderr:
                    return b"", "HTTP status unavailable (gh escape-sequence refusal)"
                status = re.search(rb"HTTP\s+(\d{3})", stderr)
                if (
                    status and status[1] == b"401"
                    or b"gh auth login" in stderr.lower()
                    or b"gh_token" in stderr.lower()
                ):
                    label = f"HTTP {status[1].decode()}" if status else "HTTP status unavailable"
                    return b"", f"{label} (unauthenticated gh)"
                return b"", (
                    f"HTTP {status[1].decode()}" if status
                    else "HTTP status unavailable (gh failed)"
                )
            excerpt = _select_excerpt(output, limit, token)
        return (excerpt, "") if excerpt else (b"", "HTTP status unavailable (empty log response)")
    except subprocess.TimeoutExpired:
        return b"", "HTTP status unavailable (log fetch timed out)"
    except OSError:
        return b"", "HTTP status unavailable (log fetch or state write failed)"
