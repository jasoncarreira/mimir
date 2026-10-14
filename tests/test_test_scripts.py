from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "test_full_access_control.sh"
CORE = ["run", "--locked", "--extra", "dev", "--extra", "bench", "pytest", "-q", "-n", "6"]
PLUGIN = ["run", "--locked", "--all-packages", "--extra", "dev", "--extra", "bench",
          "pytest", "-q", "-n", "6", "tests", "plugins/cultivate/tests"]


def _run_script(
    case_dir: Path, *, plugin: bool = False, args: tuple[str, ...] = (),
    fail_uv: int = 0, fail_npm: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    (case_dir / "package.json").write_text("{}", encoding="utf-8")
    (case_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    (case_dir / "mimir/optional-skills/existing/tests").mkdir(parents=True)
    if plugin:
        (case_dir / "plugins/cultivate/mimir_cultivate/defaults/optional-skills/moved/tests").mkdir(parents=True)
        (case_dir / "plugins/cultivate/pyproject.toml").write_text("", encoding="utf-8")
        (case_dir / "plugins/cultivate/tests").mkdir(exist_ok=True)

    # Both shims record argv/environment and exit immediately; the test never
    # executes nested pytest, npm ci, or the script itself recursively.
    for command in ("uv", "npm"):
        executable = fake_bin / command
        executable.write_text(
            """#!/bin/sh
exec "$TEST_PYTHON" - "$0" "$@" <<'PY'
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[1]).name
log = Path(os.environ["TEST_LOG"])
entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
call = 1 + sum(entry["command"] == name for entry in entries)
entry = {"command": name, "argv": sys.argv[2:],
         "access_control": os.environ.get("MIMIR_ACCESS_CONTROL_ENFORCED"),
         "plugins": os.environ.get("MIMIR_PLUGINS")}
with log.open("a") as stream:
    stream.write(json.dumps(entry) + "\\n")
if call == int(os.environ[f"TEST_FAIL_{name.upper()}"]):
    raise SystemExit(23)
PY
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    log = case_dir / "calls.jsonl"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
        "MIMIR_ACCESS_CONTROL_ENFORCED": "inherited",
        "MIMIR_PLUGINS": "inherited",
        "TEST_PYTHON": sys.executable,
        "TEST_LOG": str(log),
        "TEST_FAIL_UV": str(fail_uv),
        "TEST_FAIL_NPM": str(fail_npm),
    })
    completed = subprocess.run(
        [str(SCRIPT), *args], cwd=case_dir, env=env,
        capture_output=True, text=True, check=False, timeout=10,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return completed, calls


def test_full_access_control_matrix(tmp_path: Path) -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    completed, calls = _run_script(tmp_path / "success", plugin=True)
    assert completed.returncode == 0
    assert calls == [
        {"command": "uv", "argv": CORE, "access_control": None, "plugins": None},
        {"command": "uv", "argv": CORE, "access_control": "1", "plugins": None},
        {"command": "uv", "argv": PLUGIN, "access_control": None, "plugins": "cultivate"},
        {"command": "uv", "argv": PLUGIN, "access_control": "1", "plugins": "cultivate"},
        {"command": "uv", "argv": [*PLUGIN[:3], *CORE[2:], "mimir/optional-skills/existing/tests"],
         "access_control": None, "plugins": None},
        {"command": "uv", "argv": [*PLUGIN[:3], *CORE[2:], "plugins/cultivate/mimir_cultivate/defaults/optional-skills/moved/tests"],
         "access_control": None, "plugins": None},
        *({"command": "npm", "argv": argv, "access_control": "inherited", "plugins": "inherited"}
          for argv in (["ci"], ["run", "test"], ["run", "build"])),
    ]


def test_full_access_control_rejects_arguments(tmp_path: Path) -> None:
    rejected, calls = _run_script(tmp_path / "argument", args=("--tb=long; touch unsafe",))
    assert rejected.returncode == 64
    assert rejected.stderr == "usage: scripts/test_full_access_control.sh\n"
    assert calls == []


@pytest.mark.parametrize("fail_uv", range(1, 7))
def test_full_access_control_propagates_python_stage_failures(
    tmp_path: Path, fail_uv: int,
) -> None:
    failed, calls = _run_script(tmp_path / "failure", plugin=True, fail_uv=fail_uv)
    assert failed.returncode == 23
    assert len(calls) == fail_uv
    assert all(call["command"] == "uv" for call in calls)


@pytest.mark.parametrize("fail_npm", range(1, 4))
def test_full_access_control_propagates_frontend_stage_failures(
    tmp_path: Path, fail_npm: int,
) -> None:
    failed, calls = _run_script(tmp_path / "failure", plugin=True, fail_npm=fail_npm)
    assert failed.returncode == 23
    assert len([call for call in calls if call["command"] == "npm"]) == fail_npm
    assert len([call for call in calls if call["command"] == "uv"]) == 6


def test_full_access_control_without_optional_package(tmp_path: Path) -> None:
    completed, calls = _run_script(tmp_path / "core-only")
    assert completed.returncode == 0
    assert [call["argv"] for call in calls if call["command"] == "uv"] == [
        CORE, CORE, [*CORE, "mimir/optional-skills/existing/tests"],
    ]
