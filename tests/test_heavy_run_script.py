"""``scripts/heavy_run.sh`` queues a heavy command on the host's shared lock.

The factory's ``verify`` runs through it, so its two fallbacks matter as much as
the wrapped path: a host without the lock tools must still run the suite, and a
command already under the lock must not try to take it again (``lockf`` is not
re-entrant, so that would wait on its own parent forever). The real tools are
replaced with recording stubs, so these tests take no lock on the host.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "heavy_run.sh"


def _stub(path: Path, log: Path) -> Path:
    # Record argv and the lock marker, then run the wrapped command like the real tool.
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s\\n" "$(basename "$0") $*" "${{MIMIR_HEAVY_LOCK_HELD:-}}" >> "{log}"\n'
        'case "$(basename "$0")" in\n'
        '  gtimeout) shift 3; exec "$@" ;;\n'
        '  lockf) shift 2; exec "$@" ;;\n'
        "esac\n"
    )
    path.chmod(0o755)
    return path


def _run(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = tmp_path / "calls.log"
    lockf = _stub(tmp_path / "lockf", log)
    gtimeout = _stub(tmp_path / "gtimeout", log)
    full_env = {
        "PATH": os.environ["PATH"],
        "MIMIR_HEAVY_LOCKF": str(lockf),
        "MIMIR_HEAVY_GTIMEOUT": str(gtimeout),
        "MIMIR_HEAVY_LOCK_FILE": str(tmp_path / "heavy.lock"),
        **env,
    }
    proc = subprocess.run(
        ["sh", str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=30,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_wraps_command_in_timeout_then_lock(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "sh", "-c", 'echo "held=$MIMIR_HEAVY_LOCK_HELD"; exit 3')
    assert proc.returncode == 3
    assert proc.stdout.strip() == "held=1"
    lock = tmp_path / "heavy.lock"
    assert calls == [
        f"gtimeout -k 10s 7200s {tmp_path / 'lockf'} -k {lock} sh -c "
        'echo "held=$MIMIR_HEAVY_LOCK_HELD"; exit 3|1',
        f'lockf -k {lock} sh -c echo "held=$MIMIR_HEAVY_LOCK_HELD"; exit 3|1',
    ]


def test_timeout_is_configurable(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "true", MIMIR_HEAVY_LOCK_TIMEOUT="60")
    assert proc.returncode == 0
    assert calls[0].startswith("gtimeout -k 10s 60s ")


def test_runs_unwrapped_when_lock_already_held(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "sh", "-c", "echo ran", MIMIR_HEAVY_LOCK_HELD="1")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ran"
    assert calls == []


@pytest.mark.parametrize("missing", ["MIMIR_HEAVY_LOCKF", "MIMIR_HEAVY_GTIMEOUT"])
def test_runs_unwrapped_when_tools_are_missing(tmp_path: Path, missing: str) -> None:
    proc, calls = _run(
        tmp_path, "sh", "-c", 'echo "held=${MIMIR_HEAVY_LOCK_HELD:-}"',
        **{missing: str(tmp_path / "absent-tool")},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "held="
    assert calls == []


def test_requires_a_command(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path)
    assert proc.returncode == 64
    assert "usage" in proc.stderr
    assert calls == []


_REAL_LOCKF = "/usr/bin/lockf"
_REAL_GTIMEOUT = shutil.which("gtimeout") or "/opt/homebrew/bin/gtimeout"
_real_tools = pytest.mark.skipif(
    not (os.access(_REAL_LOCKF, os.X_OK) and os.access(_REAL_GTIMEOUT, os.X_OK)),
    reason="host lockf/gtimeout not available",
)


def _real_env(lock: Path, **extra: str) -> dict[str, str]:
    # A private lock file: these tests never touch the host's shared lock.
    env = {k: v for k, v in os.environ.items() if k != "MIMIR_HEAVY_LOCK_HELD"}
    env.update(
        MIMIR_HEAVY_LOCKF=_REAL_LOCKF, MIMIR_HEAVY_GTIMEOUT=_REAL_GTIMEOUT,
        MIMIR_HEAVY_LOCK_FILE=str(lock), **extra,
    )
    return env


def _wait_for(path: Path, deadline: float = 10.0) -> None:
    end = time.monotonic() + deadline
    while not path.exists():
        assert time.monotonic() < end, f"{path} never appeared"
        time.sleep(0.05)


@_real_tools
def test_real_lock_serializes_a_second_wrapper(tmp_path: Path) -> None:
    lock, release, entered = tmp_path / "heavy.lock", tmp_path / "release", tmp_path / "second-entered"
    first = subprocess.Popen(
        ["sh", str(SCRIPT), "sh", "-c", f'touch "{tmp_path}/first-held"; while [ ! -e "{release}" ]; do sleep 0.05; done'],
        env=_real_env(lock),
    )
    try:
        _wait_for(tmp_path / "first-held")
        second = subprocess.Popen(["sh", str(SCRIPT), "touch", str(entered)], env=_real_env(lock))
        time.sleep(1.0)
        assert not entered.exists(), "second wrapper ran its payload while the lock was held"
        release.touch()
        assert first.wait(timeout=20) == 0
        assert second.wait(timeout=20) == 0
        assert entered.exists()
    finally:
        release.touch()
        first.kill()


@_real_tools
def test_real_timeout_releases_the_lock(tmp_path: Path) -> None:
    lock, entered = tmp_path / "heavy.lock", tmp_path / "after-timeout"
    holder = subprocess.run(
        ["sh", str(SCRIPT), "sleep", "30"], env=_real_env(lock, MIMIR_HEAVY_LOCK_TIMEOUT="1"),
        timeout=30,
    )
    assert holder.returncode == 124  # gtimeout's timed-out status
    after = subprocess.run(
        ["sh", str(SCRIPT), "touch", str(entered)], env=_real_env(lock, MIMIR_HEAVY_LOCK_TIMEOUT="10"),
        timeout=30,
    )
    assert after.returncode == 0 and entered.exists()
