"""Cultivate's PR-board data provider: remote parsing and the short cache.

Restores the coverage the retired ``tests/test_pr_board.py`` carried for core
``mimir.pr_board``; the HTTP-level failure and normalization cases live in
``test_web_api.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mimir_cultivate import board


@pytest.fixture(autouse=True)
def _own_board_cache(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Give each test its own copy of the board's module cache."""
    cache: dict = {}
    monkeypatch.setattr(board, "_cache", cache)
    return cache


@pytest.mark.parametrize(("remote", "repo"), [
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo", "owner/repo"),
    ("github.com:owner/repo.git\n", "owner/repo"),
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("https://gitlab.com/owner/repo.git", None),
    ("git@gitlab.com:owner/repo.git", None),
    ("https://github.com.evil.example/owner/repo.git", None),
    ("https://github.com/owner", None),
    ("https://github.com/owner/repo/extra", None),
    ("file:///tmp/repo.git", None),
    ("", None),
])
def test_github_repo_parses_scp_ssh_and_https_origins_only(remote: str, repo: str | None) -> None:
    assert board._github_repo(remote) == repo


def _origin(remote: str, calls: list[list[str]]):
    def run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout=remote, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [
    "https://gitlab.com/owner/repo.git", "file:///tmp/repo.git", "not a url",
])
async def test_non_github_or_unparseable_origin_is_unavailable_without_calling_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _origin(remote, calls))

    payload = await board.build_pr_board_payload(tmp_path)

    assert payload["available"] is False
    assert payload["repo"] is None
    assert payload["error"] == "origin remote is not a parseable GitHub repo"
    assert payload["pull_requests"] == []
    assert [call[0] for call in calls] == ["git"]


@pytest.mark.asyncio
async def test_missing_home_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("no command may run without a home")

    monkeypatch.setattr(subprocess, "run", unexpected)
    payload = await board.build_pr_board_payload(None)
    assert payload["available"] is False
    assert payload["error"] == "home path not configured"


@pytest.mark.asyncio
async def test_payload_is_cached_per_home_for_ninety_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _origin("https://github.com/owner/home.git", calls))
    now = [1000.0]
    monkeypatch.setattr(board.time, "monotonic", lambda: now[0])

    first = await board.build_pr_board_payload(tmp_path)
    assert first["available"] is True and first["repo"] == "owner/home"
    assert len(calls) == 2  # git remote + gh pr list

    now[0] += 89.9
    assert await board.build_pr_board_payload(tmp_path) is first
    assert len(calls) == 2

    other = tmp_path / "other"
    other.mkdir()
    assert await board.build_pr_board_payload(other) is not first
    assert len(calls) == 4

    now[0] += 0.2  # past the 90 s expiry of the first entry
    refreshed = await board.build_pr_board_payload(tmp_path)
    assert refreshed is not first
    assert len(calls) == 6
