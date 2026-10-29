"""Contract tests for scripts/pytest_legs.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pytest_legs.py"
_spec = importlib.util.spec_from_file_location("pytest_legs_under_test", SCRIPT)
legs = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = legs
_spec.loader.exec_module(legs)


class Recorder:
    def __init__(self, returncodes=()):
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self._returncodes = list(returncodes)

    def __call__(self, argv, *, cwd, env):
        self.calls.append((argv, cwd, env))
        code = self._returncodes.pop(0) if self._returncodes else 0
        return SimpleNamespace(returncode=code)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / "tests" / name).write_text("")
    return tmp_path


def run(repo: Path, args: list[str], recorder: Recorder, env=None) -> int:
    return legs.main(args, root=repo, base_env=env if env is not None else {}, runner=recorder)


def test_leg_argv_and_cwd(repo):
    recorder = Recorder()
    assert run(repo, ["core:tests/test_a.py,tests/test_b.py"], recorder) == 0
    ((argv, cwd, _env),) = recorder.calls
    assert argv == [sys.executable, "-m", "pytest", "-q", "--tb=short",
                    "tests/test_a.py", "tests/test_b.py"]
    assert cwd == repo


@pytest.mark.parametrize("selection,plugins,enforced", [
    ("core", None, None),
    ("cultivate", "cultivate", None),
    ("core-enforced", None, "1"),
    ("cultivate-enforced", "cultivate", "1"),
])
def test_selection_environment(repo, selection, plugins, enforced):
    recorder = Recorder()
    ambient = {"MIMIR_PLUGINS": "other", "MIMIR_ACCESS_CONTROL_ENFORCED": "0", "KEEP": "yes"}
    assert run(repo, [f"{selection}:tests/test_a.py"], recorder, ambient) == 0
    env = recorder.calls[0][2]
    assert env.get("MIMIR_PLUGINS") == plugins
    assert env.get("MIMIR_ACCESS_CONTROL_ENFORCED") == enforced
    assert env["KEEP"] == "yes"
    assert ambient["MIMIR_PLUGINS"] == "other"


def test_legs_run_serially_in_order_and_stop_at_first_failure(repo):
    recorder = Recorder([0, 3, 0])
    status = run(repo, ["core:tests/test_a.py", "cultivate:tests/test_b.py",
                        "core:tests/test_a.py"], recorder)
    assert status == 3
    assert [call[0][-1] for call in recorder.calls] == ["tests/test_a.py", "tests/test_b.py"]


def test_pytest_exit_five_is_a_failure(repo):
    assert run(repo, ["core:tests/test_a.py"], Recorder([5])) == 5


def test_signal_death_is_a_failure(repo):
    assert run(repo, ["core:tests/test_a.py"], Recorder([-9])) == 137


@pytest.mark.parametrize("args", [
    ["tests/test_a.py"],
    ["core"],
    ["core:"],
    ["core:tests/test_a.py,"],
    ["core:,tests/test_a.py"],
    ["bogus:tests/test_a.py"],
    ["skills:tests/test_a.py"],
    ["skills:"],
    ["core:--collect-only"],
    ["core:tests/missing.py"],
    ["core:../outside.py"],
    ["core:/etc/passwd"],
])
def test_malformed_leg_exits_64_before_anything_runs(repo, args):
    recorder = Recorder()
    assert run(repo, ["core:tests/test_a.py", *args], recorder) == 64
    assert recorder.calls == []


def test_a_bad_later_leg_prevents_earlier_legs_from_running(repo):
    recorder = Recorder()
    assert run(repo, ["core:tests/test_a.py", "bogus:tests/test_a.py"], recorder) == 64
    assert recorder.calls == []


def test_no_arguments_exits_64(repo):
    assert run(repo, [], Recorder()) == 64


def _skill_tree(repo: Path) -> None:
    for relative in (
        "mimir/optional-skills/one/tests",
        "mimir/optional-skills/two/tests",
        "mimir/optional-skills/empty/tests",
        "plugins/cultivate/mimir_cultivate/defaults/skills/three/tests",
    ):
        (repo / relative).mkdir(parents=True)
    for relative in (
        "mimir/optional-skills/one/tests/test_x.py",
        "mimir/optional-skills/two/tests/test_y.py",
        "plugins/cultivate/mimir_cultivate/defaults/skills/three/tests/test_z.py",
    ):
        (repo / relative).write_text("")
    (repo / "mimir/optional-skills/empty/tests/conftest.py").write_text("")


def test_skills_all_runs_one_process_per_test_directory_without_plugins(repo):
    _skill_tree(repo)
    recorder = Recorder()
    ambient = {"MIMIR_PLUGINS": "cultivate", "MIMIR_ACCESS_CONTROL_ENFORCED": "1"}
    assert run(repo, ["skills:all"], recorder, ambient) == 0
    assert [call[0][-1] for call in recorder.calls] == [
        "mimir/optional-skills/one/tests",
        "mimir/optional-skills/two/tests",
        "plugins/cultivate/mimir_cultivate/defaults/skills/three/tests",
    ]
    for _argv, cwd, env in recorder.calls:
        assert cwd == repo
        assert "MIMIR_PLUGINS" not in env and "MIMIR_ACCESS_CONTROL_ENFORCED" not in env


def test_skills_all_stops_at_the_first_failing_directory(repo):
    _skill_tree(repo)
    recorder = Recorder([0, 2])
    assert run(repo, ["skills:all"], recorder) == 2
    assert len(recorder.calls) == 2


def test_skills_all_fails_when_nothing_matches(repo, capsys):
    recorder = Recorder()
    assert run(repo, ["skills:all"], recorder) == 1
    assert recorder.calls == []
    assert "no optional-skills test directories" in capsys.readouterr().err


def test_script_exits_64_on_a_malformed_leg_end_to_end():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "nonsense"], capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 64
    assert "malformed leg" in completed.stderr
