from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "test_full_access_control.sh"


def _run_script(
    case_dir: Path,
    *,
    args: tuple[str, ...] = (),
    fail_call: int = 0,
    fail_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    log = case_dir / "uv.jsonl"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/bin/sh
exec "$TEST_PYTHON" - "$@" <<'PY'
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["TEST_UV_LOG"])
calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
call = len(calls) + 1
entry = {
    "argv": sys.argv[1:],
    "access_control": os.environ.get("MIMIR_ACCESS_CONTROL_ENFORCED"),
}
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(entry) + "\\n")
if call == int(os.environ["TEST_UV_FAIL_CALL"]):
    raise SystemExit(int(os.environ["TEST_UV_FAIL_CODE"]))
PY
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
            "MIMIR_ACCESS_CONTROL_ENFORCED": "inherited",
            "TEST_PYTHON": sys.executable,
            "TEST_UV_LOG": str(log),
            "TEST_UV_FAIL_CALL": str(fail_call),
            "TEST_UV_FAIL_CODE": str(fail_code),
        }
    )
    completed = subprocess.run(
        [str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return completed, calls


def test_full_access_control_script_is_argv_safe_and_propagates_each_suite_failure(
    tmp_path: Path,
) -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR

    completed, calls = _run_script(tmp_path / "success")
    assert completed.returncode == 0
    assert calls == [
        {
            "argv": ["run", "pytest", "-q"],
            "access_control": "inherited",
        },
        {
            "argv": ["run", "pytest", "-q", "--tb=short"],
            "access_control": "1",
        },
    ]

    rejected, calls = _run_script(
        tmp_path / "argument",
        args=("--tb=long; scripts/test_full_access_control.sh",),
    )
    assert rejected.returncode == 64
    assert rejected.stderr == "usage: scripts/test_full_access_control.sh\n"
    assert calls == []

    first_failed, calls = _run_script(
        tmp_path / "first-failure",
        fail_call=1,
        fail_code=23,
    )
    assert first_failed.returncode == 23
    assert calls == [
        {
            "argv": ["run", "pytest", "-q"],
            "access_control": "inherited",
        }
    ]

    second_failed, calls = _run_script(
        tmp_path / "second-failure",
        fail_call=2,
        fail_code=29,
    )
    assert second_failed.returncode == 29
    assert calls == [
        {
            "argv": ["run", "pytest", "-q"],
            "access_control": "inherited",
        },
        {
            "argv": ["run", "pytest", "-q", "--tb=short"],
            "access_control": "1",
        },
    ]
