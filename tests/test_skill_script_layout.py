"""Conformance guards for executable code shipped by skills."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOTS = (
    REPO_ROOT / "mimir" / "skills",
    REPO_ROOT / "mimir" / "optional-skills",
)
EXECUTABLE_SUFFIXES = {".py", ".sh", ".js", ".ts"}


def _skill_dirs() -> list[Path]:
    return sorted(
        skill_dir
        for root in SKILL_ROOTS
        for skill_dir in root.iterdir()
        if skill_dir.is_dir()
    )


def _poller_manifests() -> list[Path]:
    return [path for skill_dir in _skill_dirs() if (path := skill_dir / "pollers.json").is_file()]


def _manifest_script(command: str) -> Path:
    scripts = [Path(token) for token in shlex.split(command) if Path(token).suffix in EXECUTABLE_SUFFIXES]
    assert len(scripts) == 1, f"poller command must name exactly one script: {command!r}"
    assert not scripts[0].is_absolute(), f"poller script must be relative to its skill: {command!r}"
    return scripts[0]


@pytest.mark.parametrize("manifest", _poller_manifests(), ids=lambda path: path.parent.name)
def test_poller_manifest_commands_resolve(manifest: Path) -> None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pollers = data.get("pollers") or []
    assert pollers, f"{manifest}: no pollers declared"
    for poller in pollers:
        script = _manifest_script(poller["command"])
        assert (manifest.parent / script).is_file(), (
            f"{manifest}: poller {poller.get('name')!r} points at missing script {script}"
        )


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda path: path.name)
def test_skill_root_has_no_executable_code(skill_dir: Path) -> None:
    root_scripts = sorted(
        path.name
        for path in skill_dir.iterdir()
        if path.is_file()
        and path.suffix in EXECUTABLE_SUFFIXES
        and path.name != "__init__.py"
    )
    assert not root_scripts, (
        f"{skill_dir}: executable code must live under scripts/: {root_scripts}"
    )


def test_skill_scripts_resolve_from_installed_package() -> None:
    package_root = files("mimir")
    source_root = REPO_ROOT / "mimir"
    scripts = sorted(
        path
        for root in SKILL_ROOTS
        for path in root.glob("*/scripts/**/*")
        if path.is_file() and path.suffix in EXECUTABLE_SUFFIXES
    )
    assert scripts
    missing = [
        str(path.relative_to(source_root))
        for path in scripts
        if not package_root.joinpath(*path.relative_to(source_root).parts).is_file()
    ]
    assert not missing, f"skill scripts missing from installed package: {missing}"
