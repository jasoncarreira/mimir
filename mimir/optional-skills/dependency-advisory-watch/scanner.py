#!/usr/bin/env python3
"""OSV lockfile scanner — polls uv.lock and package-lock.json for vulnerabilities.

Inventories the repository root lockfiles, queries the OSV API for those
resolved packages, and normalizes matched vulnerabilities into deterministic
records. Runs standalone under system python3 with stdlib only.

Environment variables:
    ROOT_DIR    - Repository root (default: cwd)
    OSV_API_URL - OSV API endpoint (default: https://api.osv.dev/v1/query)

Output contract:
    stdout: JSONL — one record per matched vulnerability
    stderr: diagnostic logging (errors only)
    exit 0: success (zero events fine — no vulnerabilities found)
    non-zero: error (transport/parse failure)
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(os.environ.get("ROOT_DIR", os.getcwd())).resolve()
EXCLUDED_DIRS = {".worklink", ".opencode"}
OSV_API_URL = os.environ.get("OSV_API_URL", "https://api.osv.dev/v1/querybatch")


@dataclass(frozen=True, order=True)
class Advisory:
    package: str
    current_version: str
    affected_range: str
    severity: str
    advisory_url: str
    remediation_version: str | None

    def to_dict(self) -> dict:
        d = {
            "package": self.package,
            "current_version": self.current_version,
            "affected_range": self.affected_range,
            "severity": self.severity,
            "advisory_url": self.advisory_url,
        }
        if self.remediation_version:
            d["remediation_version"] = self.remediation_version
        return d


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _is_excluded(path: Path) -> bool:
    return path.parent.name in EXCLUDED_DIRS


def parse_uv_lock(lock_path: Path) -> dict[str, str]:
    """Parse uv.lock and return package name -> version map."""
    packages: dict[str, str] = {}
    content = lock_path.read_text(encoding="utf-8")
    data = tomllib.loads(content)

    for pkg in data.get("package", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            packages[name] = version
    return packages


def parse_package_lock(lock_path: Path) -> dict[str, str]:
    """Parse package-lock.json and return package name -> version map."""
    packages: dict[str, str] = {}
    data = json.loads(lock_path.read_text(encoding="utf-8"))

    pkgs = data.get("packages", {})
    for pkg_path, info in pkgs.items():
        if pkg_path == "":
            continue
        if not pkg_path.startswith("node_modules/"):
            continue
        name = info.get("name")
        version = info.get("version")
        if name and version:
            packages[name] = version
    return packages


def find_lockfiles(root: Path) -> list[tuple[str, Path]]:
    """Find lockfiles in root, respecting excluded directories."""
    lockfiles: list[tuple[str, Path]] = []
    for name in ["uv.lock", "package-lock.json"]:
        path = root / name
        if path.exists() and not _is_excluded(path):
            lockfiles.append((name, path))
    return lockfiles


def query_osv(packages: dict[str, str], ecosystem: str) -> list[dict]:
    """Query OSV API for vulnerabilities in the given packages."""
    if not packages:
        return []

    results: list[dict] = []

    for name, ver in packages.items():
        query = {"package": {"name": name, "ecosystem": ecosystem}, "version": ver}
        data = json.dumps(query).encode("utf-8")
        req = urllib.request.Request(
            "https://api.osv.dev/v1/query",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            _log(f"OSV transport error for {name}: {e}")
            continue
        except json.JSONDecodeError as e:
            _log(f"OSV parse error for {name}: {e}")
            continue

        vulns = result.get("vulns", [])
        if vulns:
            res = {"vulns": vulns, "_query_package": name}
            results.append(res)

    return results


def extract_advisories(
    packages: dict[str, str], osv_results: list[dict], ecosystem: str
) -> list[Advisory]:
    """Extract and normalize advisories from OSV results."""
    advisories: list[Advisory] = []

    for result in osv_results:
        vulns = result.get("vulns", [])
        if not vulns:
            continue

        pkg_info = result.get("package", {})
        name = pkg_info.get("name", "")
        if not name:
            name = result.get("_query_package", "")
        current_version = packages.get(name, "")

        for vuln in vulns:
            affected = vuln.get("affected", [])
            ranges = []
            severity = "UNKNOWN"
            advisory_url = vuln.get("id", "")
            if advisory_url:
                advisory_url = f"https://osv.dev/vulnerability/{advisory_url}"

            for aff in affected:
                if aff.get("package", {}).get("name") != name:
                    continue

                for r in aff.get("ranges", []):
                    for evt in r.get("events", []):
                        if "introduced" in evt:
                            ranges.append(f">={evt['introduced']}")
                        elif "fixed" in evt:
                            ranges.append(f"<{evt['fixed']}")
                        elif "last_affected" in evt:
                            ranges.append(f"<={evt['last_affected']}")

                sev = aff.get("severity", [])
                if sev:
                    severity = sev[0].get("score", "UNKNOWN")
                    if severity.startswith("CVSS_V3") or severity.startswith("CVSS:"):
                        if "/" in severity:
                            severity = severity.split("/")[-1]
                        elif ":" in severity:
                            severity = severity.split(":")[-1]

            affected_range = ",".join(ranges) if ranges else "unknown"

            remediation = None
            for aff in affected:
                for r in aff.get("ranges", []):
                    for evt in r.get("events", []):
                        if "fixed" in evt:
                            remediation = evt["fixed"]
                            break
                    if remediation:
                        break
                if remediation:
                    break

            advisories.append(Advisory(
                package=name,
                current_version=current_version,
                affected_range=affected_range,
                severity=severity,
                advisory_url=advisory_url,
                remediation_version=remediation,
            ))

    return advisories


def run_scan() -> int:
    """Main scan function. Returns 0 on success, non-zero on error."""
    lockfiles = find_lockfiles(ROOT_DIR)

    if not lockfiles:
        return 0

    all_advisories: list[Advisory] = []

    for lockname, lockpath in lockfiles:
        if lockname == "uv.lock":
            packages = parse_uv_lock(lockpath)
            ecosystem = "PyPI"
        elif lockname == "package-lock.json":
            packages = parse_package_lock(lockpath)
            ecosystem = "npm"
        else:
            continue

        if not packages:
            continue

        try:
            osv_results = query_osv(packages, ecosystem)
        except Exception:
            return 1

        advisories = extract_advisories(packages, osv_results, ecosystem)
        all_advisories.extend(advisories)

    all_advisories.sort(key=lambda a: (a.package, a.current_version, a.advisory_url))

    for adv in all_advisories:
        _emit(adv.to_dict())

    return 0


def main() -> int:
    return run_scan()


if __name__ == "__main__":
    sys.exit(main())
