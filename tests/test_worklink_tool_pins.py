from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from mimir.worklink.backends import ToolPin
from mimir.worklink.tool_pins import (
    ChainlinkBumpFiler,
    ToolPinDrift,
    UpstreamVersion,
    default_tool_pins,
    inventory_tool_pins,
    render_bump_issue_body,
    render_bump_issue_title,
)


class FakeResolver:
    def __init__(self, current: str, *, changelog: str | None = None, risk: str | None = None) -> None:
        self.current = current
        self.changelog = changelog
        self.risk = risk
        self.calls: list[ToolPin] = []

    def resolve(self, pin: ToolPin) -> UpstreamVersion:
        self.calls.append(pin)
        return UpstreamVersion(current=self.current, changelog=self.changelog, risk=self.risk)


class FailingResolver:
    def resolve(self, pin: ToolPin) -> UpstreamVersion:
        raise RuntimeError("upstream unavailable")


def test_default_tool_pin_inventory_covers_distinct_executable_risk_surfaces() -> None:
    pins = {pin.name: pin for pin in default_tool_pins()}

    expected = {
        "codex", "chainlink", "mermaid-cli", "claude-code", "osv-scanner", "gogcli",
        "opencode", "opencode-feature-factory", "opencode-project-memory",
        "opencode-openai-codex-auth", "opencode-anthropic-auth",
    }
    assert set(pins) == expected
    assert pins["codex"].category == "coding-cli"
    assert pins["claude-code"].category == "coding-cli"
    assert pins["chainlink"].category == "issue-cli"
    assert pins["mermaid-cli"].category == "renderer"
    assert pins["gogcli"].category == "integration-cli"
    assert pins["osv-scanner"].category == "security-scanner"
    assert pins["opencode"].category == "coding-cli"
    assert pins["opencode-feature-factory"].category == "coding-plugin"
    assert pins["opencode-project-memory"].category == "coding-plugin"
    assert pins["opencode-openai-codex-auth"].category == "coding-plugin"
    assert pins["opencode-anthropic-auth"].category == "coding-plugin"
    for pin in pins.values():
        assert pin.pin
        assert pin.smoke
        assert pin.source
        assert pin.install
        assert pin.risk


def test_default_tool_pin_inventory_matches_shipped_install_literals() -> None:
    pins = {pin.name: pin for pin in default_tool_pins()}
    root = Path(__file__).resolve().parents[1]
    install_text = "\n".join(
        (root / relpath).read_text(encoding="utf-8")
        for relpath in (
            "Dockerfile",
            "mimir/scaffold_docker.py",
            "mimir/skills/chainlink/dockerfile.fragment",
            "mimir/optional-skills/gmail-poller/dockerfile.fragment",
            "mimir/optional-skills/dependency-advisory-watch/dockerfile.fragment",
        )
    )

    assert f"@openai/codex@{pins['codex'].pin}" in install_text
    assert f"--tag {pins['chainlink'].pin}" in install_text
    assert f"@mermaid-js/mermaid-cli@{pins['mermaid-cli'].pin}" in install_text
    assert f"@anthropic-ai/claude-code@{pins['claude-code'].pin}" in install_text
    assert f"github.com/steipete/gogcli/cmd/gog@{pins['gogcli'].pin}" in install_text
    assert pins["osv-scanner"].pin in install_text
    assert f"opencode-ai@{pins['opencode'].pin}" in install_text
    assert f"opencode-feature-factory@{pins['opencode-feature-factory'].pin}" in install_text
    assert f"opencode-project-memory@{pins['opencode-project-memory'].pin}" in install_text


def test_inventory_tool_pins_reports_drift_without_mutating_or_smoking() -> None:
    pin = ToolPin(
        name="codex",
        category="coding-cli",
        pin="0.139.0",
        smoke="codex --version",
        source="npm",
        package="@openai/codex",
    )
    resolver = FakeResolver("0.140.0", changelog="- fixed worker mode", risk="medium")

    inventory = inventory_tool_pins([pin], {"npm": resolver})

    assert resolver.calls == [pin]
    assert inventory.diagnostics == ()
    assert inventory.drift == (
        ToolPinDrift(pin=pin, current="0.140.0", changelog="- fixed worker mode", risk="medium"),
    )
    assert inventory.drift[0].dedupe_key == "worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0"


def test_inventory_tool_pins_skips_matching_manual_unknown_and_failed_resolvers() -> None:
    matching = ToolPin("mermaid", "renderer", "11.16.0", "mmdc --version", source="npm")
    manual = ToolPin("bespoke", "coding-cli", "local", "bespoke --version", source="manual")
    unknown = ToolPin("other", "coding-cli", "1.0.0", "other --version", source="github")
    failing = ToolPin("chainlink", "issue-cli", "1.6.0", "chainlink --version", source="cargo")

    inventory = inventory_tool_pins(
        [matching, manual, unknown, failing],
        {"npm": FakeResolver("11.16.0"), "cargo": FailingResolver()},
    )

    assert inventory.drift == ()
    assert [(diag.name, diag.reason) for diag in inventory.diagnostics] == [
        ("bespoke", "manual pin has no upstream resolver"),
        ("other", "no resolver for source/category: github"),
        ("chainlink", "resolver failed: upstream unavailable"),
    ]


def test_render_bump_issue_body_is_worklink_ready_and_uses_smoke_as_suggested_test() -> None:
    drift = ToolPinDrift(
        pin=ToolPin(
            name="codex",
            category="coding-cli",
            pin="0.139.0",
            smoke="codex --version && uv run pytest -q tests/test_worklink_backends.py",
            source="npm",
            package="@openai/codex",
        ),
        current="0.140.0",
        changelog="- release notes here",
        risk="low risk",
    )

    assert render_bump_issue_title(drift) == "Bump Worklink codex pin to 0.140.0"
    body = render_bump_issue_body(drift)

    assert "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0" in body
    assert "- release notes here" in body
    assert "low risk" in body
    assert "Install surface:" in body
    assert "Confirm the category/risk surface is still `coding-cli`" in body
    assert "Acceptance criteria:" in body
    assert "Review criteria:" in body
    assert "Worklink notes:" in body
    assert "- Suggested test command: codex --version && uv run pytest -q tests/test_worklink_backends.py" in body


def test_chainlink_bump_filer_reuses_existing_issue_by_dedupe_key() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = json.dumps([
            {
                "id": 777,
                "title": "Bump Worklink codex pin",
                "description": "Dedupe-Key: worklink-tool-pin:coding-cli:codex:0.139.0->0.140.0",
            }
        ])
        return subprocess.CompletedProcess(args, 0, stdout, "")

    drift = ToolPinDrift(
        pin=ToolPin("codex", "coding-cli", "0.139.0", "codex --version", source="npm"),
        current="0.140.0",
    )

    assert ChainlinkBumpFiler(runner=runner).file(drift) == 777
    assert calls == [["chainlink", "issue", "search", drift.dedupe_key, "--json"]]


def test_chainlink_bump_filer_creates_low_priority_issue_when_no_duplicate() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["chainlink", "issue", "search"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 0, "Created issue #778\n", "")

    drift = ToolPinDrift(
        pin=ToolPin("codex", "coding-cli", "0.139.0", "codex --version", source="npm"),
        current="0.140.0",
    )

    assert ChainlinkBumpFiler(runner=runner).file(drift) == 778
    assert calls[1][:4] == ["chainlink", "issue", "create", "Bump Worklink codex pin to 0.140.0"]
    assert "--priority" in calls[1]
    assert "low" in calls[1]
    assert "--label" in calls[1]
    assert "tool-pin" in calls[1]


def test_chainlink_bump_filer_raises_on_create_failure() -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["chainlink", "issue", "search"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 1, "", "boom")

    drift = ToolPinDrift(
        pin=ToolPin("codex", "coding-cli", "0.139.0", "codex --version", source="npm"),
        current="0.140.0",
    )

    with pytest.raises(RuntimeError, match="boom"):
        ChainlinkBumpFiler(runner=runner).file(drift)


def _load_tool_pins_poller():
    import importlib.util
    import sys

    poller_path = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "optional-skills" / "worklink-tool-pins" / "poller.py"
    )
    spec = importlib.util.spec_from_file_location("wtp_poller_under_test", poller_path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the poller's @dataclass __module__ lookup resolves.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def test_poller_home_requires_mimir_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """#portability: the tool-pins poller must not default to a hard-coded
    container path. Without MIMIR_HOME, _home() is None and main() emits a
    misconfigured signal instead of scanning a nonexistent /mimir-home."""
    import contextlib
    import io

    mod = _load_tool_pins_poller()
    assert not hasattr(mod, "DEFAULT_HOME")  # the /mimir-home default is gone

    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert mod._home() is None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main()
    assert rc == 0
    emitted = json.loads(buf.getvalue().strip())
    assert emitted["signal"] == "worklink_tool_pins_misconfigured"
    assert emitted["reason"] == "MIMIR_HOME unset"

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert mod._home() == tmp_path
