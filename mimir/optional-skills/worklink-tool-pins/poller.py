#!/usr/bin/env python3
"""Worklink tool-pin drift poller.

This file is intentionally standalone: optional pollers are launched as
``python3 poller.py`` in a scrubbed subprocess environment, not inside mimir's
venv/import path. Keep this script stdlib-only and do not import ``mimir.*``.

Loads the ``tool_pins`` section from ``<home>/worklink.yaml``, inventories the
configured pins against upstream version sources, and files/reuses low-priority
Chainlink bump issues when drift is detected. Healthy paths are silent: missing
config, no pins, or no drift all exit 0 with no stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping, Protocol, Sequence

POLLER_NAME = os.environ.get("POLLER_NAME", "worklink-tool-pins")


@dataclass(frozen=True)
class ToolPin:
    name: str
    category: str
    pin: str
    smoke: str
    source: str | None = None
    package: str | None = None
    repo: str | None = None


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


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _emit(event: dict) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


class NpmVersionResolver:
    def __init__(self, *, runner=subprocess.run) -> None:
        self.runner = runner

    def resolve(self, pin: ToolPin) -> UpstreamVersion:
        package = pin.package or pin.name
        result = self.runner(
            ["npm", "view", package, "version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"npm view failed for {package}")
        version = result.stdout.strip().splitlines()[-1].strip()
        if not version:
            raise RuntimeError(f"npm view returned no version for {package}")
        return UpstreamVersion(
            current=version,
            changelog=f"Check npm package `{package}` release metadata before bumping.",
            risk="NPM CLI bump: verify binary availability and run the configured smoke command.",
        )


class GitHubReleaseResolver:
    def __init__(self, *, runner=subprocess.run) -> None:
        self.runner = runner

    def resolve(self, pin: ToolPin) -> UpstreamVersion:
        if not pin.repo:
            raise RuntimeError("github release pin requires repo")
        result = self.runner(
            ["gh", "release", "view", "--repo", pin.repo, "--json", "tagName", "url"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"gh release view failed for {pin.repo}")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh release view returned non-JSON for {pin.repo}") from exc
        tag = str(data.get("tagName") or "").strip()
        if not tag:
            raise RuntimeError(f"gh release view returned no tagName for {pin.repo}")
        url = str(data.get("url") or "").strip()
        changelog = f"Review latest GitHub release for `{pin.repo}`."
        if url:
            changelog += f"\n{url}"
        return UpstreamVersion(
            current=tag,
            changelog=changelog,
            risk="GitHub release bump: verify compatibility and run the configured smoke command.",
        )


def inventory_tool_pins(
    pins: Sequence[ToolPin],
    resolvers: Mapping[str, ToolPinResolver],
) -> ToolPinInventory:
    """Compare configured pins against upstream resolvers without side effects."""

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
    """Render a Worklink-ready Chainlink bump issue body."""

    pin = drift.pin
    changelog = drift.changelog or "No changelog metadata was returned by the resolver."
    risk = drift.risk or "Review upstream release notes and keep the bump scoped to this tool pin."
    package = pin.package or pin.repo or pin.name
    source = pin.source or pin.category
    return f"""Dedupe-Key: {drift.dedupe_key}

Bump configured Worklink tool pin `{pin.name}` for category `{pin.category}` from `{pin.pin}` to `{drift.current}`.

Upstream:
- Source: `{source}`
- Package/repo: `{package}`
- Current configured pin: `{pin.pin}`
- Latest resolved pin: `{drift.current}`

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


def _home() -> Path | None:
    """Agent home from ``MIMIR_HOME``. ``None`` when unset — the poller then
    refuses to run rather than guessing a container path like ``/mimir-home``
    that doesn't exist off-Docker (matches the chainlink-orchestrator poller)."""
    raw = os.environ.get("MIMIR_HOME")
    return Path(raw) if raw else None


def _worklink_config_path(home: Path) -> Path:
    return Path(os.environ.get("WORKLINK_CONFIG") or home / "worklink.yaml")


def _chainlink_cwd(home: Path) -> Path:
    return Path(os.environ.get("CHAINLINK_CWD") or home)


def _chainlink_bin() -> str:
    return os.environ.get("CHAINLINK_BIN", "/usr/local/bin/chainlink")


def _chainlink_runner(cwd: Path):
    def run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, **kwargs)

    return run


def _resolvers() -> dict[str, ToolPinResolver]:
    npm = NpmVersionResolver()
    gh_release = GitHubReleaseResolver()
    return {
        "npm": npm,
        "github-release": gh_release,
        "github": gh_release,
        "github-tag": gh_release,
    }


def _load_config(path: Path) -> tuple[ToolPin, ...] | None:
    if not path.exists():
        return None
    return _parse_tool_pins_section(path.read_text(encoding="utf-8"))


def _parse_tool_pins_section(text: str) -> tuple[ToolPin, ...]:
    """Parse the documented top-level ``tool_pins`` list using stdlib only.

    This is not a general YAML parser. It supports the Worklink tool-pin shape
    documented by this skill: a top-level ``tool_pins:`` key whose value is a
    list of scalar mappings. Poller subprocesses cannot assume PyYAML or mimir's
    import path are available, so the standalone parser is the production path.
    """

    block = _tool_pins_block(text)
    if block is None:
        return ()
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in block:
        line = _strip_inline_comment(raw).strip()
        if not line:
            continue
        if line.startswith("- ") or line == "-":
            if current is not None:
                items.append(current)
            current = {}
            rest = line[1:].strip()
            if rest:
                key, value = _split_key_value(rest)
                current[key] = _parse_scalar(value)
            continue
        if current is None:
            raise ValueError("worklink tool_pins must be a list of mappings")
        key, value = _split_key_value(line)
        current[key] = _parse_scalar(value)
    if current is not None:
        items.append(current)
    return tuple(_tool_pin_from_mapping(item, index=index) for index, item in enumerate(items))


def _tool_pins_block(text: str) -> list[str] | None:
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        stripped = _strip_inline_comment(raw).strip()
        if not stripped:
            continue
        if not stripped.startswith("tool_pins:"):
            continue
        key, value = _split_key_value(stripped)
        if key != "tool_pins":
            continue
        if value.strip() in {"", "|"}:
            base_indent = _indent(raw)
            block: list[str] = []
            for candidate in lines[index + 1 :]:
                candidate_stripped = _strip_inline_comment(candidate).strip()
                if candidate_stripped and _indent(candidate) <= base_indent:
                    break
                block.append(candidate)
            return block
        if value.strip() == "[]":
            return []
        raise ValueError("worklink tool_pins must be a list")
    return None


def _tool_pin_from_mapping(data: Mapping[str, str], *, index: int) -> ToolPin:
    missing = [field for field in ("name", "category", "pin", "smoke") if field not in data]
    if missing:
        raise ValueError(f"worklink tool_pins[{index}] missing required field(s): {', '.join(missing)}")
    return ToolPin(
        name=str(data["name"]),
        category=str(data["category"]),
        pin=str(data["pin"]),
        smoke=str(data["smoke"]),
        source=str(data["source"]) if "source" in data else None,
        package=str(data["package"]) if "package" in data else None,
        repo=str(data["repo"]) if "repo" in data else None,
    )


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"expected key/value mapping entry: {text}")
    key, value = text.split(":", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"empty key in mapping entry: {text}")
    return key, value.strip()


def _parse_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip('"\'')
        return str(parsed)
    return value


def _strip_inline_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None and (index == 0 or text[index - 1].isspace()):
            return text[:index]
    return text


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


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


def _event_for_drift(drift: ToolPinDrift, issue_id: int | None) -> dict:
    return {
        "poller": POLLER_NAME,
        "event_type": "worklink_tool_pin_drift",
        "issue_id": issue_id,
        "dedupe_key": drift.dedupe_key,
        "tool_name": drift.pin.name,
        "category": drift.pin.category,
        "configured_pin": drift.pin.pin,
        "current_pin": drift.current,
        "prompt": (
            f"Worklink tool pin drift detected for {drift.pin.name}: "
            f"configured {drift.pin.pin}, upstream {drift.current}. "
            f"Chainlink bump issue: #{issue_id if issue_id is not None else 'unknown'}."
        ),
    }


def main() -> int:
    home = _home()
    if home is None:
        _emit({"signal": "worklink_tool_pins_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    config_path = _worklink_config_path(home)
    try:
        tool_pins = _load_config(config_path)
    except Exception as exc:  # noqa: BLE001 - bad local config is a poller runtime error.
        _log(f"failed to load {config_path}: {exc}")
        return 1
    if not tool_pins:
        return 0

    inventory = inventory_tool_pins(tool_pins, _resolvers())
    for diagnostic in inventory.diagnostics:
        _log(f"tool-pin diagnostic {diagnostic.category}/{diagnostic.name}: {diagnostic.reason}")
    if not inventory.drift:
        return 0

    filer = ChainlinkBumpFiler(chainlink_bin=_chainlink_bin(), runner=_chainlink_runner(_chainlink_cwd(home)))
    for drift in inventory.drift:
        issue_id = filer.file(drift)
        _emit(_event_for_drift(drift, issue_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
