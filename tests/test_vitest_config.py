from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_factory_checkouts_are_not_collected(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("node") is None or not (repo / "node_modules" / ".bin" / "vitest").exists():
        pytest.skip("node and installed node_modules/vitest are required for collection")

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
