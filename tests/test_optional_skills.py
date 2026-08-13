"""Cross-skill checks for optional-skill deployment artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mimir.access_control import agent_writable_roots, parse_declared_shell_commands


_ROOT = Path(__file__).resolve().parents[1]
_OPTIONAL_SKILLS = _ROOT / "mimir" / "optional-skills"
_DECLARING_SKILLS = ("social-cli", "gmail-poller")


@pytest.mark.parametrize("skill_name", _DECLARING_SKILLS)
def test_shipped_shell_commands_parse_in_installed_skill(
    skill_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse shipped declarations against the deployment's real write roots."""
    home = tmp_path / "mimir-home"
    installed = home / "skills" / skill_name
    shutil.copytree(_OPTIONAL_SKILLS / skill_name, installed)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    manifest = json.loads((installed / "pollers.json").read_text(encoding="utf-8"))
    for poller in manifest["pollers"]:
        declarations = poller["authority"]["shell_commands"]
        for declaration in declarations:
            script = declaration.get("script")
            if script:
                declaration["script"] = str(
                    installed / "scripts" / Path(script).name
                )
        parsed = parse_declared_shell_commands(
            declarations,
            writable_roots=agent_writable_roots(home),
        )
        assert len(parsed) == len(declarations)


@pytest.mark.parametrize("skill_name", _DECLARING_SKILLS)
def test_shell_wrappers_do_not_expose_interpreter_passthrough(skill_name: str) -> None:
    scripts = (_OPTIONAL_SKILLS / skill_name / "scripts").glob("run-*.sh")
    wrappers = list(scripts)
    assert len(wrappers) == 1
    text = wrappers[0].read_text(encoding="utf-8")
    assert "eval " not in text
    assert 'bash -c' not in text
    assert 'python3 -c' not in text
    assert 'python3 -m' not in text


@pytest.mark.parametrize(
    "skill_name,arguments",
    [
        ("social-cli", ["social-cli-feed", "count", "-c", "id"]),
        ("social-cli", ["social-cli-feed", "count", "-m", "module"]),
        ("gmail-poller", ["gmail", "messages", "search", "query", "-c", "id"]),
        ("gmail-poller", ["gmail", "messages", "search", "query", "-m", "module"]),
    ],
)
def test_shell_wrappers_refuse_code_passthrough_options(
    skill_name: str, arguments: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = next((_OPTIONAL_SKILLS / skill_name / "scripts").glob("run-*.sh"))
    monkeypatch.setenv("GOG_ACCOUNT", "agent@example.test")
    proc = subprocess.run(
        ["bash", str(wrapper), *arguments], capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "unsupported option" in proc.stderr
