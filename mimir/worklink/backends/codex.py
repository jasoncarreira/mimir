"""Codex CLI Worklink backend."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import signal
from typing import Any, Sequence

from .base import Caps, RawResult, WorkOrder


@dataclass(frozen=True)
class CodexBackend:
    """Adapter for ``codex exec`` in an isolated Worklink worktree."""

    bin: str = "codex"
    extra_args: Sequence[str] = field(default_factory=tuple)
    name: str = "codex"

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="coding-cli",
            persistent_sessions=False,
            json_output=True,
            native_pr_creation=False,
            worktree_safe=True,
            quota_pool="codex-subscription",
        )

    async def run(self, order: WorkOrder) -> RawResult:
        command = self._command(order)
        transcript_path = _transcript_path(order.transcript_root, order.issue_id)
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(order.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(order.worktree),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            _write_transcript(
                transcript_path,
                command=command,
                exit_code=None,
                status="backend_error",
                stdout="",
                stderr=str(exc),
                timed_out=False,
            )
            return RawResult(-1, transcript_path, "backend_error", str(exc))

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=order.timeout_s
            )
        except TimeoutError:
            timed_out = True
            await _kill_process_group(proc)
            stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1
        status = "timeout" if timed_out else _status_from_output(exit_code, stdout, stderr)
        error = _error_from_status(status, stdout, stderr)
        _write_transcript(
            transcript_path,
            command=command,
            exit_code=exit_code,
            status=status,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )
        return RawResult(exit_code, transcript_path, status, error)

    def _command(self, order: WorkOrder) -> list[str]:
        args = list(self.extra_args) or ["exec", "--json"]
        prompt = self._prompt(order)
        if args and args[0] == "exec":
            return [self.bin, "exec", "--cd", str(order.worktree), *args[1:], prompt]
        return [self.bin, *args, "--cd", str(order.worktree), prompt]

    @staticmethod
    def _prompt(order: WorkOrder) -> str:
        if not order.rules:
            return order.prompt
        return f"{order.rules.rstrip()}\n\n{order.prompt}"


def _transcript_path(transcript_root: Path | None, issue_id: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = transcript_root or _default_transcript_root()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"codex-{issue_id}-{stamp}.json"


def _default_transcript_root() -> Path:
    home = Path(os.environ.get("MIMIR_HOME", str(Path.home())))
    return home / "state" / "worklink" / "transcripts"


def _write_transcript(
    path: Path,
    *,
    command: Sequence[str],
    exit_code: int | None,
    status: str,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> None:
    payload: dict[str, Any] = {
        "backend": "codex",
        "command": list(command),
        "exit_code": exit_code,
        "backend_status": status,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await proc.wait()


def _status_from_output(exit_code: int, stdout: str, stderr: str) -> str:
    if exit_code == 0:
        return "success"
    combined = f"{stdout}\n{stderr}".lower()
    if "rate limit" in combined or "quota" in combined or "429" in combined:
        return "quota_exhausted"
    if re.search(r"\b(unauthorized|authentication|login)\b", combined):
        return "auth_error"
    return "failed"


def _error_from_status(status: str, stdout: str, stderr: str) -> str | None:
    if status == "success":
        return None
    detail = (stderr or stdout).strip()
    if status == "timeout":
        return "codex execution timed out" + (f": {detail}" if detail else "")
    return detail or status
