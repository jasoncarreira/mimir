"""Chainlink side of tracker filing: search, create, arm and adoption checks."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..worklink.claims import claim_records_from_comments
from .record import (
    ExternalRecord,
    ExternalRecordError,
    parse_external_record,
    render_external_record,
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

READY_LABEL = "worklink:ready"
EPIC_LABEL = "worklink:epic"
LIFECYCLE_LABELS = frozenset(
    {
        "worklink:ready",
        "worklink:in-progress",
        "worklink:review",
        "worklink:blocked",
        "worklink:failed",
    }
)
_TIMEOUT_S = 60


class WorkUnavailable(RuntimeError):
    """Chainlink could not answer; callers must not act on a missing answer."""


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _issue_id(row: Mapping[str, Any]) -> int | None:
    ids = {_positive_int(row[key]) for key in ("id", "number") if key in row}
    if len(ids) != 1:
        return None
    return next(iter(ids))


class ChainlinkWork:
    """Files and arms Chainlink issues that carry an External record."""

    def __init__(
        self,
        bin: str | None = None,
        cwd: str | Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.bin = bin or os.environ.get("CHAINLINK_BIN") or "chainlink"
        self.cwd = Path(cwd) if cwd is not None else None
        self.runner = runner or self._default_runner

    def _default_runner(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=self.cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_S,
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner([self.bin, *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkUnavailable(f"chainlink {args[0]} {args[1]} did not run") from exc

    def _json(self, *args: str) -> Any:
        result = self._run(*args)
        if result.returncode != 0:
            raise WorkUnavailable(f"chainlink {args[0]} {args[1]} failed")
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise WorkUnavailable(f"chainlink {args[0]} {args[1]} returned invalid JSON") from exc

    def show(self, issue_id: int) -> dict[str, Any]:
        payload = self._json("issue", "show", str(issue_id), "--json")
        if not isinstance(payload, dict):
            raise WorkUnavailable("chainlink issue show returned an unexpected shape")
        return payload

    def find(self, external_ref: str) -> list[int]:
        """Return the ids of issues whose External record is exactly ``external_ref``."""

        rows = self._json("issue", "search", external_ref, "--json")
        if not isinstance(rows, list):
            raise WorkUnavailable("chainlink issue search returned an unexpected shape")
        found: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            issue_id = _issue_id(row)
            if issue_id is None:
                continue
            description = row.get("description")
            if not isinstance(description, str):
                shown = self.show(issue_id).get("description")
                description = shown if isinstance(shown, str) else ""
            try:
                record = parse_external_record(description)
            except ExternalRecordError:
                continue
            if record is not None and record.external_ref() == external_ref:
                found.add(issue_id)
        return sorted(found)

    def create(
        self,
        *,
        title: str,
        description: str,
        record: ExternalRecord,
        epic: bool = False,
    ) -> int | None:
        """Create a ready issue; the id is None when the CLI did not report one."""

        labels = [READY_LABEL, *([EPIC_LABEL] if epic else [])]
        argv = [
            "issue",
            "create",
            title,
            "--description",
            f"{description}\n\n{render_external_record(record)}",
        ]
        for label in labels:
            argv += ["--label", label]
        result = self._run(*argv)
        if result.returncode != 0:
            raise WorkUnavailable("chainlink issue create failed")
        return _created_issue_id(result.stdout or "")

    def arm(self, issue_id: int) -> None:
        result = self._run("issue", "label", str(issue_id), READY_LABEL)
        if result.returncode != 0:
            raise WorkUnavailable("chainlink issue label failed")

    def never_armed(self, item: Mapping[str, Any]) -> bool:
        """True only for an open issue with no lifecycle label and no claim record.

        An unreadable or malformed snapshot is never treated as unarmed.
        """

        status = item.get("status")
        if not isinstance(status, str) or status.casefold() != "open":
            return False
        labels = _labels(item.get("labels"))
        comments = _comments(item.get("comments"))
        if labels is None or comments is None:
            return False
        if labels & LIFECYCLE_LABELS:
            return False
        return not claim_records_from_comments(comments)


def _labels(raw: object) -> set[str] | None:
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return None
    labels: set[str] = set()
    for entry in raw:
        value = entry.get("name", entry.get("label")) if isinstance(entry, dict) else entry
        if not isinstance(value, str) or not value.strip():
            return None
        labels.add(value.strip())
    return labels


def _comments(raw: object) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    comments: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            comments.append(entry)
        elif isinstance(entry, dict):
            text = entry.get("content") or entry.get("text") or entry.get("body") or ""
            comments.append(str(text))
        else:
            return None
    return comments


def _created_issue_id(stdout: str) -> int | None:
    for token in stdout.replace("#", " #").split():
        if token.startswith("#") and token[1:].isdigit():
            return _positive_int(int(token[1:]))
    return None
