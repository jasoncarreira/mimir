from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def fresh_poller(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("POLLER_NAME", "worklink-tool-pins")
    monkeypatch.delenv("WORKLINK_CONFIG", raising=False)
    monkeypatch.delenv("CHAINLINK_CWD", raising=False)
    monkeypatch.delenv("CHAINLINK_BIN", raising=False)
    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _events(capsys) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _write_config(home: Path, body: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "worklink.yaml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


class FakeNpmResolver:
    def __init__(self, current: str, poller_module=None) -> None:
        self.current = current
        self.poller_module = poller_module
        self.calls = []

    def resolve(self, pin):
        self.calls.append(pin)
        return self.poller_module.UpstreamVersion(current=self.current, changelog="fake changelog", risk="fake risk")


class FailingResolver:
    def resolve(self, pin):
        raise RuntimeError("lookup unavailable")


def test_missing_config_exits_zero_silently(fresh_poller, capsys):
    assert fresh_poller.main() == 0
    assert _events(capsys) == []


def test_no_drift_exits_zero_silently(fresh_poller, monkeypatch, capsys):
    home = Path(sys.modules["os"].environ["MIMIR_HOME"])
    _write_config(
        home,
        """
        tool_pins:
          - name: codex
            category: coding-cli
            pin: "0.139.0"
            smoke: "codex --version"
            source: npm
            package: "@openai/codex"
        """,
    )
    monkeypatch.setattr(fresh_poller, "_resolvers", lambda: {"npm": FakeNpmResolver("0.139.0", fresh_poller)})

    assert fresh_poller.main() == 0
    assert _events(capsys) == []


def test_detected_drift_files_issue_and_emits_jsonl(fresh_poller, monkeypatch, capsys):
    home = Path(sys.modules["os"].environ["MIMIR_HOME"])
    _write_config(
        home,
        """
        tool_pins:
          - name: codex
            category: coding-cli
            pin: "0.139.0"
            smoke: "codex --version && echo smoke"
            source: npm
            package: "@openai/codex"
        """,
    )
    monkeypatch.setattr(fresh_poller, "_resolvers", lambda: {"npm": FakeNpmResolver("0.140.0", fresh_poller)})

    calls: list[list[str]] = []

    def runner(cwd):
        assert cwd == home

        def run(args, **kwargs):
            calls.append(args)
            if args[2] == "search":
                return subprocess.CompletedProcess(args, 0, "[]", "")
            if args[2] == "create":
                return subprocess.CompletedProcess(args, 0, "Created issue #900\n", "")
            raise AssertionError(args)

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0

    events = _events(capsys)
    assert len(events) == 1
    assert events[0]["poller"] == "worklink-tool-pins"
    assert events[0]["event_type"] == "worklink_tool_pin_drift"
    assert events[0]["issue_id"] == 900
    assert events[0]["dedupe_key"] == "worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0"
    assert "Chainlink bump issue: #900" in events[0]["prompt"]

    create = calls[1]
    assert create[:3] == ["/usr/local/bin/chainlink", "issue", "create"]
    assert "--priority" in create and create[create.index("--priority") + 1] == "low"
    body = create[create.index("--description") + 1]
    assert "Suggested test command: codex --version && echo smoke" in body


def test_reuses_existing_issue_by_dedupe_key(fresh_poller, monkeypatch, capsys):
    home = Path(sys.modules["os"].environ["MIMIR_HOME"])
    _write_config(
        home,
        """
        tool_pins:
          - name: codex
            category: coding-cli
            pin: "0.139.0"
            smoke: "codex --version"
            source: npm
        """,
    )
    monkeypatch.setattr(fresh_poller, "_resolvers", lambda: {"npm": FakeNpmResolver("0.140.0", fresh_poller)})

    calls: list[list[str]] = []

    def runner(cwd):
        def run(args, **kwargs):
            calls.append(args)
            assert args[:3] == ["/usr/local/bin/chainlink", "issue", "search"]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "id": 901,
                            "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
                        }
                    ]
                ),
                "",
            )

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0
    assert len(calls) == 1
    assert _events(capsys)[0]["issue_id"] == 901


def test_lookup_failure_is_diagnostic_not_emit_or_nonzero(fresh_poller, monkeypatch, capsys):
    home = Path(sys.modules["os"].environ["MIMIR_HOME"])
    _write_config(
        home,
        """
        tool_pins:
          - name: codex
            category: coding-cli
            pin: "0.139.0"
            smoke: "codex --version"
            source: npm
        """,
    )
    monkeypatch.setattr(fresh_poller, "_resolvers", lambda: {"npm": FailingResolver()})

    assert fresh_poller.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolver failed: lookup unavailable" in captured.err


def test_production_subprocess_imports_without_mimir_or_pyyaml(tmp_path: Path):
    skill_dir = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    _write_config(
        home,
        """
        tool_pins:
          - name: codex
            category: coding-cli
            pin: "0.139.0"
            smoke: "codex --version"
            source: manual
        """,
    )

    result = subprocess.run(
        ["python3", "poller.py"],
        cwd=skill_dir,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "MIMIR_HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "manual pin has no upstream resolver" in result.stderr


def test_resolvers_do_not_run_smoke_commands(monkeypatch, fresh_poller):
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["npm", "view", "@openai/codex"]:
            return subprocess.CompletedProcess(args, 0, "0.140.0\n", "")
        if args[:4] == ["gh", "release", "view", "--repo"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"tagName": "v1.2.3", "url": "https://example.test"}), "")
        raise AssertionError(f"unexpected command: {args}")

    npm = fresh_poller.NpmVersionResolver(runner=run)
    gh = fresh_poller.GitHubReleaseResolver(runner=run)

    assert npm.resolve(fresh_poller.ToolPin("codex", "coding-cli", "0.139.0", "SHOULD_NOT_RUN", source="npm", package="@openai/codex")).current == "0.140.0"
    assert gh.resolve(fresh_poller.ToolPin("chainlink", "tracker", "v1.0.0", "SHOULD_NOT_RUN", source="github-release", repo="owner/repo")).current == "v1.2.3"
    assert calls == [
        ["npm", "view", "@openai/codex", "version"],
        ["gh", "release", "view", "--repo", "owner/repo", "--json", "tagName", "url"],
    ]
