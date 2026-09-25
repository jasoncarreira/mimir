"""Source-controlled Worklink tool-pin data."""

from __future__ import annotations

from .backends import ToolPin


OPENCODE_VERSION = "1.18.21"


DEFAULT_TOOL_PINS: tuple[ToolPin, ...] = (
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
        pin="11.16.0",
        smoke="mmdc --version",
        source="npm",
        package="@mermaid-js/mermaid-cli",
        install="scaffold Dockerfiles; used by the mermaid-diagrams skill rather than Worklink execution",
        risk="Low: renderer drift is usually isolated to diagram generation and Chromium dependencies.",
    ),
    ToolPin(
        name="osv-scanner",
        category="security-scanner",
        pin="v2.4.0",
        smoke="osv-scanner --version",
        source="github-release",
        repo="google/osv-scanner",
        install="dependency-advisory-watch optional-skill dockerfile.fragment",
        risk="High: dependency security findings and fail-open behavior depend on scanner CLI semantics.",
    ),
    ToolPin(
        name="gogcli",
        category="integration-cli",
        pin="v0.9.0",
        smoke=(
            "gog --version && gog gmail messages search 'in:inbox newer_than:1d' "
            "--account \"$GOG_ACCOUNT\" --max 1 --json --no-input"
        ),
        source="github-release",
        repo="steipete/gogcli",
        install="gmail-poller optional-skill dockerfile.fragment",
        risk="High: Google Workspace helper CLI; pre-1.0 minor-version drift can break Gmail polling subcommands on Muninn, so version jumps need an authenticated smoke before merge.",
    ),
    ToolPin(
        name="opencode",
        category="coding-cli",
        pin=OPENCODE_VERSION,
        smoke="opencode --version",
        source="npm",
        package="opencode-ai",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1",
        risk="High: Worklink coding backend; changes can affect prompt execution, plugin loading, and auth flow.",
    ),
    ToolPin(
        name="feature-factory",
        category="coding-cli",
        pin="0.10.8",
        smoke="test -f \"$MIMIR_FACTORY_ENTRYPOINT\"",
        source="npm",
        package="feature-factory",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1; owns the absolute factory.js CLI entrypoint",
        risk="High: Feature factory state-machine CLI; changes can affect Worklink epic lifecycle and recovery.",
    ),
    ToolPin(
        name="opencode-feature-factory",
        category="coding-plugin",
        pin="0.10.8",
        smoke="opencode --version",
        source="npm",
        package="opencode-feature-factory",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1; registered as OpenCode plugin",
        risk="High: Feature factory plugin; changes can affect Worklink epic/leaf dispatch and review flow.",
    ),
    ToolPin(
        name="opencode-project-memory",
        category="coding-plugin",
        pin="0.1.0",
        smoke="opencode --version",
        source="npm",
        package="opencode-project-memory",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1; registered as OpenCode plugin with memoryDir=.opencode/memory",
        risk="Medium: Project memory plugin; changes can affect memory indexing and retrieval behavior.",
    ),
    ToolPin(
        name="opencode-openai-codex-auth",
        category="coding-plugin",
        pin="4.4.0",
        smoke="opencode --version",
        source="npm",
        package="opencode-openai-codex-auth",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1; provides Codex auth for OpenCode",
        risk="Medium: Codex auth plugin; changes can affect authentication flow to OpenAI Codex.",
    ),
    ToolPin(
        name="opencode-anthropic-auth",
        category="coding-plugin",
        pin="0.0.13",
        smoke="opencode --version",
        source="npm",
        package="opencode-anthropic-auth",
        install="root/scaffold Dockerfiles only when MIMIR_ENABLE_OPENCODE=1; provides Anthropic auth for OpenCode",
        risk="Medium: Anthropic auth plugin; changes can affect authentication flow to Anthropic API.",
    ),
)


def default_tool_pins() -> tuple[ToolPin, ...]:
    """Return the source-controlled initial Worklink external executable inventory."""

    return DEFAULT_TOOL_PINS
