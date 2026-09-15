from __future__ import annotations

from pathlib import Path

import pytest

from mimir import access_control as ac


@pytest.mark.parametrize("relative", [".", "attachments/body", "unknown/file", "state/pollers/event"])
def test_home_helper_rejects_nonreference_paths(tmp_path: Path, relative: str) -> None:
    assert ac._home_reference_integrity(tmp_path, Path(relative)) == "untrusted"


@pytest.mark.parametrize("relative", ["skills", "skills/example/SKILL.md", "skills/example/script.py"])
def test_home_helper_trusts_skills_without_records(tmp_path: Path, relative: str) -> None:
    assert ac._home_reference_integrity(tmp_path, Path(relative)) == "trusted"
