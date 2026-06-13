#!/usr/bin/env python3
"""Worklink tool-pin drift poller.

Loads ``<home>/worklink.yaml``, inventories configured ``tool_pins`` against
upstream version sources, and files/reuses low-priority Chainlink bump issues
when drift is detected. Healthy paths are silent: missing config, no pins, or no
drift all exit 0 with no stdout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from mimir.worklink.backends import ToolPin, WorklinkConfig
from mimir.worklink.tool_pins import (
    ChainlinkBumpFiler,
    ToolPinDrift,
    ToolPinResolver,
    UpstreamVersion,
    inventory_tool_pins,
)

POLLER_NAME = os.environ.get("POLLER_NAME", "worklink-tool-pins")
DEFAULT_HOME = Path("/mimir-home")


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


def _home() -> Path:
    return Path(os.environ.get("MIMIR_HOME") or DEFAULT_HOME)


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


def _load_config(path: Path) -> WorklinkConfig | None:
    if not path.exists():
        return None
    return WorklinkConfig.load(path)


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
    config_path = _worklink_config_path(home)
    try:
        config = _load_config(config_path)
    except Exception as exc:  # noqa: BLE001 - bad local config is a poller runtime error.
        _log(f"failed to load {config_path}: {exc}")
        return 1
    if config is None or not config.tool_pins:
        return 0

    inventory = inventory_tool_pins(config.tool_pins, _resolvers())
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
