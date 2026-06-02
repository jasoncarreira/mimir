"""Tests for ``mimir.git_bootstrap`` (PR 4b/4d of MIMIR_HOME_GIT_TRACKING).

Covers the idempotent bootstrap flow:

- Bootstrap on an empty home with no env → ``git init`` + bootstrap
  commit + identity + .gitignore + pre-commit hook.
- Bootstrap on an existing repo → no re-init, identity + hook + gitignore
  refreshed if missing, no-op otherwise.
- Bootstrap on existing repo with operator's local commits and a
  divergent (non-fast-forward) remote → ``git_pull_blocked`` event.
- Pre-commit hook is executable and refuses secret-shaped content.
- Allowlist .gitignore: a binary file under ``memory/`` (e.g.
  ``memory/atoms.db``) is NOT staged by ``git add -A``.
- Token never leaks into events.jsonl (PAT-regex redaction in
  ``_redact``).
- **PR 4d** — credential-helper plumbing: ``<home>/.git/credentials``
  is written 0600 with the canonical
  ``https://x-access-token:<PAT>@<host>`` line, ``credential.helper``
  is set to ``store --file=<abs-path>``, the remote URL stays clean
  (no embedded token), and a legacy in-URL token from a PR4b-era
  config gets migrated to the clean form on next bootstrap.

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


# ─── credential helper installation (PR 4d) ──────────────────────────


def _read_creds(home: Path) -> str:
    """Helper for the credential-file assertions."""
    return (home / ".git" / "credentials").read_text()


def test_credential_helper_writes_canonical_line(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/jasoncarreira/mimirbot-state.git",
        github_token="ghp_AbCdEf123",
    )
    assert res.credentials_written is True
    line = _read_creds(home).rstrip("\n")
    assert line == "https://x-access-token:ghp_AbCdEf123@github.com"


def test_credential_helper_url_encodes_token_with_specials(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="tok+en/with=specials",
    )
    creds = _read_creds(home)
    assert "tok%2Ben%2Fwith%3Dspecials" in creds


def test_credential_helper_file_is_mode_600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="abc",
    )
    creds_path = home / ".git" / "credentials"
    mode = creds_path.stat().st_mode & 0o777
    # Group + other must have no bits; owner read+write only.
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_credential_helper_local_config_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="abc",
    )
    helper = _git("config", "--local", "credential.helper", cwd=home).stdout.strip()
    expected_path = (home / ".git" / "credentials").resolve()
    assert helper == f"store --file={expected_path}"


def test_credential_helper_preserves_port(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://gh.example.com:8443/x/y.git",
        github_token="abc",
    )
    line = _read_creds(home).rstrip("\n")
    assert line == "https://x-access-token:abc@gh.example.com:8443"


def test_credential_helper_idempotent_token_rotation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="old-token",
    )
    assert "old-token" in _read_creds(home)

    # Second bootstrap with rotated token — file rewritten, mode kept.
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="new-token-xyz",
    )
    creds = _read_creds(home)
    assert "new-token-xyz" in creds
    assert "old-token" not in creds
    mode = (home / ".git" / "credentials").stat().st_mode & 0o777
    assert mode == 0o600


def test_credential_helper_skipped_for_non_https(
    tmp_path: Path, captured_events: tuple[list, Any],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="git@github.com:foo/bar.git",
        github_token="abc",
        log_event=cb,
    )
    # ssh URLs aren't supported — credential helper not installed.
    assert res.credentials_written is False
    assert not (home / ".git" / "credentials").exists()


def test_remote_url_has_no_embedded_token_after_init(
    tmp_path: Path,
) -> None:
    """After init+bootstrap, ``git remote get-url origin`` must return
    the clean URL — no token in the netloc."""
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="ghp_secret_token",
    )
    url = _git("remote", "get-url", "origin", cwd=home).stdout.strip()
    assert url == "https://github.com/foo/bar.git"
    assert "ghp_secret_token" not in url
    # And it shouldn't be in the raw .git/config either.
    raw_config = (home / ".git" / "config").read_text()
    assert "ghp_secret_token" not in raw_config


def test_legacy_in_url_token_migrated_on_existing_repo(
    tmp_path: Path,
) -> None:
    """If an existing repo has a PR4b-style token-in-URL remote (user
    upgrading from PR 4b → PR 4d), bootstrap must rewrite the remote
    to the clean URL and flag ``legacy_token_url_migrated``."""
    home = tmp_path / "home"
    home.mkdir()

    # First bootstrap with PR4b-shape: manually plant a token-in-URL.
    git_bootstrap.bootstrap_git_repo(home)  # init only, no remote
    legacy_url = "https://OLD_PAT_VALUE@github.com/foo/bar.git"
    _git("remote", "add", "origin", legacy_url, cwd=home)

    # PR4d-shape bootstrap: should detect + strip.
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo="https://github.com/foo/bar.git",
        github_token="new-token",
    )
    assert res.legacy_token_url_migrated is True
    url_after = _git("remote", "get-url", "origin", cwd=home).stdout.strip()
    assert url_after == "https://github.com/foo/bar.git"
    assert "OLD_PAT_VALUE" not in (home / ".git" / "config").read_text()


def test_url_has_embedded_token_helper() -> None:
    # Direct cover of the migration-detection helper.
    has = git_bootstrap._url_has_embedded_token
    assert has("https://tok@github.com/foo/bar.git") is True
    assert has("https://github.com/foo/bar.git") is False
    assert has("git@github.com:foo/bar.git") is False  # ssh — not our problem
    assert has("") is False


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


def test_pre_commit_hook_allows_token_named_note(tmp_path: Path) -> None:
    """chainlink #352: a note whose name contains "token"/"credential" but has
    no secret content commits cleanly — the filename backstop no longer blocks
    it; only the content scan applies."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_hook(home)

    (home / "memory" / "issues").mkdir(parents=True, exist_ok=True)
    (home / "memory" / "issues" / "gog-token-expiry.md").write_text(
        "gog OAuth tokens expire; re-auth via `gog auth`. No secret here.\n"
    )
    _git("add", "memory/issues/gog-token-expiry.md", cwd=home)

    proc = subprocess.run(
        ["git", "commit", "-m", "token-named note, clean content"],
        cwd=home, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pre_commit_hook_still_refuses_dot_env(tmp_path: Path) -> None:
    """Regression: the high-signal filename patterns #352 keeps still bite — a
    ``.env`` is refused even with benign content (force-added past gitignore)."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_hook(home)

    (home / "memory" / ".env").write_text("BENIGN=1\n")
    _git("add", "-f", "memory/.env", cwd=home)

    proc = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=home, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    msg = (proc.stdout + proc.stderr).lower()
    assert "filename" in msg or "secret" in msg


# ─── allowlist .gitignore behaviour ──────────────────────────────────


def test_gitignore_blocks_atoms_db_under_state(tmp_path: Path) -> None:
    """Belt-and-suspenders: ``state/atoms.db`` (a likely accidental
    drop point) must be excluded by the gitignore even though
    ``state/**`` is allowlisted — the wildcard ``*.db`` re-block wins."""
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


def test_gitignore_admits_arbitrary_state_file(tmp_path: Path) -> None:
    """``state/**`` is allowlisted so the agent's working state persists by
    default. An arbitrary, non-curated state note (e.g. voice-drafts.md) must
    be tracked — not silently dropped the way the old narrow allowlist did."""
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(home)

    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "voice-drafts.md").write_text("draft\n")

    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=home, capture_output=True, text=True, check=True,
    )
    assert "state/voice-drafts.md" in res.stdout


def test_gitignore_admits_token_named_memory_note(tmp_path: Path) -> None:
    """chainlink #352: a legit note whose name merely contains "token" /
    "credential" (no secret content) is no longer blocked by the gitignore —
    the *token*/*credential* filename re-blocks were dropped."""
    home = tmp_path / "home"
    home.mkdir()
    git_bootstrap.bootstrap_git_repo(home)

    (home / "memory" / "issues").mkdir(parents=True, exist_ok=True)
    (home / "memory" / "issues" / "gog-token-expiry.md").write_text("note\n")
    (home / "memory" / "issues" / "social-cli-credentials.md").write_text("note\n")

    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=home, capture_output=True, text=True, check=True,
    )
    assert "memory/issues/gog-token-expiry.md" in res.stdout
    assert "memory/issues/social-cli-credentials.md" in res.stdout


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


# ─── upstream tracking (PR 4e) ───────────────────────────────────────


def test_init_path_pushes_to_empty_remote(
    tmp_path: Path,
    upstream_repo: Path,
    captured_events: tuple[list, Any],
) -> None:
    """Fresh init + a bare empty remote → bootstrap must do ``git push
    -u origin main`` so the remote ``main`` exists and local has
    upstream tracking from the very first start.

    This reproduces the production bug: container starts with a
    non-empty ``/mimir-home`` (memory/, state/ already exist) but no
    ``.git`` and an empty remote repo. Clone is skipped (home not
    empty), init runs, bootstrap commit lands — but without PR 4e the
    remote ``main`` is never created and local has no upstream config.
    """
    home = tmp_path / "home"
    home.mkdir()
    # Put a file in home so the dir isn't empty → clone path skipped
    # → falls through to init path.
    (home / "memory").mkdir()
    (home / "memory" / "seed.md").write_text("seed\n")
    events, cb = captured_events

    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=upstream_repo.as_uri(),
        github_token="abc",  # local file:// URLs don't auth-check
        log_event=cb,
    )

    assert res.initialized is True
    assert res.cloned is False, "non-empty home → clone path must be skipped"
    assert res.bootstrap_commit is True
    assert res.upstream_set is True, "upstream tracking must be set"
    assert res.initial_push is True, "initial push must run on empty remote"

    # branch.main now has upstream config.
    upstream = _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name",
        "main@{upstream}", cwd=home,
    ).stdout.strip()
    assert upstream == "origin/main"

    # The bootstrap commit landed on the remote.
    remote_log = subprocess.run(
        ["git", "--git-dir", str(upstream_repo), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "initial mimir-home bootstrap" in remote_log

    # And we logged a git_upstream_set event with action=initial_push.
    upstream_events = [
        (k, p) for k, p in events
        if k == "git_upstream_set" and p.get("action") == "initial_push"
    ]
    assert len(upstream_events) == 1


def test_existing_repo_no_tracking_remote_has_main(
    tmp_path: Path,
    seeded_remote: Path,
    captured_events: tuple[list, Any],
) -> None:
    """Existing repo on disk (not freshly init'd this run) with a
    remote that already has ``main`` but local lacks tracking → set
    upstream from origin/main, no initial push."""
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    # First bootstrap (no remote yet) — yields the init shape.
    git_bootstrap.bootstrap_git_repo(home)
    # Manually add the remote without setting tracking (simulates
    # the "PR4b-era init happened, now the remote has commits but
    # local main was never pushed -u" state).
    _git("remote", "add", "origin", seeded_remote.as_uri(), cwd=home)
    # Confirm no tracking yet.
    no_upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref",
         "--symbolic-full-name", "main@{upstream}"],
        cwd=home, capture_output=True, text=True,
    )
    assert no_upstream.returncode != 0  # main@{upstream} unresolvable

    # Second bootstrap → existing repo path, should set tracking
    # without needing to push.
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=seeded_remote.as_uri(),
        github_token="abc",
        log_event=cb,
    )

    assert res.upstream_set is True
    assert res.initial_push is False, (
        "remote already has main; should not push -u"
    )

    upstream = _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name",
        "main@{upstream}", cwd=home,
    ).stdout.strip()
    assert upstream == "origin/main"

    upstream_events = [
        (k, p) for k, p in events
        if k == "git_upstream_set"
        and p.get("action") == "tracking_existing_remote"
    ]
    assert len(upstream_events) == 1


def test_bootstrap_emits_paired_positives_on_pull_success(
    tmp_path: Path,
    upstream_repo: Path,
    captured_events: tuple[list, Any],
) -> None:
    """chainlink #65 (sub B): on a clean fast-forward pull, bootstrap
    emits ``git_fetch_ok`` and ``git_pull_ok`` so the algedonic block
    can surface recovery alongside any sticky ``git_pull_blocked``
    failure line."""
    # Seed the remote with a commit so there's something to fetch + pull.
    op = tmp_path / "op-clone"
    op.mkdir()
    _git("init", "-q", "-b", "main", cwd=op)
    _git("config", "user.email", "op@x", cwd=op)
    _git("config", "user.name", "op", cwd=op)
    _git("config", "commit.gpgsign", "false", cwd=op)
    (op / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=op)
    _git("commit", "-q", "-m", "seed", cwd=op)
    _git("remote", "add", "origin", upstream_repo.as_uri(), cwd=op)
    _git("push", "-q", "-u", "origin", "main", cwd=op)

    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events
    git_bootstrap.bootstrap_git_repo(
        home, state_repo=upstream_repo.as_uri(), github_token="tok",
        log_event=cb,
    )

    # Operator lands a new commit upstream so the next bootstrap pulls it.
    (op / "added.md").write_text("added\n")
    _git("add", "added.md", cwd=op)
    _git("commit", "-q", "-m", "added", cwd=op)
    _git("push", "-q", "origin", "main", cwd=op)

    events.clear()
    res = git_bootstrap.bootstrap_git_repo(
        home, state_repo=upstream_repo.as_uri(), github_token="tok",
        log_event=cb,
    )

    assert res.pulled is True
    assert res.pull_blocked is False
    kinds = [k for k, _ in events]
    # Both paired positives fire on the happy path.
    assert "git_fetch_ok" in kinds
    assert "git_pull_ok" in kinds
    # And the sticky failure event does NOT fire on the happy path.
    assert "git_pull_blocked" not in kinds


def test_existing_repo_already_tracking_is_noop(
    tmp_path: Path,
    upstream_repo: Path,
    captured_events: tuple[list, Any],
) -> None:
    """Second bootstrap on the same home — tracking already configured
    by the first run, helper short-circuits."""
    home = tmp_path / "home"
    home.mkdir()
    # Non-empty home → init path (matches the production shape).
    (home / "memory").mkdir()
    (home / "memory" / "seed.md").write_text("seed\n")

    # First run: init + initial push, sets tracking.
    first = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=upstream_repo.as_uri(),
        github_token="abc",
    )
    assert first.upstream_set is True
    assert first.initial_push is True

    # Second run: existing repo path. Tracking already set, helper
    # returns early; upstream_set stays False on this BootstrapResult
    # (it's a fresh result for this invocation, not cumulative).
    events, cb = captured_events
    second = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=upstream_repo.as_uri(),
        github_token="abc",
        log_event=cb,
    )

    assert second.upstream_set is False
    assert second.initial_push is False
    # No git_upstream_set event this time — it short-circuited.
    assert not any(k == "git_upstream_set" for k, _ in events)


def test_no_state_repo_skips_upstream_setup(
    tmp_path: Path,
    captured_events: tuple[list, Any],
) -> None:
    """Bootstrap without a state_repo / token → helper not invoked,
    no upstream/initial_push attempts (no remote to push to)."""
    home = tmp_path / "home"
    home.mkdir()
    events, cb = captured_events

    res = git_bootstrap.bootstrap_git_repo(home, log_event=cb)

    assert res.initialized is True
    assert res.upstream_set is False
    assert res.initial_push is False
    # No upstream-related events.
    assert not any(k == "git_upstream_set" for k, _ in events)
    assert not any(k == "git_initial_push_failed" for k, _ in events)


def test_unreachable_remote_does_not_raise(
    tmp_path: Path,
    captured_events: tuple[list, Any],
) -> None:
    """If the remote is unreachable (network outage, bad path),
    bootstrap returns normally — never raises. Local commits stand;
    the next bootstrap retries the upstream-tracking probe.

    Specifically: ``ls-remote`` fails before we even try to push, so
    the helper short-circuits. ``upstream_set`` stays False.
    """
    home = tmp_path / "home"
    home.mkdir()
    # Non-empty home so we go through init path, not clone.
    (home / "memory").mkdir()
    (home / "memory" / "seed.md").write_text("seed\n")
    events, cb = captured_events

    # Point at a nonexistent local path → ls-remote fails.
    nonexistent = tmp_path / "does-not-exist.git"
    res = git_bootstrap.bootstrap_git_repo(
        home,
        state_repo=nonexistent.as_uri(),
        github_token="abc",
        log_event=cb,
    )

    # Init succeeded; remote configured; just no upstream wired.
    assert res.initialized is True
    assert res.bootstrap_commit is True
    assert res.remote_configured is True
    assert res.upstream_set is False
    assert res.initial_push is False
    # No upstream events at all — helper bailed before trying to push.
    assert not any(k == "git_upstream_set" for k, _ in events)


# ─── PR #111 review-fix-2: TimeoutExpired contract ────────────────────


def test_run_translates_timeout_to_called_process_error_when_check_true(
    monkeypatch,
):
    """PR #111 re-review pin: when ``_run`` hits the 30s timeout AND
    the caller used ``check=True``, the wrapper raises
    ``CalledProcessError(returncode=124)`` so existing
    ``except CalledProcessError`` handlers cover the timeout path."""
    import subprocess
    from mimir.git_bootstrap import _run

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args") or args[0], timeout=30,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _run(["git", "fetch"], cwd=Path("/tmp"), check=True)
    assert exc_info.value.returncode == 124
    assert "timed out after 30s" in (exc_info.value.stderr or "")


def test_run_returns_completed_process_on_timeout_when_check_false(
    monkeypatch,
):
    """PR #111 re-review pin: ``check=False`` callers (e.g.
    ``_existing_remote_url``) inspect ``returncode`` directly. The
    wrapper must NOT raise on timeout for them — instead return a
    ``CompletedProcess(returncode=124)`` so the existing returncode
    branch keeps working."""
    import subprocess
    from mimir.git_bootstrap import _run

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args") or args[0], timeout=30,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run(["git", "remote", "-v"], cwd=Path("/tmp"), check=False)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 124
    assert "timed out after 30s" in (result.stderr or "")


# ─── ensure_workspace_hooks (chainlink #249) ─────────────────────────


def _init_bare_repo(path: Path) -> None:
    """Init a minimal git repo with .git/hooks/ dir for hook install tests."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)


def test_ensure_workspace_hooks_installs_pre_push(tmp_path: Path) -> None:
    """ensure_workspace_hooks() copies the pre-push template into the
    workspace repo's .git/hooks/ directory, marks it executable,
    and returns True."""
    workspace = tmp_path / "workspace"
    _init_bare_repo(workspace)

    result = git_bootstrap.ensure_workspace_hooks(workspace)

    assert result is True
    hook = workspace / ".git" / "hooks" / "pre-push"
    assert hook.is_file()
    assert hook.stat().st_mode & stat.S_IXUSR
    # Sanity-check contents — the hook must exit 0 for non-origin remotes.
    content = hook.read_text()
    assert "origin" in content
    assert "stale" in content.lower()


def test_ensure_workspace_hooks_idempotent(tmp_path: Path) -> None:
    """Calling ensure_workspace_hooks() twice must not error and must
    leave a valid hook (template update propagates on re-install)."""
    workspace = tmp_path / "workspace"
    _init_bare_repo(workspace)

    r1 = git_bootstrap.ensure_workspace_hooks(workspace)
    r2 = git_bootstrap.ensure_workspace_hooks(workspace)

    assert r1 is True
    assert r2 is True
    assert (workspace / ".git" / "hooks" / "pre-push").is_file()


def test_ensure_workspace_hooks_no_git_dir_returns_false(tmp_path: Path) -> None:
    """If the path has no .git/hooks dir (not a git repo), the function
    returns False without raising."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = git_bootstrap.ensure_workspace_hooks(not_a_repo)

    assert result is False
    assert not (not_a_repo / ".git").exists()


def test_ensure_workspace_hooks_missing_template_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the pre-push template is missing, ensure_workspace_hooks returns
    False and does not raise."""
    workspace = tmp_path / "workspace"
    _init_bare_repo(workspace)

    # Point the bootstrap templates dir at an empty tmp dir.
    empty_templates = tmp_path / "empty-templates"
    empty_templates.mkdir()
    monkeypatch.setattr(git_bootstrap, "_TEMPLATES_DIR", empty_templates)

    result = git_bootstrap.ensure_workspace_hooks(workspace)

    assert result is False
    assert not (workspace / ".git" / "hooks" / "pre-push").is_file()


def test_pre_push_hook_refuses_stale_branch(tmp_path: Path) -> None:
    """The installed pre-push hook must exit 1 and print a staleness
    message when the branch's merge-base is not origin/main's current tip.

    Setup:
      - Create a bare 'origin' repo with two commits on main.
      - Clone into a workspace.
      - Push one commit to advance origin/main WITHOUT updating local.
      - Install the pre-push hook.
      - Try to push; the hook should refuse because the branch is stale.
    """
    # Bare origin repo.
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", cwd=tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )

    # First clone (workspace A) — makes the initial commit.
    ws_a = tmp_path / "ws_a"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(ws_a)],
        check=True, capture_output=True,
    )
    _git("config", "user.name", "Test", cwd=ws_a)
    _git("config", "user.email", "test@example.com", cwd=ws_a)
    _git("config", "commit.gpgsign", "false", cwd=ws_a)
    (ws_a / "README.md").write_text("initial\n")
    _git("add", "README.md", cwd=ws_a)
    _git("commit", "-q", "-m", "initial", cwd=ws_a)
    _git("push", "-q", "origin", "main", cwd=ws_a)

    # Second clone (workspace B) — our test workspace.
    ws_b = tmp_path / "ws_b"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(ws_b)],
        check=True, capture_output=True,
    )
    _git("config", "user.name", "Test", cwd=ws_b)
    _git("config", "user.email", "test@example.com", cwd=ws_b)
    _git("config", "commit.gpgsign", "false", cwd=ws_b)

    # Advance origin/main via ws_a (simulates another PR merging).
    (ws_a / "other.md").write_text("other PR\n")
    _git("add", "other.md", cwd=ws_a)
    _git("commit", "-q", "-m", "other PR merged to main", cwd=ws_a)
    _git("push", "-q", "origin", "main", cwd=ws_a)

    # Now ws_b creates a branch and commits — its base is stale.
    _git("checkout", "-b", "my-feature", cwd=ws_b)
    (ws_b / "feature.md").write_text("feature\n")
    _git("add", "feature.md", cwd=ws_b)
    _git("commit", "-q", "-m", "add feature", cwd=ws_b)

    # Install the hook.
    git_bootstrap.ensure_workspace_hooks(ws_b)

    # Push should be refused by the hook.
    proc = subprocess.run(
        ["git", "push", "origin", "my-feature"],
        cwd=ws_b, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "stale" in combined.lower() or "merge-base" in combined.lower()


def test_pre_push_hook_passes_current_branch(tmp_path: Path) -> None:
    """The hook must exit 0 when the branch's merge-base IS origin/main tip
    (i.e., the branch was branched off current main)."""
    # Bare origin.
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )

    # Initial commit on main.
    ws_a = tmp_path / "ws_a"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(ws_a)],
        check=True, capture_output=True,
    )
    _git("config", "user.name", "Test", cwd=ws_a)
    _git("config", "user.email", "test@example.com", cwd=ws_a)
    _git("config", "commit.gpgsign", "false", cwd=ws_a)
    (ws_a / "README.md").write_text("initial\n")
    _git("add", "README.md", cwd=ws_a)
    _git("commit", "-q", "-m", "initial", cwd=ws_a)
    _git("push", "-q", "origin", "main", cwd=ws_a)

    # Clone fresh (already on current main).
    ws_b = tmp_path / "ws_b"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(ws_b)],
        check=True, capture_output=True,
    )
    _git("config", "user.name", "Test", cwd=ws_b)
    _git("config", "user.email", "test@example.com", cwd=ws_b)
    _git("config", "commit.gpgsign", "false", cwd=ws_b)

    # Branch off fresh main → NOT stale.
    _git("checkout", "-b", "fresh-feature", cwd=ws_b)
    (ws_b / "feat.md").write_text("new feature\n")
    _git("add", "feat.md", cwd=ws_b)
    _git("commit", "-q", "-m", "fresh feature", cwd=ws_b)

    git_bootstrap.ensure_workspace_hooks(ws_b)

    # Push should succeed (hook exits 0).
    proc = subprocess.run(
        ["git", "push", "origin", "fresh-feature"],
        cwd=ws_b, capture_output=True, text=True, check=False,
    )
    # returncode 0 = hook passed; git itself may not error even if remote
    # receives the branch fine.
    assert proc.returncode == 0


def test_credential_helper_idempotent_across_bootstraps(tmp_path: Path) -> None:
    """Re-running bootstrap (happens on every container start) must NOT
    accumulate duplicate ``credential.helper`` entries. Pre-fix the reset
    used a bare ``git config credential.helper ""`` which errors on the
    now multi-valued key AND fails to clear, so each boot appended another
    ``store`` helper (observed: 273 on a long-lived dev container).
    ``--replace-all`` keeps it at exactly ``[empty, store]``."""
    home = tmp_path / "home"
    home.mkdir()
    for _ in range(3):
        git_bootstrap.bootstrap_git_repo(
            home,
            state_repo="https://github.com/foo/bar.git",
            github_token="abc",
        )
    values = _git(
        "config", "--local", "--get-all", "credential.helper", cwd=home,
    ).stdout.splitlines()
    expected = (home / ".git" / "credentials").resolve()
    assert values == ["", f"store --file={expected}"], (
        f"credential.helper accumulated across bootstraps: {values!r}"
    )
