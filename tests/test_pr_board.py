from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mimir import pr_board


@pytest.fixture(autouse=True)
def _clear_pr_board_cache():
    pr_board.clear_pr_board_cache()
    yield
    pr_board.clear_pr_board_cache()


def test_derive_github_repo_from_supported_origin_urls():
    assert pr_board._derive_github_repo("git@github.com:owner/repo.git") == "owner/repo"
    assert pr_board._derive_github_repo("https://github.com/owner/repo.git") == "owner/repo"
    assert pr_board._derive_github_repo("ssh://git@github.com/owner/repo.git") == "owner/repo"
    assert pr_board._derive_github_repo("https://gitlab.com/owner/repo.git") is None


@pytest.mark.asyncio
async def test_pr_board_fetches_open_prs_from_home_origin(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:owner/home.git\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            payload = [
                {
                    "number": 12,
                    "title": "Proposal PR",
                    "url": "https://github.com/owner/home/pull/12",
                    "author": {"login": "agent"},
                    "createdAt": "2026-06-27T12:00:00Z",
                    "reviewDecision": "REVIEW_REQUIRED",
                    "isDraft": False,
                }
            ]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = await pr_board.build_pr_board_payload(tmp_path)

    assert payload["available"] is True
    assert payload["repo"] == "owner/home"
    assert payload["pull_requests"] == [
        {
            "number": 12,
            "title": "Proposal PR",
            "url": "https://github.com/owner/home/pull/12",
            "author": "agent",
            "created_at": "2026-06-27T12:00:00Z",
            "review_decision": "REVIEW_REQUIRED",
            "is_draft": False,
        }
    ]
    assert [
        "gh", "pr", "list",
        "-R", "owner/home",
        "--state", "open",
        "--json", "number,title,url,author,createdAt,reviewDecision,isDraft",
    ] in calls


@pytest.mark.asyncio
async def test_pr_board_caches_for_short_ttl(tmp_path: Path, monkeypatch):
    call_count = 0

    def fake_run(args, **kwargs):
        nonlocal call_count
        call_count += 1
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/home.git", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = await pr_board.build_pr_board_payload(tmp_path)
    second = await pr_board.build_pr_board_payload(tmp_path)

    assert first is second
    assert call_count == 2


@pytest.mark.asyncio
async def test_pr_board_handles_missing_gh(tmp_path: Path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/home.git", stderr="")
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = await pr_board.build_pr_board_payload(tmp_path)

    assert payload["available"] is False
    assert payload["repo"] == "owner/home"
    assert "gh CLI not on PATH" in payload["error"]
    assert payload["pull_requests"] == []


@pytest.mark.asyncio
async def test_pr_board_handles_gh_auth_or_network_failure(tmp_path: Path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/home.git", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 401: Bad credentials")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = await pr_board.build_pr_board_payload(tmp_path)

    assert payload["available"] is False
    assert payload["repo"] == "owner/home"
    assert "Bad credentials" in payload["error"]


@pytest.mark.asyncio
async def test_pr_board_handles_unparseable_remote(tmp_path: Path, monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="file:///tmp/repo.git", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = await pr_board.build_pr_board_payload(tmp_path)

    assert payload["available"] is False
    assert "parseable GitHub repo" in payload["error"]


@pytest.mark.asyncio
async def test_pr_board_handles_garbled_json(tmp_path: Path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/home.git", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="{not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = await pr_board.build_pr_board_payload(tmp_path)

    assert payload["available"] is False
    assert payload["repo"] == "owner/home"
    assert "gh output" in payload["error"]
