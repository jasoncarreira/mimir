"""Worklink tool-pin inventory and bump-issue rendering.

This module is deliberately pure around drift detection: configured ``ToolPin``
entries are compared against injected upstream resolvers, but pins are never
mutated and smoke commands are never run during inventory. The optional poller
slice can wire real network resolvers and Chainlink filing on top of these
primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Callable, Mapping, Protocol, Sequence

from .backends import ToolPin


DEFAULT_TOOL_PINS: tuple[ToolPin, ...] = (
    ToolPin(
        name="codex",
        category="coding-cli",
        pin="0.137.0",
        smoke="codex --version && env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_worklink_backends.py",
        source="npm",
        package="@openai/codex",
        install="scaffold Dockerfiles when the codex-plus extra is selected",
        risk="High: Worklink's first coding backend; changes can affect prompt execution, sandbox flags, transcript shape, and quota consumption.",
    ),
    ToolPin(
        name="chainlink",
        category="issue-cli",
        pin="chainlink-1.6.0",
        smoke="chainlink --version && chainlink issue ready",
        source="github-release",
        repo="dollspace-gay/chainlink",
        install="chainlink bundled-skill dockerfile.fragment",
        risk="High: Worklink coordination depends on issue, lock, comment, and dependency semantics staying compatible.",
    ),
    ToolPin(
        name="mermaid-cli",
        category="renderer",
        pin="11.15.0",
        smoke="mmdc --version",
        source="npm",
        package="@mermaid-js/mermaid-cli",
        install="scaffold Dockerfiles; used by the mermaid-diagrams skill rather than Worklink execution",
        risk="Low: renderer drift is usually isolated to diagram generation and Chromium dependencies.",
    ),
    ToolPin(
        name="claude-code",
        category="coding-cli",
        pin="2.1.168",
        smoke="claude --version",
        source="npm",
        package="@anthropic-ai/claude-code",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_CLAUDE_CODE=1",
        risk="Medium: optional second coding backend; real smoke requires deployments with the Claude CLI installed.",
    ),
    ToolPin(
        name="gogcli",
        category="integration-cli",
        pin="v0.9.0",
        smoke="gog --version",
        source="github-release",
        repo="steipete/gogcli",
        install="gmail-poller optional-skill dockerfile.fragment",
        risk="Medium: Google Workspace helper CLI; drift can break Gmail/Calendar polling independently of Worklink coding backends.",
    ),
)


def default_tool_pins() -> tuple[ToolPin, ...]:
    """Return the source-controlled initial Worklink external executable inventory."""

    return DEFAULT_TOOL_PINS


@dataclass(frozen=True)
class UpstreamVersion:
    """Resolved upstream version for one configured tool pin."""

    current: str
    changelog: str | None = None
    risk: str | None = None


@dataclass(frozen=True)
class ToolPinDiagnostic:
    """Non-fatal inventory diagnostic for skipped pins/resolver failures."""

    name: str
    category: str
    reason: str


@dataclass(frozen=True)
class ToolPinDrift:
    """A configured tool pin differs from its upstream version."""

    pin: ToolPin
    current: str
    changelog: str | None = None
    risk: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"worklink-tool-pin:{self.pin.category}:{self.pin.name}:{self.pin.pin}->{self.current}"


@dataclass(frozen=True)
class ToolPinInventory:
    """Result of a tool-pin inventory pass."""

    drift: tuple[ToolPinDrift, ...]
    diagnostics: tuple[ToolPinDiagnostic, ...] = ()


class ToolPinResolver(Protocol):
    def resolve(self, pin: ToolPin) -> UpstreamVersion: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]


def inventory_tool_pins(
    pins: Sequence[ToolPin],
    resolvers: Mapping[str, ToolPinResolver],
) -> ToolPinInventory:
    """Compare configured pins against upstream resolvers without side effects.

    Resolvers are selected by ``source`` first, then by ``category``. Pins with a
    missing/manual source, unknown source/category, matching upstream version, or
    resolver failure are reported as diagnostics/skips instead of crashing the
    poller path.
    """

    drift: list[ToolPinDrift] = []
    diagnostics: list[ToolPinDiagnostic] = []
    for pin in pins:
        resolver_key = pin.source or pin.category
        if not resolver_key or resolver_key in {"manual", "local"}:
            diagnostics.append(_diagnostic(pin, "manual pin has no upstream resolver"))
            continue
        resolver = resolvers.get(resolver_key) or resolvers.get(pin.category)
        if resolver is None:
            diagnostics.append(_diagnostic(pin, f"no resolver for source/category: {resolver_key}"))
            continue
        try:
            upstream = resolver.resolve(pin)
        except Exception as exc:  # noqa: BLE001 - poller path must skip, not crash.
            diagnostics.append(_diagnostic(pin, f"resolver failed: {exc}"))
            continue
        if upstream.current == pin.pin:
            continue
        drift.append(
            ToolPinDrift(
                pin=pin,
                current=upstream.current,
                changelog=upstream.changelog,
                risk=upstream.risk,
            )
        )
    return ToolPinInventory(drift=tuple(drift), diagnostics=tuple(diagnostics))


def render_bump_issue_title(drift: ToolPinDrift) -> str:
    """Render a low-priority Chainlink title for a tool-pin bump."""

    return f"Bump Worklink {drift.pin.name} pin to {drift.current}"


def render_bump_issue_body(drift: ToolPinDrift) -> str:
    """Render a Worklink-ready Chainlink bump issue body.

    The body includes a stable dedupe key, changelog/risk notes, planner
    sections, and the configured smoke command as the executor's suggested test
    command. The smoke command is documentation/evidence instruction only; this
    function never executes it.
    """

    pin = drift.pin
    changelog = drift.changelog or "No changelog metadata was returned by the resolver."
    risk = drift.risk or pin.risk or "Review upstream release notes and keep the bump scoped to this tool pin."
    package = pin.package or pin.repo or pin.name
    source = pin.source or pin.category
    install = pin.install or "Configured in worklink.yaml/tool_pins."
    return f"""Dedupe-Key: {drift.dedupe_key}

Bump configured Worklink tool pin `{pin.name}` for category `{pin.category}` from `{pin.pin}` to `{drift.current}`.

Upstream:
- Source: `{source}`
- Package/repo: `{package}`
- Current configured pin: `{pin.pin}`
- Latest resolved pin: `{drift.current}`
- Install surface: {install}

Changelog / release notes:
{changelog}

Risk notes:
{risk}

Acceptance criteria:
- [ ] Update the configured `{pin.name}` Worklink tool pin from `{pin.pin}` to `{drift.current}`.
- [ ] Keep the change scoped to the pin bump and any required lockfile/generated metadata.
- [ ] Run the configured smoke command successfully.

Review criteria:
- Verify the upstream version is still current at review time and the smoke output covers `{pin.name}`.
- Confirm the category/risk surface is still `{pin.category}`; do not treat renderer/helper CLI bumps as equivalent to coding-backend bumps.

Worklink notes:
- Scope: Worklink tool-pin configuration for `{pin.name}` only.
- Out of scope: unrelated tool upgrades, backend behavior changes, or poller policy changes.
- Suggested test command: {pin.smoke}
"""


class ChainlinkBumpFiler:
    """Create Chainlink bump issues with dedupe-key protection."""

    def __init__(self, *, chainlink_bin: str = "chainlink", runner: Runner | None = None) -> None:
        self.chainlink_bin = chainlink_bin
        self.runner = runner or _run

    def existing_issue_id(self, dedupe_key: str) -> int | None:
        result = self.runner([self.chainlink_bin, "issue", "search", dedupe_key, "--json"])
        if result.returncode != 0:
            return None
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(issues, list):
            return None
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            haystack = "\n".join(
                str(issue.get(field) or "")
                for field in ("title", "description", "body")
            )
            if dedupe_key in haystack:
                issue_id = issue.get("id") or issue.get("number")
                try:
                    return int(issue_id)
                except (TypeError, ValueError):
                    return None
        return None

    def file(self, drift: ToolPinDrift) -> int | None:
        existing = self.existing_issue_id(drift.dedupe_key)
        if existing is not None:
            return existing
        result = self.runner(
            [
                self.chainlink_bin,
                "issue",
                "create",
                render_bump_issue_title(drift),
                "--description",
                render_bump_issue_body(drift),
                "--priority",
                "low",
                "--label",
                "worklink",
                "--label",
                "tool-pin",
            ]
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or "chainlink issue create failed"
            raise RuntimeError(message)
        return _created_issue_id(result.stdout)


def _diagnostic(pin: ToolPin, reason: str) -> ToolPinDiagnostic:
    return ToolPinDiagnostic(name=pin.name, category=pin.category, reason=reason)


def _created_issue_id(stdout: str) -> int | None:
    for token in stdout.replace("#", " #").split():
        if token.startswith("#"):
            try:
                return int(token[1:])
            except ValueError:
                continue
    return None


def _run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, **kwargs)
