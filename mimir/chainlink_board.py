"""Read-only Chainlink/Worklink board payloads for the React dashboard."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHAINLINK_TIMEOUT_SECONDS = 5.0
CHAINLINK_MAX_ISSUES = 250
CHAINLINK_MAX_SHOWS = 120

_WORKLINK_EVIDENCE_RE = re.compile(r"^(\d+)-(\d+)\.json$")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if text.isdigit():
            return int(text)
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels")
    if isinstance(labels, list):
        return sorted(str(label) for label in labels if str(label))
    if isinstance(labels, str):
        return sorted(label.strip() for label in labels.split(",") if label.strip())
    return []


def _first_present(issue: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in issue:
            return issue[key]
    return None


def _issue_id(issue: dict[str, Any]) -> int | None:
    return _as_int(_first_present(issue, ("id", "number", "issue_id")))


def _parent_id(issue: dict[str, Any]) -> int | None:
    return _as_int(_first_present(issue, ("parent_id", "parent", "parent_issue_id")))


def _child_ids(issue: dict[str, Any]) -> list[int]:
    raw = _first_present(issue, ("subissues", "children", "child_ids", "subissue_ids"))
    out: list[int] = []
    for item in _as_list(raw):
        child_id = _issue_id(item) if isinstance(item, dict) else _as_int(item)
        if child_id is not None:
            out.append(child_id)
    return sorted(set(out))


def _edge_ids(issue: dict[str, Any], keys: tuple[str, ...]) -> list[int]:
    raw = _first_present(issue, keys)
    out: list[int] = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            edge_id = _issue_id(item) or _as_int(item.get("id"))
        else:
            edge_id = _as_int(item)
        if edge_id is not None:
            out.append(edge_id)
    return sorted(set(out))


def _comments(issue: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for index, raw in enumerate(_as_list(issue.get("comments"))):
        if isinstance(raw, str):
            out.append({"id": str(index), "author": "", "created_at": "", "body": raw})
            continue
        record = _as_record(raw)
        body = _as_text(
            _first_present(record, ("body", "text", "comment", "message", "content"))
        )
        if not body:
            continue
        out.append({
            "id": str(_first_present(record, ("id", "comment_id")) or index),
            "author": _as_text(_first_present(record, ("author", "user", "agent"))),
            "created_at": _as_text(_first_present(record, ("created_at", "timestamp", "ts"))),
            "body": body,
        })
    return out


def _lifecycle_status(issue: dict[str, Any]) -> str:
    labels = set(_labels(issue))
    raw_status = _as_text(issue.get("status")).lower()
    if raw_status in {"closed", "done"} or bool(issue.get("closed_at")):
        return "done"
    if "worklink:review" in labels or "review" in labels:
        return "review"
    if "worklink:in-progress" in labels or "in-progress" in labels:
        return "in-progress"
    if "worklink:blocked" in labels or "blocked" in labels:
        return "blocked"
    if "worklink:ready" in labels or "ready" in labels:
        return "ready"
    blocked_by = _edge_ids(issue, ("blocked_by", "blockers", "blocked_by_ids"))
    return "blocked" if blocked_by else "open"


def _priority(issue: dict[str, Any]) -> str:
    priority = _as_text(issue.get("priority")).lower()
    return priority or "normal"


def _summarize_issue(
    issue: dict[str, Any],
    *,
    children_by_parent: dict[int, list[int]],
    issues_by_id: dict[int, dict[str, Any]],
    worklink_by_issue: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    issue_id = _issue_id(issue)
    if issue_id is None:
        return None
    children = sorted(set(_child_ids(issue)) | set(children_by_parent.get(issue_id, [])))
    child_done = 0
    for child_id in children:
        child = issues_by_id.get(child_id)
        if child and _lifecycle_status(child) == "done":
            child_done += 1
    blocked_by = _edge_ids(issue, ("blocked_by", "blockers", "blocked_by_ids"))
    blocking = _edge_ids(issue, ("blocking", "blocks", "blocking_ids"))
    return {
        "id": issue_id,
        "title": _as_text(issue.get("title")) or f"Issue #{issue_id}",
        "status": _lifecycle_status(issue),
        "raw_status": _as_text(issue.get("status")) or "open",
        "priority": _priority(issue),
        "labels": _labels(issue),
        "parent_id": _parent_id(issue),
        "child_ids": children,
        "child_progress": {"done": child_done, "total": len(children)},
        "blocked_by": blocked_by,
        "blocking": blocking,
        "updated_at": _as_text(_first_present(issue, ("updated_at", "updated", "modified_at"))),
        "created_at": _as_text(_first_present(issue, ("created_at", "created"))),
        "description": _as_text(_first_present(issue, ("description", "body", "details"))),
        "comments": _comments(issue),
        "worklink": worklink_by_issue.get(issue_id),
    }


def _read_worklink_evidence(home: Path) -> dict[int, dict[str, Any]]:
    evidence_dir = home / "state" / "worklink" / "evidence"
    if not evidence_dir.is_dir():
        return {}
    latest: dict[int, dict[str, Any]] = {}
    for path in evidence_dir.glob("*.json"):
        match = _WORKLINK_EVIDENCE_RE.match(path.name)
        if not match:
            continue
        issue_id = int(match.group(1))
        attempt = int(match.group(2))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        prior = latest.get(issue_id)
        if prior and int(prior.get("attempt") or 0) > attempt:
            continue
        transcript = _as_text(payload.get("transcript"))
        evidence_rel = str(path.relative_to(home))
        latest[issue_id] = {
            "issue": issue_id,
            "attempt": attempt,
            "backend": _as_text(payload.get("backend")),
            "status": _as_text(payload.get("status")) or "unknown",
            "branch": _as_text(payload.get("branch")),
            "started_at": _as_text(payload.get("started_at")),
            "finished_at": _as_text(payload.get("finished_at")),
            "diff_stat": _as_text(payload.get("diff_stat")),
            "tests": payload.get("tests") if isinstance(payload.get("tests"), dict) else None,
            "pr_url": _as_text(payload.get("pr_url")),
            "blocked_reason": _as_text(payload.get("blocked_reason")),
            "transcript": transcript,
            "transcript_href": _artifact_href(transcript) if transcript else "",
            "evidence_path": evidence_rel,
            "evidence_href": _artifact_href(evidence_rel),
        }
    return latest


def _artifact_href(relative_path: str) -> str:
    return f"/api/v1/chainlink-board/artifact?path={relative_path}"


async def _run_chainlink_json(home: Path, args: list[str]) -> tuple[Any | None, str | None]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "chainlink",
            *args,
            cwd=str(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "chainlink CLI not on PATH"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:500]

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CHAINLINK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        return None, f"chainlink timed out after {CHAINLINK_TIMEOUT_SECONDS}s"

    if proc.returncode != 0:
        err_text = (stderr or b"").decode("utf-8", errors="replace").strip()
        return None, err_text[:500] or f"chainlink exit code {proc.returncode}"
    try:
        return json.loads(stdout.decode("utf-8", errors="replace")), None
    except json.JSONDecodeError as exc:
        return None, f"chainlink output: {exc}"


async def _load_issue_details(home: Path, ids: list[int]) -> dict[int, dict[str, Any]]:
    semaphore = asyncio.Semaphore(8)

    async def load(issue_id: int) -> tuple[int, dict[str, Any] | None]:
        async with semaphore:
            payload, error = await _run_chainlink_json(
                home, ["issue", "show", str(issue_id), "--json"],
            )
        if error or not isinstance(payload, dict):
            return issue_id, None
        return issue_id, payload

    pairs = await asyncio.gather(*(load(issue_id) for issue_id in ids[:CHAINLINK_MAX_SHOWS]))
    return {issue_id: detail for issue_id, detail in pairs if detail is not None}


async def build_chainlink_board_payload(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {
            "available": False,
            "error": "home path not configured",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "columns": [],
            "issues": [],
            "roots": [],
            "edges": [],
            "filters": {"labels": [], "statuses": [], "priorities": []},
            "truncated": False,
            "total_count": 0,
        }

    payload, error = await _run_chainlink_json(
        home, ["issue", "list", "--status", "all", "--json"],
    )
    if error:
        return {
            "available": False,
            "error": error,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "columns": [],
            "issues": [],
            "roots": [],
            "edges": [],
            "filters": {"labels": [], "statuses": [], "priorities": []},
            "truncated": False,
            "total_count": 0,
        }
    if not isinstance(payload, list):
        return {
            "available": False,
            "error": "chainlink returned non-list payload",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "columns": [],
            "issues": [],
            "roots": [],
            "edges": [],
            "filters": {"labels": [], "statuses": [], "priorities": []},
            "truncated": False,
            "total_count": 0,
        }

    raw_issues = [issue for issue in payload if isinstance(issue, dict)]
    total_count = len(raw_issues)
    raw_issues = raw_issues[:CHAINLINK_MAX_ISSUES]
    ids = [issue_id for issue in raw_issues if (issue_id := _issue_id(issue)) is not None]
    details = await _load_issue_details(home, ids)
    merged = []
    for issue in raw_issues:
        issue_id = _issue_id(issue)
        detail = details.get(issue_id or -1, {})
        merged.append({**issue, **detail})

    issues_by_id = {
        issue_id: issue
        for issue in merged
        if (issue_id := _issue_id(issue)) is not None
    }
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for issue in merged:
        issue_id = _issue_id(issue)
        parent_id = _parent_id(issue)
        if issue_id is not None and parent_id is not None:
            children_by_parent[parent_id].append(issue_id)

    worklink_by_issue = _read_worklink_evidence(home)
    summaries = [
        summary for issue in merged
        if (
            summary := _summarize_issue(
                issue,
                children_by_parent=children_by_parent,
                issues_by_id=issues_by_id,
                worklink_by_issue=worklink_by_issue,
            )
        ) is not None
    ]
    summaries.sort(key=lambda issue: (issue["status"] == "done", issue["priority"], issue["id"]))

    statuses = ["open", "ready", "blocked", "in-progress", "review", "done"]
    columns = [
        {
            "id": status,
            "title": status.replace("-", " ").title(),
            "issue_ids": [issue["id"] for issue in summaries if issue["status"] == status],
        }
        for status in statuses
    ]
    edges = []
    for issue in summaries:
        for blocker in issue["blocked_by"]:
            edges.append({"from": blocker, "to": issue["id"], "kind": "blocks"})
        for child in issue["child_ids"]:
            edges.append({"from": issue["id"], "to": child, "kind": "parent"})
    labels = sorted({label for issue in summaries for label in issue["labels"]})
    priorities = sorted({issue["priority"] for issue in summaries})

    return {
        "available": True,
        "error": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "columns": columns,
        "issues": summaries,
        "roots": [issue["id"] for issue in summaries if issue["parent_id"] is None],
        "edges": edges,
        "filters": {
            "labels": labels,
            "statuses": statuses,
            "priorities": priorities,
        },
        "truncated": total_count > len(raw_issues),
        "total_count": total_count,
    }


def resolve_worklink_artifact(home: Path, requested: str) -> Path | None:
    relative = requested.strip().lstrip("/")
    if not relative:
        return None
    root = (home / "state" / "worklink").resolve()
    path = (home / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None
