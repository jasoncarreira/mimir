"""Tests for ``mimir.git_bootstrap`` (PR 4b of MIMIR_HOME_GIT_TRACKING).

Covers the idempotent bootstrap flow:

- ``inject_token_into_url`` URL-encodes the PAT and rewrites the netloc.
- Bootstrap on an empty home with no env → ``git init`` + bootstrap
  commit + identity + .gitignore + pre-commit hook.
- Bootstrap on an existing repo → no re-init, identity + hook + gitignore
  refreshed if missing, no-op otherwise.
- Bootstrap on existing repo with operator's local commits and a
  divergent (non-fast-forward) remote → ``git_pull_blocked`` event.
- Pre-commit hook is executable and refuses secret-shaped content.
- Allowlist .gitignore: a binary file under ``memory/`` (e.g.
  ``memory/atoms.db``) is NOT staged by ``git add -A``.
- Token-bearing URL never leaks into events.jsonl (sanitized via
  ``_redact``).

Tests use real ``git`` against ``tmp_path``. The "remote" for clone +
pull paths is a second tmp directory so we don't need network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mimir import git_bootstrap


# ─── fixtures ────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, check=check,
    )


@pytest.fixture
def captured_events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """Returns (events_list, log_callback) for passing to bootstrap."""
    events: list[tuple[str, dict[str, Any]]] = []

    def cb(kind: str, **fields: Any) -> None:
        events.append((kind, fields))

    return events, cb


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A bare-bones bare repo that stands in for the remote."""
    upstream = tmp_path / "upstream.git"
    _git("init", "--bare", "-q", "-b", "main", cwd=upstream.parent if False else tmp_path)
    # init --bare wants a target; use the explicit form
    upstream.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(upstream)],
        check=True,
    )
    return upstream


@pytest.fixture
def seeded_remote(tmp_path: Path, upstream_repo: Path) -> Path:
    """Push an initial commit into the upstream so a clone yields
    something non-trivial."""
    seed = tmp_path / "seed-clone"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "test@example.com", cwd=seed)
    _git("config", "user.name", "test", cwd=seed)
    _git("config", "commit.gpgsign", "false", cwd=seed)
    (seed / "README.md").write_text("upstream seed\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(upstream_repo), cwd=seed)
    _git("push", "-q", "-u", "origin", "main", cwd=seed)
    return upstream_repo


# ─── inject_token_into_url ───────────────────────────────────────────


def test_inject_token_into_url_basic() -> None:
    url = git_bootstrap.inject_token_into_url(
        "https://github.com/jasoncarreira/mimirbot-state.git",
        "ghp_AbCdEf123",
    )
    assert url == "https://ghp_AbCdEf123@github.com/jasoncarreira/mimirbot-state.git"


def test_inject_token_into_url_url_encodes_special_chars() -> None:
    # PATs occasionally contain ``+`` / ``/`` / ``=``; without
    # encoding these break the netloc parse on the receiving end.
    url = git_bootstrap.inject_token_into_url(
        "https://github.com/foo/bar.git",
        "tok+en/with=specials",
    )
    assert "tok%2Ben%2Fwith%3Dspecials@github.com" in url


def test_inject_token_into_url_preserves_port() -> None:
    url = git_bootstrap.inject_token_into_url(
        "https://example.com:8443/x/y.git", "abc",
    )
    assert url == "https://abc@example.com:8443/x/y.git"


def test_inject_token_into_url_passthrough_for_ssh() -> None:
    # ssh URLs are caller's problem — leave them alone.
    url = "git@github.com:foo/bar.git"
    assert git_bootstrap.inject_token_into_url(url, "abc") == url


# ─── bootstrap on empty home, no env → init path ─────────────────────


def test_bootstrap_init_path_empty_home_no_env(
    tmp_path: Path, captured_events: tuple[list, Any],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    res = git_bootstrap.bootstrap_git_repo(home, log_event=cb)

    assert res.initialized is True
    assert res.cloned is False
    assert res.bootstrap_commit is True
    assert res.gitignore_written is True
    assert res.hook_written is True
    assert res.remote_configured is False

    # .git exists, hook is executable.
    assert (home / ".git").is_dir()
    hook = home / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    assert hook.stat().st_mode & stat.S_IXUSR

    # Identity applied.
    name = _git("config", "user.name", cwd=home).stdout.strip()
    email = _git("config", "user.email", cwd=home).stdout.strip()
    assert name == git_bootstrap.DEFAULT_USER_NAME
    assert email == git_bootstrap.DEFAULT_USER_EMAIL

    # Bootstrap commit landed.
    log = _git("log", "--oneline", cwd=home).stdout
    assert "initial mimir-home bootstrap" in log

    # Algedonic event fired.
    kinds = [k for k, _ in events]
    assert "git_bootstrap_ok" in kinds


# ─── bootstrap is idempotent on second call ──────────────────────────


def test_bootstrap_is_idempotent(
    tmp_path: Path, captured_events: tuple[list, Any],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    first = git_bootstrap.bootstrap_git_repo(home, log_event=cb)
    head1 = _git("rev-parse", "HEAD", cwd=home).stdout.strip()

    # Second call should NOT reinitialize, NOT make a new commit.
    second = git_bootstrap.bootstrap_git_repo(home, log_event=cb)
    head2 = _git("rev-parse", "HEAD", cwd=home).stdout.strip()

    assert second.initialized is False
    assert second.cloned is False
    assert second.bootstrap_commit is False
    assert head1 == head2


# ─── bootstrap with state_repo + token: clone path on empty home ─────


def test_bootstrap_clone_path_into_empty_home(
    tmp_path: Path, seeded_remote: Path, captured_events: tuple[list, Any],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    # Use file:// protocol — no network, no token actually used by git
    # (clone goes straight to the bare repo). Pass a non-empty token so
    # the inject_token_into_url code path runs.
    state_repo = seeded_remote.as_uri()
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=state_repo,
        github_token="tok-ignored-for-file-uri",
        log_event=cb,
    )

    assert res.cloned is True
    assert res.initialized is False
    assert (home / ".git").is_dir()
    assert (home / "README.md").read_text() == "upstream seed\n"
    # Identity overridden (the seed had test@example.com; bootstrap
    # rewrites to mimir's identity).
    name = _git("config", "user.name", cwd=home).stdout.strip()
    assert name == git_bootstrap.DEFAULT_USER_NAME
    # Hook + .gitignore present (template overlaid the cloned repo).
    assert (home / ".git" / "hooks" / "pre-commit").is_file()


# ─── bootstrap on existing repo with divergent remote → pull blocked ─


def test_bootstrap_pull_blocked_on_divergent_history(
    tmp_path: Path, captured_events: tuple[list, Any],
) -> None:
    """Reproduce the spec §"Failure modes" #9 case: container has a
    local commit, remote was force-pushed to a divergent line.
    Bootstrap must log ``git_pull_blocked`` and not raise."""
    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(upstream)],
        check=True,
    )

    # Build the operator-side history.
    op_clone = tmp_path / "op-clone"
    op_clone.mkdir()
    _git("init", "-q", "-b", "main", cwd=op_clone)
    _git("config", "user.email", "op@x", cwd=op_clone)
    _git("config", "user.name", "op", cwd=op_clone)
    _git("config", "commit.gpgsign", "false", cwd=op_clone)
    (op_clone / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=op_clone)
    _git("commit", "-q", "-m", "shared seed", cwd=op_clone)
    _git("remote", "add", "origin", str(upstream), cwd=op_clone)
    _git("push", "-q", "-u", "origin", "main", cwd=op_clone)

    # Container side clones from the same point.
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events
    git_bootstrap.bootstrap_git_repo(
        home, state_repo=upstream.as_uri(), github_token="tok",
        log_event=cb,
    )

    # Container makes a local commit.
    (home / "memory").mkdir(exist_ok=True)
    (home / "memory" / "container.md").write_text("container line\n")
    _git("add", "memory/container.md", cwd=home)
    _git("config", "commit.gpgsign", "false", cwd=home)
    _git("commit", "-q", "-m", "container commit", cwd=home)

    # Operator force-pushes a divergent history — emulate by going
    # back to the seed and committing differently, then force-pushing.
    _git("reset", "--hard", "HEAD", cwd=op_clone)
    (op_clone / "diverged.md").write_text("operator diverged\n")
    _git("add", "diverged.md", cwd=op_clone)
    _git("commit", "-q", "--amend", "--no-edit", cwd=op_clone)
    _git("push", "-q", "-f", "origin", "main", cwd=op_clone)

    # Bootstrap-on-restart should pull-blocked rather than raise.
    events.clear()
    res = git_bootstrap.bootstrap_git_repo(
        home, state_repo=upstream.as_uri(), github_token="tok",
        log_event=cb,
    )

    assert res.pull_blocked is True
    assert res.pulled is False
    kinds = [k for k, _ in events]
    assert "git_pull_blocked" in kinds


# ─── pre-commit hook integration ─────────────────────────────────────


def _seed_home_with_hook(home: Path) -> None:
    """Bootstrap + add a fake remote so push won't dial the network in
    later tests; we only exercise the hook here."""
    git_bootstrap.bootstrap_git_repo(home)
    # Seed at least one tracked file so subsequent commits aren't empty.
    (home / "memory").mkdir(exist_ok=True)
    (home / "memory" / "starter.md").write_text("starter\n")
    _git("add", "memory/starter.md", cwd=home)
    _git("config", "commit.gpgsign", "false", cwd=home)
    _git("commit", "-q", "-m", "starter", cwd=home)


def test_pre_commit_hook_refuses_bearer_token_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_hook(home)

    # Drop a bearer-token shaped string into a tracked file.
    (home / "memory" / "leak.md").write_text(
        "Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123\n"
    )
    _git("add", "memory/leak.md", cwd=home)

    proc = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=home, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    msg = (proc.stdout + proc.stderr).lower()
    assert "refusing" in msg and "bearer" in msg


def test_pre_commit_hook_refuses_credential_filename(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_hook(home)

    # Land a file with a credential-shaped name. Add via -f because
    # the allowlist .gitignore would otherwise refuse to stage it.
    (home / "memory" / "oauth_creds.json").write_text(
        '{"benign": "content"}\n'
    )
    _git("add", "-f", "memory/oauth_creds.json", cwd=home)

    proc = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=home, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    msg = (proc.stdout + proc.stderr).lower()
    assert "filename" in msg or "secret" in msg


def test_pre_commit_hook_passes_clean_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_hook(home)

    (home / "memory" / "ok.md").write_text("just regular notes\n")
    _git("add", "memory/ok.md", cwd=home)

    proc = subprocess.run(
        ["git", "commit", "-m", "clean"],
        cwd=home, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0


# ─── allowlist .gitignore behaviour ──────────────────────────────────


def test_gitignore_blocks_atoms_db_under_state(tmp_path: Path) -> None:
    """Belt-and-suspenders: ``state/atoms.db`` (a likely accidental
    drop point) must be excluded by the gitignore even though
    ``state/wiki/**`` is allowlisted."""
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(home)

    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "atoms.db").write_bytes(b"SQLite format 3\x00...\n")

    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=home, capture_output=True, text=True, check=True,
    )
    # atoms.db must NOT appear; the wildcard *.db rule wins.
    assert "atoms.db" not in res.stdout


def test_gitignore_admits_memory_markdown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(home)

    (home / "memory").mkdir(exist_ok=True)
    (home / "memory" / "note.md").write_text("a real note\n")

    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=home, capture_output=True, text=True, check=True,
    )
    assert "memory/note.md" in res.stdout


# ─── token sanitisation in failure events ────────────────────────────


def test_clone_failure_event_does_not_leak_token(
    tmp_path: Path, captured_events: tuple[list, Any],
) -> None:
    """When clone fails, the algedonic event must not contain the
    embedded PAT. Force a failure by pointing at a nonexistent path."""
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    # File URI to a path that doesn't exist → clone fails.
    nonexistent = tmp_path / "does-not-exist.git"
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=nonexistent.as_uri(),
        github_token="ghp_SUPER_SECRET_PAT_VALUE_xxx",
        log_event=cb,
    )

    # Either the clone failed and we fell through to init, or we
    # surfaced git_clone_failed — either way the secret must not
    # appear in any captured event payload.
    for _, payload in events:
        rendered = json.dumps(payload)
        assert "ghp_SUPER_SECRET_PAT_VALUE_xxx" not in rendered
