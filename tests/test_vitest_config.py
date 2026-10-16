from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_factory_checkouts_are_not_collected(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("node") is None or not (repo / "node_modules" / ".bin" / "vitest").exists():
        reason = "node and installed node_modules/vitest are required for collection"
        if os.environ.get("MIMIR_REQUIRE_VITEST") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    source = repo / "frontend" / "src" / "uiState.test.ts"
    fixture = tmp_path / "checkout"
    for relative in (
        "frontend/src/uiState.test.ts",
        ".factory/worktrees/x/y/frontend/src/uiState.test.ts",
        ".factory-sandboxes/z/frontend/src/uiState.test.ts",
    ):
        target = fixture / relative
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)

    def collected(root: Path) -> set[str]:
        # list --filesOnly does not load setupFiles, absent in the fixture root.
        result = subprocess.run(
            [str(repo / "node_modules" / ".bin" / "vitest"), "list", "--filesOnly",
             "--root", str(root), "--config", str(repo / "vitest.config.ts")],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return set(result.stdout.splitlines())

    files = collected(fixture)
    assert "frontend/src/uiState.test.ts" in files
    assert not any(".factory" in path for path in files), files

    repo_files = collected(repo)
    assert "frontend/src/uiState.test.ts" in repo_files
    assert not any(".factory" in path for path in repo_files), repo_files


@pytest.mark.parametrize("required", [False, True])
def test_missing_vitest_skips_locally_but_fails_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    if required:
        monkeypatch.setenv("MIMIR_REQUIRE_VITEST", "1")
    else:
        monkeypatch.delenv("MIMIR_REQUIRE_VITEST", raising=False)
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome, match="required for collection"):
        test_factory_checkouts_are_not_collected(tmp_path)


def test_frontend_ci_requires_collection_guard() -> None:
    import shlex

    import yaml

    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["frontend"]["steps"]
    guard = next(
        step for step in steps
        if step.get("name") == "Verify Vitest collection excludes factory checkouts"
    )
    install = next(step for step in steps if step.get("run") == "npm ci")
    uv = next(step for step in steps if step.get("uses", "").startswith("astral-sh/setup-uv@"))
    assert steps.index(install) < steps.index(guard)
    assert steps.index(uv) < steps.index(guard)
    assert "if" not in guard
    assert not guard.get("continue-on-error", False)
    assert guard["env"]["MIMIR_REQUIRE_VITEST"] == "1"
    assert shlex.split(guard["run"]) == [
        "uv", "run", "--extra", "dev", "pytest", "-q", "tests/test_vitest_config.py",
    ]
