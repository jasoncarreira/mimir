from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class FactoryRecoveryIdentity:
    signature: str
    occurrence_id: str
    run_id: str
    attempt: int
    session: str


@dataclass(frozen=True)
class DetachedWorklinkProcess:
    pid: int
    log_path: Path


def launch_detached_worklink(
    *,
    command: str,
    issue_id: int,
    home: Path,
    repo: str | Path,
    state_dir: Path,
    run_bin: Sequence[str],
    recovery: FactoryRecoveryIdentity | None = None,
    env_overrides: Mapping[str, str] | None = None,
    popen: Callable[..., object] = subprocess.Popen,
    argv: Sequence[str] | None = None,
    log_name: str | None = None,
) -> DetachedWorklinkProcess:
    """Launch one autonomous Worklink child and return without waiting."""
    child_argv = list(argv) if argv is not None else [
        *run_bin, "worklink", command, str(issue_id),
        "--home", str(home), "--repo", str(repo), "--autonomous",
    ]
    if recovery is not None:
        if command != "run-epic":
            raise ValueError("factory recovery identity requires run-epic")
        child_argv.extend([
            "--expected-signature", recovery.signature,
            "--expected-occurrence", recovery.occurrence_id,
            "--expected-run-id", recovery.run_id,
            "--expected-attempt", str(recovery.attempt),
            "--expected-session", recovery.session,
        ])
    log_path = state_dir / (log_name or f"{command}-{issue_id}.log")
    try:
        log_fh = log_path.open("ab")
    except OSError:
        log_fh = subprocess.DEVNULL
    try:
        process = popen(
            child_argv,
            cwd=str(repo),
            env={
                **os.environ,
                **(env_overrides or {}),
                "WORKLINK_RUN_LOG": str(log_path),
            },
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        if log_fh not in (subprocess.DEVNULL, None):
            try:
                log_fh.close()
            except OSError:
                pass
    return DetachedWorklinkProcess(pid=int(getattr(process, "pid", 0) or 0), log_path=log_path)
