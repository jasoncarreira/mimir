"""Bounded, read-only access to the operator's Chainlink issue tracker."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from ..worklink.autonomy import chainlink_bin

_MAX_OUTPUT = 131_072
_MAX_DESCRIPTION = 16_384
_MAX_COMMENT = 2_048
_MAX_COMMENTS = 5


def _excerpt(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


@tool
def chainlink_issue_show(issue_id: int) -> dict[str, Any]:
    """Read a bounded issue projection from the operator's Chainlink tracker by ID.

    This is a read-only query; it cannot create, label, or close issues.
    """
    if type(issue_id) is not int or issue_id < 1:
        return {"error": "invalid_issue_id"}
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home or not (Path(home) / ".chainlink").is_dir():
        return {"error": "chainlink_tracker_unavailable"}
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [chainlink_bin(), "issue", "show", str(issue_id), "--json"],
                cwd=home, stdout=output, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            if result.returncode != 0:
                return {"error": "issue_not_found", "issue_id": issue_id}
            if output.tell() > _MAX_OUTPUT:
                return {"error": "chainlink_read_failed"}
            output.seek(0)
            payload = json.load(output)
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return {"error": "chainlink_read_failed"}

    if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] != issue_id:
        return {"error": "chainlink_read_failed"}
    if not all(isinstance(payload.get(key), str) for key in ("title", "status", "description")):
        return {"error": "chainlink_read_failed"}
    labels, comments, parent = (payload.get(key) for key in ("labels", "comments", "parent_id"))
    if (not isinstance(labels, list) or not all(isinstance(label, str) for label in labels)
            or not isinstance(comments, list)
            or (parent is not None and (type(parent) is not int or parent < 1))):
        return {"error": "chainlink_read_failed"}
    recent = []
    for comment in comments[-_MAX_COMMENTS:]:
        if not isinstance(comment, dict):
            return {"error": "chainlink_read_failed"}
        body = comment.get("content", comment.get("body"))
        if not isinstance(body, str):
            return {"error": "chainlink_read_failed"}
        recent.append({
            "body": _excerpt(body, _MAX_COMMENT),
            "author": _excerpt(comment.get("author", ""), 128) if isinstance(comment.get("author", ""), str) else "",
            "created_at": _excerpt(comment.get("created_at", ""), 64) if isinstance(comment.get("created_at", ""), str) else "",
        })
    return {
        "id": issue_id,
        "title": _excerpt(payload["title"], 1_024),
        "status": _excerpt(payload["status"], 32),
        "labels": [_excerpt(label, 128) for label in labels[:32]],
        "parent": parent,
        "description_excerpt": _excerpt(payload["description"], _MAX_DESCRIPTION),
        "recent_comments": recent,
    }
