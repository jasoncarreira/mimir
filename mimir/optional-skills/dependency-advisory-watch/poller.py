#!/usr/bin/env python3
"""OSV lockfile scanner poller.

This file is intentionally standalone: optional pollers are launched as
``python3 poller.py`` in a scrubbed subprocess environment, not inside mimir's
venv/import path. Keep this script stdlib-only and do not import ``mimir.*``.

Inventories the repository root ``uv.lock`` and ``package-lock.json``, queries
the OSV API for those resolved Python and npm packages, and normalizes matched
vulnerabilities into deterministic records. Healthy paths are silent: no
lockfiles, no packages, or no vulnerabilities all exit 0 with no stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error
from typing import Sequence

POLLER_NAME = os.environ.get("POLLER_NAME", "dependency-advisory-watch")

SCOPED_DIRS = frozenset({".worklink", ".opencode", ".git", "node_modules", "__pycache__", ".pytest_cache"})

OSV_API_URL = "https://api.osv.dev/v1/query"


@dataclass(frozen=True)
class LockfilePackage:
    """A resolved package from a lockfile."""

    name: str
    version: str
    ecosystem: str


@dataclass(frozen=True)
class Vulnerability:
    """A normalized vulnerability record from OSV."""

    package_name: str
    package_version: str
    ecosystem: str
    affected_range: str
    severity: str | None
    advisory_url: str
    remediation_version: str | None

    def to_event(self) -> dict:
        event = {
            "poller": POLLER_NAME,
            "event_type": "dependency_advisory",
            "package_name": self.package_name,
            "package_version": self.package_version,
            "ecosystem": self.ecosystem,
            "affected_range": self.affected_range,
            "advisory_url": self.advisory_url,
        }
        if self.severity:
            event["severity"] = self.severity
        if self.remediation_version:
            event["remediation_version"] = self.remediation_version
        return event


def _log(message: str) -> None:
    _emit({"log": message})


def _emit(event: dict) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def _scan_uv_lock(lockfile_path: Path) -> Sequence[LockfilePackage]:
    packages: list[LockfilePackage] = []
    content = lockfile_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or not lines[0].strip().startswith("version = "):
        raise RuntimeError(f"invalid uv.lock format: missing version header")
    in_package = False
    current_name: str | None = None
    current_version: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[[package]]"):
            in_package = True
            current_name = None
            current_version = None
            continue
        if not in_package:
            continue
        if stripped.startswith("name = "):
            current_name = stripped[7:].strip('"')
            continue
        if stripped.startswith("version = "):
            current_version = stripped[10:].strip('"')
            continue
        if stripped.startswith("source = "):
            if current_name and current_version:
                packages.append(LockfilePackage(current_name, current_version, "PyPI"))
            in_package = False
            current_name = None
            current_version = None
            continue

    return tuple(packages)


def _scan_package_lock(lockfile_path: Path) -> Sequence[LockfilePackage]:
    packages: list[LockfilePackage] = []
    content = lockfile_path.read_text(encoding="utf-8")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError(f"failed to parse {lockfile_path}")

    packages_obj = data.get("packages", {})
    if not isinstance(packages_obj, dict):
        raise RuntimeError(f"unexpected packages format in {lockfile_path}")

    for pkg_key, pkg_data in packages_obj.items():
        if not isinstance(pkg_data, dict):
            continue
        version = pkg_data.get("version")
        if not version or not isinstance(version, str):
            continue
        name = pkg_data.get("name")
        if not name or not isinstance(name, str):
            continue
        packages.append(LockfilePackage(name, version, "npm"))

    return tuple(packages)


def _inventory_lockfiles(root: Path) -> Sequence[LockfilePackage]:
    packages: list[LockfilePackage] = []

    uv_lock = root / "uv.lock"
    if uv_lock.exists():
        try:
            packages.extend(_scan_uv_lock(uv_lock))
        except Exception as exc:
            raise RuntimeError(f"failed to scan uv.lock: {exc}")

    package_lock = root / "package-lock.json"
    if package_lock.exists():
        try:
            packages.extend(_scan_package_lock(package_lock))
        except Exception as exc:
            raise RuntimeError(f"failed to scan package-lock.json: {exc}")

    return tuple(sorted(packages, key=lambda p: (p.ecosystem, p.name, p.version)))


def _query_osv(packages: Sequence[LockfilePackage]) -> Sequence[Vulnerability]:
    vulnerabilities: list[Vulnerability] = []

    for pkg in packages:
        query = {
            "package": {
                "name": pkg.name,
                "ecosystem": pkg.ecosystem,
            },
            "version": pkg.version,
        }
        try:
            request = urllib.request.Request(
                OSV_API_URL,
                data=json.dumps(query).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"OSV API returned status {response.status}")
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OSV API request failed: {exc}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"failed to parse OSV response: {exc}")

        vulns = result.get("vulns", [])
        if not vulns:
            continue

        for vuln in vulns:
            advisory_id = vuln.get("id", "")
            advisory_url = f"https://osv.dev/vulnerability/{advisory_id}" if advisory_id else ""

            severity = None
            severity_data = vuln.get("severity", [])
            if isinstance(severity_data, list):
                for s in severity_data:
                    if isinstance(s, dict):
                        severity = s.get("score") or s.get("type")
                        if severity:
                            break

            affected = vuln.get("affected", [])
            affected_range = ""
            remediation_version = None
            if affected:
                first_aff = affected[0]
                ranges = first_aff.get("ranges", [])
                if ranges:
                    first_range = ranges[0]
                    events = first_range.get("events", [])
                    for event in events:
                        if "fixed" in event:
                            remediation_version = event["fixed"]
                        elif "lessThan" in event:
                            affected_range = f"<{event['lessThan']}"
                        elif "introduced" in event:
                            affected_range = f">={event['introduced']}"

            vulnerabilities.append(
                Vulnerability(
                    package_name=pkg.name,
                    package_version=pkg.version,
                    ecosystem=pkg.ecosystem,
                    affected_range=affected_range,
                    severity=severity,
                    advisory_url=advisory_url,
                    remediation_version=remediation_version,
                )
            )

    return tuple(
        sorted(
            vulnerabilities,
            key=lambda v: (v.package_name, v.package_version, v.advisory_url),
        )
    )


def _root() -> Path:
    raw = os.environ.get("MIMIR_SCAN_ROOT")
    if raw:
        return Path(raw)
    return Path.cwd()


def _is_in_scoped_dir(path: Path) -> bool:
    for scoped in SCOPED_DIRS:
        if path.name == scoped:
            return True
        for parent in path.parents:
            if parent.name == scoped:
                return True
    return False


def main() -> int:
    root = _root()

    if _is_in_scoped_dir(root):
        _emit(
            {
                "signal": "dependency_advisory_scan_skipped",
                "reason": f"root {root} is in scoped directory",
            }
        )
        return 0

    try:
        packages = _inventory_lockfiles(root)
    except Exception as exc:
        _log(f"lockfile inventory failed: {exc}")
        _emit(
            {
                "signal": "dependency_advisory_inventory_failed",
                "reason": str(exc),
            }
        )
        return 1

    if not packages:
        return 0

    try:
        vulnerabilities = _query_osv(packages)
    except Exception as exc:
        _log(f"OSV query failed: {exc}")
        _emit(
            {
                "signal": "dependency_advisory_osv_query_failed",
                "reason": str(exc),
            }
        )
        return 1

    for vuln in vulnerabilities:
        _emit(vuln.to_event())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
