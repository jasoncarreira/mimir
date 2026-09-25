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


def test_inventory_reports_drift_without_mutating_or_smoking(fresh_poller):
    pin = fresh_poller.ToolPin(
        "codex",
        "coding-cli",
        "0.139.0",
        "codex --version",
        source="npm",
        package="@openai/codex",
    )
    resolver = FakeNpmResolver("0.140.0", fresh_poller)

    inventory = fresh_poller.inventory_tool_pins([pin], {"npm": resolver})

    assert resolver.calls == [pin]
    assert inventory.diagnostics == ()
    assert inventory.drift == (
        fresh_poller.ToolPinDrift(
            pin=pin,
            current="0.140.0",
            changelog="fake changelog",
            risk="fake risk",
        ),
    )
    assert inventory.drift[0].dedupe_key == "worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0"


def test_inventory_skips_matching_manual_unknown_and_failed_resolvers(fresh_poller):
    matching = fresh_poller.ToolPin("mermaid", "renderer", "11.16.0", "mmdc --version", source="npm")
    manual = fresh_poller.ToolPin("bespoke", "coding-cli", "local", "bespoke --version", source="manual")
    unknown = fresh_poller.ToolPin("other", "coding-cli", "1.0.0", "other --version", source="github")
    failing = fresh_poller.ToolPin("chainlink", "issue-cli", "1.6.0", "chainlink --version", source="cargo")

    inventory = fresh_poller.inventory_tool_pins(
        [matching, manual, unknown, failing],
        {"npm": FakeNpmResolver("11.16.0", fresh_poller), "cargo": FailingResolver()},
    )

    assert inventory.drift == ()
    assert [(diag.name, diag.reason) for diag in inventory.diagnostics] == [
        ("bespoke", "manual pin has no upstream resolver"),
        ("other", "no resolver for source/category: github"),
        ("chainlink", "resolver failed: lookup unavailable"),
    ]


def test_rendered_bump_issue_is_worklink_ready(fresh_poller):
    drift = fresh_poller.ToolPinDrift(
        pin=fresh_poller.ToolPin(
            "codex",
            "coding-cli",
            "0.139.0",
            "codex --version && uv run pytest -q tests/test_worklink_backends.py",
            source="npm",
            package="@openai/codex",
        ),
        current="0.140.0",
        changelog="- release notes here",
        risk="low risk",
    )

    assert fresh_poller.render_bump_issue_title(drift) == "Bump Worklink codex pin to 0.140.0"
    body = fresh_poller.render_bump_issue_body(drift)

    assert "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0" in body
    assert "- release notes here" in body
    assert "low risk" in body
    assert "Acceptance criteria:" in body
    assert "Review criteria:" in body
    assert "Worklink notes:" in body
    assert "- Suggested test command: codex --version && uv run pytest -q tests/test_worklink_backends.py" in body


def test_poller_home_requires_mimir_home(fresh_poller, monkeypatch, tmp_path, capsys):
    assert not hasattr(fresh_poller, "DEFAULT_HOME")

    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert fresh_poller._home() is None
    assert fresh_poller.main() == 0
    emitted = _events(capsys)[0]
    assert emitted["signal"] == "worklink_tool_pins_misconfigured"
    assert emitted["reason"] == "MIMIR_HOME unset"

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert fresh_poller._home() == tmp_path


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


@pytest.mark.parametrize("binary", [None, "", "/custom tools/chainlink"])
def test_detected_drift_files_issue_and_emits_jsonl(fresh_poller, monkeypatch, capsys, binary):
    if binary is not None:
        monkeypatch.setenv("CHAINLINK_BIN", binary)
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
    assert calls[0][:3] == [binary or "chainlink", "issue", "search"]
    assert create[:3] == [binary or "chainlink", "issue", "create"]
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
            assert args[:3] == ["chainlink", "issue", "search"]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "id": "invalid",
                            "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
                        },
                        {
                            "id": 901,
                            "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
                        },
                    ]
                ),
                "",
            )

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0
    assert len(calls) == 1
    assert _events(capsys)[0]["issue_id"] == 901


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "reason"),
    [
        (1, "", "index.lock exists", "chainlink issue search failed: index.lock exists"),
        (0, "not JSON", "", "chainlink issue search returned invalid JSON"),
        (0, "{}", "", "chainlink issue search JSON was not a list"),
    ],
)
def test_unavailable_dedupe_search_skips_create_and_emits_durable_signal(
    fresh_poller, monkeypatch, capsys, returncode, stdout, stderr, reason,
):
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
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0
    assert len(calls) == 1
    event = _events(capsys)[0]
    assert event["signal"] == "worklink_tool_pin_dedupe_check_failed"
    assert event["dedupe_key"] == "worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0"
    assert event["reason"] == reason


@pytest.mark.parametrize(
    "issue_id",
    [
        pytest.param("invalid", id="malformed-string"),
        pytest.param("901", id="numeric-string"),
        pytest.param(True, id="boolean"),
        pytest.param(1.5, id="float"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(None, id="missing"),
    ],
)
def test_sole_exact_match_without_valid_id_skips_create_and_emits_durable_signal(
    fresh_poller, monkeypatch, capsys, issue_id,
):
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
            issue = {
                "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
            }
            if issue_id is not None:
                issue["id"] = issue_id
            return subprocess.CompletedProcess(args, 0, json.dumps([issue]), "")

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0
    assert len(calls) == 1
    event = _events(capsys)[0]
    assert event["signal"] == "worklink_tool_pin_dedupe_check_failed"
    assert event["dedupe_key"] == "worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0"
    assert event["reason"] == (
        "chainlink issue search returned an exact dedupe-key match "
        "without a strict positive-integer issue id"
    )


def test_valid_number_is_reused_when_id_field_is_malformed(
    fresh_poller, monkeypatch, capsys,
):
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
    monkeypatch.setattr(
        fresh_poller,
        "_resolvers",
        lambda: {"npm": FakeNpmResolver("0.140.0", fresh_poller)},
    )
    calls: list[list[str]] = []

    def runner(cwd):
        def run(args, **kwargs):
            calls.append(args)
            issue = {
                "id": "malformed",
                "number": 902,
                "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
            }
            return subprocess.CompletedProcess(args, 0, json.dumps([issue]), "")

        return run

    monkeypatch.setattr(fresh_poller, "_chainlink_runner", runner)

    assert fresh_poller.main() == 0
    assert len(calls) == 1
    event = _events(capsys)[0]
    assert event["issue_id"] == 902


def test_create_failure_raises(fresh_poller):
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[:3] == ["chainlink", "issue", "search"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 1, "", "boom")

    drift = fresh_poller.ToolPinDrift(
        pin=fresh_poller.ToolPin("codex", "coding-cli", "0.139.0", "codex --version", source="npm"),
        current="0.140.0",
    )

    with pytest.raises(RuntimeError, match="boom"):
        fresh_poller.ChainlinkBumpFiler(runner=runner).file(drift)
    assert len(calls) == 2


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
        ["python3", "scripts/poller.py"],
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
