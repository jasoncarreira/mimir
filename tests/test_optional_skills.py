"""Cross-skill checks for optional-skill deployment artifacts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mimir.access_control import agent_writable_roots, parse_declared_shell_commands
from mimir.pollers import _parse_poller_authority


_ROOT = Path(__file__).resolve().parents[1]
_OPTIONAL_SKILLS = _ROOT / "mimir" / "optional-skills"
_DECLARING_SKILLS = ("gmail-poller",)
_WRAPPER_SKILLS = ("social-cli", "gmail-poller")
_REQUIRES_DEPLOYMENT_BASH = pytest.mark.skipif(
    not Path("/usr/bin/bash").exists(),
    reason="requires deployment-image executable path /usr/bin/bash",
)


@pytest.mark.parametrize("skill_name,expected_names", [
    ("gmail-poller", {"gmail-inbox"}),
    ("social-cli", {"social-cli-notifications", "social-cli-feed"}),
])
def test_shipped_poller_manifests_omit_unusable_grants_and_options(
    skill_name: str, expected_names: set[str],
) -> None:
    manifest = json.loads(
        (_OPTIONAL_SKILLS / skill_name / "pollers.json").read_text(encoding="utf-8")
    )
    pollers = manifest["pollers"]
    assert {poller["name"] for poller in pollers} == expected_names
    for poller in pollers:
        assert not {"memory_store", "saga_feedback", "saga_mark_contributions"} & set(
            poller["authority"]["capabilities"]
        ), poller["name"]
        if skill_name == "social-cli":
            assert "shell_commands" not in poller["authority"]
            assert not {"shell_exec", "bash_jobs_list", "bash_job_output"} & set(
                poller["authority"]["capabilities"]
            )
            assert poller["authority"]["proposal_surface"] == "social-outbox"


def test_gmail_poller_grants_file_reads_and_only_gog_shell_command() -> None:
    manifest = json.loads(
        (_OPTIONAL_SKILLS / "gmail-poller" / "pollers.json").read_text(encoding="utf-8")
    )
    assert len(manifest["pollers"]) == 1
    authority = manifest["pollers"][0]["authority"]
    assert {"read_file", "ls", "glob", "grep"} <= set(authority["capabilities"])
    assert authority["scoped_roots"] == ["state"]
    assert authority["shell_commands"] == [
        {
            "exec": "bash",
            "path": "/usr/bin/bash",
            "script": "/mimir-home/skills/gmail-poller/scripts/run-gog.sh",
            "options": ["--account", "--max", "--json", "--no-input"],
        }
    ]


@pytest.mark.parametrize("skill_name", _WRAPPER_SKILLS)
def test_shipped_skill_guidance_has_no_backticked_shell_file_reads(
    skill_name: str,
) -> None:
    text = (_OPTIONAL_SKILLS / skill_name / "SKILL.md").read_text(encoding="utf-8")
    shell_read = re.compile(
        r"`(?:cat|head|tail|less|wc|ls)\b(?:\s+-[^\s`]+)*\s+[^-\s`][^\s`]*`"
    )
    assert shell_read.search(text) is None, skill_name


@_REQUIRES_DEPLOYMENT_BASH
@pytest.mark.parametrize("skill_name", _DECLARING_SKILLS)
def test_shipped_shell_commands_parse_in_installed_skill(
    skill_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse shipped declarations against the deployment's real write roots."""
    home = tmp_path / "mimir-home"
    installed = home / "skills" / skill_name
    shutil.copytree(_OPTIONAL_SKILLS / skill_name, installed)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    state_root = home / "state" / "pollers"
    state_root.mkdir(parents=True)

    manifest = json.loads((installed / "pollers.json").read_text(encoding="utf-8"))
    for poller in manifest["pollers"]:
        persist_dir = state_root / poller["name"]
        persist_dir.mkdir()
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
        principal = _parse_poller_authority(
            poller["authority"],
            name=poller["name"],
            persist_dir=persist_dir,
            state_root=state_root,
            manifest_path=installed / "pollers.json",
        )
        assert len(principal.declared_shell_commands) == len(declarations)


@pytest.mark.parametrize("skill_name", _WRAPPER_SKILLS)
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
