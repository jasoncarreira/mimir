"""Cached GitHub pull request board payloads for the Ops dashboard."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PR_BOARD_TTL_SECONDS = 90.0
PR_BOARD_TIMEOUT_SECONDS = 8.0
PR_BOARD_MAX_PRS = 100

_REMOTE_SCP_RE = re.compile(r"^(?:[^@]+@)?github\.com:(?P<path>[^/]+/[^/]+?)(?:\.git)?/?$")


@dataclass
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any]


_CACHE: dict[Path, _CacheEntry] = {}
_CACHE_LOCK = asyncio.Lock()


def _empty_payload(error: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "error": error,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": None,
        "pull_requests": [],
        "truncated": False,
        "total_count": 0,
    }


def _derive_github_repo(remote: str) -> str | None:
    remote = remote.strip()
    if not remote:
        return None

    scp_match = _REMOTE_SCP_RE.match(remote)
    if scp_match:
        return scp_match.group("path").removesuffix(".git")

    parsed = urlparse(remote)
    if parsed.hostname != "github.com":
        return None
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _author_login(value: Any) -> str:
    if isinstance(value, dict):
        login = value.get("login")
        return login if isinstance(login, str) else ""
    return ""


def _normalize_pull_requests(value: Any) -> tuple[list[dict[str, Any]], int, bool] | None:
    if not isinstance(value, list):
        return None
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        rows.append({
            "number": number,
            "title": item.get("title") if isinstance(item.get("title"), str) else f"PR #{number}",
            "url": item.get("url") if isinstance(item.get("url"), str) else "",
            "author": _author_login(item.get("author")),
            "created_at": item.get("createdAt") if isinstance(item.get("createdAt"), str) else "",
            "review_decision": item.get("reviewDecision") if isinstance(item.get("reviewDecision"), str) else "",
            "is_draft": item.get("isDraft") if isinstance(item.get("isDraft"), bool) else False,
        })
    total_count = len(rows)
    truncated = total_count > PR_BOARD_MAX_PRS
    return rows[:PR_BOARD_MAX_PRS], total_count, truncated


def _load_pr_board_uncached(home: Path) -> dict[str, Any]:
    try:
        remote_proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=home,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PR_BOARD_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _empty_payload("git CLI not on PATH")
    except subprocess.TimeoutExpired:
        return _empty_payload(f"git remote timed out after {PR_BOARD_TIMEOUT_SECONDS}s")
    except Exception as exc:  # noqa: BLE001
        return _empty_payload(str(exc)[:500])

    if remote_proc.returncode != 0:
        error = (remote_proc.stderr or "").strip()
        return _empty_payload(error[:500] or f"git remote exit code {remote_proc.returncode}")

    repo = _derive_github_repo(remote_proc.stdout)
    if repo is None:
        return _empty_payload("origin remote is not a parseable GitHub repo")

    try:
        gh_proc = subprocess.run(
            [
                "gh", "pr", "list",
                "-R", repo,
                "--state", "open",
                "--json", "number,title,url,author,createdAt,reviewDecision,isDraft",
            ],
            cwd=home,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PR_BOARD_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _empty_payload("gh CLI not on PATH") | {"repo": repo}
    except subprocess.TimeoutExpired:
        return _empty_payload(f"gh pr list timed out after {PR_BOARD_TIMEOUT_SECONDS}s") | {"repo": repo}
    except Exception as exc:  # noqa: BLE001
        return _empty_payload(str(exc)[:500]) | {"repo": repo}

    if gh_proc.returncode != 0:
        error = (gh_proc.stderr or "").strip()
        return _empty_payload(error[:500] or f"gh pr list exit code {gh_proc.returncode}") | {"repo": repo}

    try:
        normalized = _normalize_pull_requests(json.loads(gh_proc.stdout))
    except json.JSONDecodeError as exc:
        return _empty_payload(f"gh output: {exc}") | {"repo": repo}
    if normalized is None:
        return _empty_payload("gh returned non-list payload") | {"repo": repo}

    pull_requests, total_count, truncated = normalized
    return {
        "available": True,
        "error": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pull_requests": pull_requests,
        "truncated": truncated,
        "total_count": total_count,
    }


async def build_pr_board_payload(home: Path | None) -> dict[str, Any]:
    if home is None:
        return _empty_payload("home path not configured")

    cache_key = home.resolve()
    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached.expires_at > now:
            return cached.payload

    payload = await asyncio.to_thread(_load_pr_board_uncached, cache_key)

    async with _CACHE_LOCK:
        _CACHE[cache_key] = _CacheEntry(
            expires_at=time.monotonic() + PR_BOARD_TTL_SECONDS,
            payload=payload,
        )
    return payload


def clear_pr_board_cache() -> None:
    _CACHE.clear()


__all__ = [
    "build_pr_board_payload",
    "clear_pr_board_cache",
]
