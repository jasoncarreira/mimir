from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from mimir.access_control import (
    agent_writable_roots,
    parse_declared_shell_commands,
    parse_service_shell_argv,
)
from mimir.skill_defs import refresh_builtin_skills
from mimir.worklink.tool_pins import OPENCODE_VERSION, default_tool_pins

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mimir/skills/tool-pin-drift/scripts/check_tool_pins.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("tool_pin_drift_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_targets_are_data_driven_and_match_authoritative_pins() -> None:
    module = _load_script()
    targets = {target["name"]: target for target in module.load_targets()}
    pins = {pin.name: pin for pin in default_tool_pins()}

    assert set(targets) == set(pins)
    for name, pin in pins.items():
        assert targets[name]["pinned_version"] == pin.pin
        assert targets[name]["source"] == pin.source
        assert targets[name].get("package") == pin.package
        assert targets[name].get("repo") == pin.repo


def test_recorded_payload_parsing_and_drift_comparison() -> None:
    module = _load_script()
    npm = {
        "name": "opencode",
        "source": "npm",
        "package": "opencode-ai",
        "pinned_version": OPENCODE_VERSION,
    }
    github = {
        "name": "chainlink",
        "source": "github-release",
        "repo": "dollspace-gay/chainlink",
        "pinned_version": "chainlink-1.6.0",
    }

    npm_latest = module.parse_latest_version(npm, f"{OPENCODE_VERSION}\n")
    gh_latest = module.parse_latest_version(
        github,
        json.dumps({"tagName": "chainlink-1.7.0", "url": "https://example.test/release"}),
    )

    assert module.classify(npm, npm_latest)["status"] == "no_drift"
    assert module.classify(npm, npm_latest)["drifted"] is False
    assert module.classify(github, gh_latest) == {
        "name": "chainlink",
        "source": "github-release",
        "pinned_version": "chainlink-1.6.0",
        "latest_version": "chainlink-1.7.0",
        "drifted": True,
        "status": "drift",
        "error": None,
    }


def test_lookup_failure_is_recorded_and_remaining_targets_continue() -> None:
    module = _load_script()
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "npm":
            return subprocess.CompletedProcess(argv, 1, "", "registry unavailable")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"tagName": "v2.4.0", "url": "https://example.test"}), "",
        )

    targets = [
        {"name": "opencode", "source": "npm", "package": "opencode-ai", "pinned_version": OPENCODE_VERSION},
        {"name": "osv-scanner", "source": "github-release", "repo": "google/osv-scanner", "pinned_version": "v2.4.0"},
    ]
    results = module.check_targets(targets, runner=runner)

    assert [result["status"] for result in results] == ["error", "no_drift"]
    assert results[0]["error"] == "registry unavailable"
    assert results[0]["latest_version"] is None
    assert results[0]["drifted"] is None
    assert calls == [
        ["npm", "view", "opencode-ai", "version"],
        ["gh", "release", "view", "--repo", "google/osv-scanner", "--json", "tagName,url"],
    ]


def test_seeded_script_is_declarable_outside_agent_writable_roots(tmp_path: Path) -> None:
    refresh_builtin_skills(tmp_path)
    script = (
        tmp_path / ".mimir_builtin_skills/tool-pin-drift/scripts/check_tool_pins.py"
    ).resolve()
    roots = agent_writable_roots(tmp_path)

    assert script.is_file()
    assert script.is_relative_to((tmp_path / ".mimir_builtin_skills").resolve())
    assert not any(script.is_relative_to(root) for root in roots)

    declaration = parse_declared_shell_commands(
        [{"exec": "python3", "path": sys.executable, "script": str(script)}],
        writable_roots=roots,
    )
    command = f"python3 {script}"
    assert parse_service_shell_argv(command, "maintenance", declared=declaration) == [
        str(Path(sys.executable).resolve()),
        str(script),
    ]
