#!/usr/bin/env python3
"""Run the pinned osv-scanner CLI over repository lockfiles.

stdout is deterministic JSONL (one normalized advisory per line).  Exit 0 means
the scan completed, including when vulnerabilities were found; scanner or JSON
failures return non-zero so the poller cannot report a false clean result.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

ROOT_DIR = Path(os.environ.get("ROOT_DIR", os.getcwd())).resolve()
OSV_SCANNER = os.environ.get("OSV_SCANNER", "osv-scanner")
LOCKFILE_NAMES = ("uv.lock", "package-lock.json")


@dataclass(frozen=True, order=True)
class Advisory:
    package: str
    current_version: str
    affected_range: str
    severity: str
    advisory_url: str
    remediation_version: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {
            "package": self.package,
            "current_version": self.current_version,
            "affected_range": self.affected_range,
            "severity": self.severity,
            "advisory_url": self.advisory_url,
        }
        if self.remediation_version:
            result["remediation_version"] = self.remediation_version
        return result


def find_lockfiles(root: Path) -> list[Path]:
    """Return supported root lockfiles in stable order."""
    return [root / name for name in LOCKFILE_NAMES if (root / name).is_file()]


def run_osv_scanner(lockfile: Path) -> dict:
    """Run osv-scanner for one lockfile and parse its JSON report.

    osv-scanner uses exit 1 when findings exist.  Exit codes above 1 are real
    scanner failures and must remain failures rather than becoming clean scans.
    """
    proc = subprocess.run(
        [OSV_SCANNER, "scan", "--format", "json", "--lockfile", str(lockfile)],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"osv-scanner failed for {lockfile.name}: {detail}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid osv-scanner JSON for {lockfile.name}: {exc}") from exc


def _severity(vulnerability: dict) -> str:
    severities = vulnerability.get("severity") or []
    if not severities:
        return "UNKNOWN"
    score = str(severities[0].get("score", "UNKNOWN"))
    return score.rsplit("/", 1)[-1] if score.startswith("CVSS") else score


def _range_and_fix(vulnerability: dict, package_name: str) -> tuple[str, str | None]:
    bounds: list[str] = []
    fixed: str | None = None
    for affected in vulnerability.get("affected") or []:
        affected_name = (affected.get("package") or {}).get("name")
        if affected_name and affected_name != package_name:
            continue
        for version_range in affected.get("ranges") or []:
            for event in version_range.get("events") or []:
                if "introduced" in event:
                    bounds.append(f">={event['introduced']}")
                elif "fixed" in event:
                    bounds.append(f"<{event['fixed']}")
                    fixed = fixed or str(event["fixed"])
                elif "last_affected" in event:
                    bounds.append(f"<={event['last_affected']}")
    return (",".join(bounds) or "unknown", fixed)


def extract_advisories(report: dict) -> list[Advisory]:
    """Normalize osv-scanner v2 ``results[].packages[]`` output."""
    advisories: list[Advisory] = []
    for result in report.get("results") or []:
        for package_result in result.get("packages") or []:
            package = package_result.get("package") or {}
            name = str(package.get("name", ""))
            version = str(package.get("version", ""))
            if not name:
                continue
            for vulnerability in package_result.get("vulnerabilities") or []:
                vuln_id = str(vulnerability.get("id", ""))
                affected_range, fixed = _range_and_fix(vulnerability, name)
                advisories.append(
                    Advisory(
                        package=name,
                        current_version=version,
                        affected_range=affected_range,
                        severity=_severity(vulnerability),
                        advisory_url=(
                            f"https://osv.dev/vulnerability/{vuln_id}" if vuln_id else ""
                        ),
                        remediation_version=fixed,
                    )
                )
    return advisories


def run_scan() -> int:
    try:
        advisories = [
            advisory
            for lockfile in find_lockfiles(ROOT_DIR)
            for advisory in extract_advisories(run_osv_scanner(lockfile))
        ]
    except (OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2

    for advisory in sorted(set(advisories)):
        print(json.dumps(advisory.to_dict(), sort_keys=True), flush=True)
    return 0


def main() -> int:
    return run_scan()


if __name__ == "__main__":
    raise SystemExit(main())
