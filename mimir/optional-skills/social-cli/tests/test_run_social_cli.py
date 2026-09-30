from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

import yaml


SKILL = Path(__file__).resolve().parent.parent


def _wrapper(tmp_path: Path) -> tuple[Path, Path]:
    """Substitute only the absolute upstream binary path in a test copy."""
    fake = tmp_path / "social-cli-upstream"
    fake.write_text("#!/bin/sh\n[ \"$1\" != count ] || exit 99\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    scripts = tmp_path / "skill/scripts"
    scripts.mkdir(parents=True)
    (scripts / "count.py").write_text((SKILL / "scripts/count.py").read_text())
    wrapper = scripts / "run-social-cli.sh"
    wrapper.write_text(
        (SKILL / "scripts/run-social-cli.sh").read_text().replace(
            "/usr/local/bin/social-cli", str(fake)
        )
    )
    return wrapper, fake


def _run(wrapper: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(wrapper), "social-cli-notifications", *args],
        capture_output=True, text=True,
        env={**os.environ, "MIMIR_HOME": str(home)},
    )


def test_count_bypasses_upstream_binary(tmp_path):
    wrapper, _ = _wrapper(tmp_path)
    poller = tmp_path / "state/pollers/social-cli-notifications"
    poller.mkdir(parents=True)
    (poller / "sent_ledger-bsky.yaml").write_text(yaml.safe_dump([
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T12:00:00Z"},
    ]))
    result = _run(wrapper, tmp_path, "count", "--platform", "bsky", "--action", "post", "--since", "2026-06-28")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n"


def test_dispatch_execs_upstream_with_original_vetting(tmp_path):
    wrapper, _ = _wrapper(tmp_path)
    (tmp_path / "state/pollers/social-cli-notifications").mkdir(parents=True)
    result = _run(wrapper, tmp_path, "dispatch", "--platform", "bsky")
    assert result.returncode == 0
    assert result.stdout == "dispatch\n--platform\nbsky\n"
    for args in [("dispatch", "--dry-run"), ("dispatch", "--platform")]:
        denied = _run(wrapper, tmp_path, *args)
        assert denied.returncode == 2
        assert denied.stdout == ""


def test_count_ignores_environment_helper_override(tmp_path, monkeypatch):
    wrapper, _ = _wrapper(tmp_path)
    (tmp_path / "state/pollers/social-cli-notifications").mkdir(parents=True)
    alternate = tmp_path / "alternate-count.py"
    alternate.write_text("print(99)\n")
    monkeypatch.setenv("SOCIAL_CLI_COUNT_HELPER", str(alternate))
    result = _run(wrapper, tmp_path, "count", "--platform", "bsky")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0\n"


def test_count_helper_is_only_beside_resolved_wrapper(tmp_path, monkeypatch):
    wrapper, _ = _wrapper(tmp_path)
    (tmp_path / "state/pollers/social-cli-notifications").mkdir(parents=True)
    alternate = tmp_path / "alternate-count.py"
    alternate.write_text("print(99)\n")
    monkeypatch.setenv("SOCIAL_CLI_COUNT_HELPER", str(alternate))
    link = tmp_path / "wrapper-link.sh"
    link.symlink_to(wrapper)
    result = _run(link, tmp_path, "count", "--platform", "bsky")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0\n"
    (wrapper.parent / "count.py").unlink()
    result = _run(wrapper, tmp_path, "count", "--platform", "bsky")
    assert result.returncode == 127
    assert result.stdout == ""
    source = (SKILL / "scripts/run-social-cli.sh").read_text()
    assert "SOCIAL_CLI_COUNT_HELPER" not in source
    assert "/workspace/mimir" not in source
    assert ".mimir_builtin_skills" not in source


def test_credentials_and_debugging_are_operator_only():
    text = (SKILL / "SKILL.md").read_text()
    heading = ""
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and (line.startswith("# ") or line.startswith("## ") or line.startswith("### ")):
            heading = line
        if ".env" in line:
            assert "operator-only" in heading.lower(), (heading, line)
    assert "**Count works:**" not in text
    assert "**Sync works** (operator)" in text
    assert "never read `.env`" in text
    assert "`read_file` it and use" in text and "`edit_file` to add entries" in text
    assert "`write_file` only" in text
    assert "counts once per published post" in text
    assert "Operator-only: from `docker exec`, `--dry-run`" in text


def test_service_read_tools_are_admitted_for_both_pollers():
    pollers = json.loads((SKILL / "pollers.json").read_text())["pollers"]
    for poller in pollers:
        capabilities = set(poller["authority"]["capabilities"])
        assert {"read_file", "grep", "glob"} <= capabilities
        assert not {"memory_store", "saga_feedback", "saga_mark_contributions"} & capabilities
        assert all("--dry-run" not in cmd.get("options", [])
                   for cmd in poller["authority"]["shell_commands"])
