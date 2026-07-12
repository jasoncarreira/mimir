#!/usr/bin/env python3
"""Dependency advisory poller — wraps scanner.py for poller contract.

This module provides the poller interface expected by the framework,
delegating the actual scanning logic to scanner.py.

Cursor semantics:
- First successful run: seeds current advisory IDs without waking the agent
- Later runs: emit one event for each newly matched advisory
- Repeated IDs: silent (no events emitted)
- Resolved advisories: when an ID disappears from scan, it's removed from cursor
- Reappearing advisories: if a previously seen ID reappears, it's emitted as new
- Cursor updates: atomic, only after fully successful scan
- Failure: leaves prior cursor unchanged, observable through non-zero exit
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import scanner

POLLER_NAME = "dependency-advisory-watch"
CURSOR_FILE = "dependency-advisory-cursor.json"


def _cursor_path() -> Path | None:
    state_dir = os.environ.get("STATE_DIR")
    if not state_dir:
        return None
    return Path(state_dir) / CURSOR_FILE


def _read_cursor(path: Path | None) -> tuple[str, ...] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        ids = data.get("advisory_ids")
        if not isinstance(ids, list):
            return None
        return tuple(str(i) for i in ids)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cursor(path: Path, advisory_ids: tuple[str, ...]) -> None:
    temp = path.with_suffix(".tmp")
    content = json.dumps(
        {"advisory_ids": sorted(advisory_ids), "version": 1},
        sort_keys=True,
    )
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _advisory_id(advisory: scanner.Advisory) -> str:
    return advisory.advisory_url.split("/")[-1] if advisory.advisory_url else ""


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def _run_scan_with_cursor() -> tuple[int, tuple[str, ...] | None]:
    """Run the scanner and return (exit_code, advisory_ids_or_error).

    Returns:
        - On success: (0, tuple of advisory IDs)
        - On failure: (non-zero, None)
    """
    cursor_path = _cursor_path()
    can_persist = cursor_path is not None
    previous_ids = _read_cursor(cursor_path) if can_persist else None

    advisories: list[scanner.Advisory] = []
    try:
        lockfiles = scanner.find_lockfiles(scanner.ROOT_DIR)
        for lockfile in lockfiles:
            report = scanner.run_osv_scanner(lockfile)
            advisories.extend(scanner.extract_advisories(report))
    except (OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return (2, None)

    current_ids = tuple(sorted(set(_advisory_id(a) for a in advisories if _advisory_id(a))))

    if previous_ids is None:
        if can_persist:
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            _write_cursor(cursor_path, current_ids)
        else:
            for advisory in advisories:
                aid = _advisory_id(advisory)
                if not aid:
                    continue
                _emit({
                    "poller": POLLER_NAME,
                    "event_type": "dependency_advisory",
                    "advisory_id": aid,
                    "package": advisory.package,
                    "current_version": advisory.current_version,
                    "affected_range": advisory.affected_range,
                    "severity": advisory.severity,
                    "advisory_url": advisory.advisory_url,
                    "remediation_version": advisory.remediation_version,
                    "prompt": (
                        f"Dependency advisory for {advisory.package}@{advisory.current_version}: "
                        f"affected {advisory.affected_range}, severity {advisory.severity}. "
                        f"See {advisory.advisory_url}"
                        + (f". Fixed in {advisory.remediation_version}" if advisory.remediation_version else ".")
                    ),
                })
        return (0, current_ids)

    new_ids = set(current_ids) - set(previous_ids)
    for advisory in advisories:
        aid = _advisory_id(advisory)
        if aid not in new_ids:
            continue
        _emit({
            "poller": POLLER_NAME,
            "event_type": "dependency_advisory",
            "advisory_id": aid,
            "package": advisory.package,
            "current_version": advisory.current_version,
            "affected_range": advisory.affected_range,
            "severity": advisory.severity,
            "advisory_url": advisory.advisory_url,
            "remediation_version": advisory.remediation_version,
            "prompt": (
                f"Dependency advisory for {advisory.package}@{advisory.current_version}: "
                f"affected {advisory.affected_range}, severity {advisory.severity}. "
                f"See {advisory.advisory_url}"
                + (f". Fixed in {advisory.remediation_version}" if advisory.remediation_version else ".")
            ),
        })

    if can_persist:
        _write_cursor(cursor_path, current_ids)

    return (0, current_ids)


def main() -> int:
    exit_code, _ = _run_scan_with_cursor()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
