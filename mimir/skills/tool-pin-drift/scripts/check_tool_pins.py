#!/usr/bin/env python3
"""Compare bundled tool pins with npm and GitHub release versions."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping, Sequence

TARGETS_PATH = Path(__file__).with_name("targets.json")
Runner = Callable[..., subprocess.CompletedProcess[str]]


def load_targets(path: Path = TARGETS_PATH) -> list[dict[str, str]]:
    """Load and validate the bundled target inventory."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("target list must be a JSON array")
    targets: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"target {index} must be an object")
        required = {"name", "source", "pinned_version"}
        source = item.get("source")
        if source not in {"npm", "github-release"}:
            raise ValueError(f"target {index} has unsupported source: {source}")
        locator = "package" if source == "npm" else "repo"
        missing = sorted((required | {locator}) - item.keys())
        if missing:
            raise ValueError(f"target {index} missing fields: {', '.join(missing)}")
        targets.append({str(key): str(value) for key, value in item.items()})
    return targets


def lookup_argv(target: Mapping[str, str]) -> list[str]:
    """Build the shell-free upstream lookup argv for one target."""

    if target["source"] == "npm":
        return ["npm", "view", target["package"], "version"]
    if target["source"] == "github-release":
        return [
            "gh", "release", "view", "--repo", target["repo"],
            "--json", "tagName,url",
        ]
    raise ValueError(f"unsupported source: {target['source']}")


def parse_latest_version(target: Mapping[str, str], payload: str) -> str:
    """Parse recorded npm or GitHub CLI output into a version string."""

    if target["source"] == "npm":
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        if not lines:
            raise ValueError("npm view returned no version")
        return lines[-1]
    if target["source"] == "github-release":
        try:
            release = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("gh release view returned invalid JSON") from exc
        if not isinstance(release, dict) or not str(release.get("tagName") or "").strip():
            raise ValueError("gh release view returned no tagName")
        return str(release["tagName"]).strip()
    raise ValueError(f"unsupported source: {target['source']}")


def classify(target: Mapping[str, str], latest: str) -> dict[str, object]:
    """Create one machine-readable drift result."""

    pinned = target["pinned_version"]
    drifted = pinned != latest
    return {
        "name": target["name"],
        "source": target["source"],
        "pinned_version": pinned,
        "latest_version": latest,
        "drifted": drifted,
        "status": "drift" if drifted else "no_drift",
        "error": None,
    }


def check_target(
    target: Mapping[str, str], *, runner: Runner = subprocess.run,
) -> dict[str, object]:
    """Check one target, converting every lookup failure into target data."""

    try:
        argv = lookup_argv(target)
        completed = runner(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail or f"lookup exited {completed.returncode}")
        return classify(target, parse_latest_version(target, completed.stdout))
    except Exception as exc:  # noqa: BLE001 - every upstream failure is per-target.
        return {
            "name": target.get("name", "unknown"),
            "source": target.get("source", "unknown"),
            "pinned_version": target.get("pinned_version"),
            "latest_version": None,
            "drifted": None,
            "status": "error",
            "error": str(exc) or type(exc).__name__,
        }


def check_targets(
    targets: Sequence[Mapping[str, str]], *, runner: Runner = subprocess.run,
) -> list[dict[str, object]]:
    """Check all targets without allowing one failure to stop the scan."""

    return [check_target(target, runner=runner) for target in targets]


def main() -> int:
    if len(sys.argv) != 1:
        print(json.dumps({"error": "this script accepts no arguments"}, sort_keys=True))
        return 2
    results = check_targets(load_targets())
    print(json.dumps({"targets": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
