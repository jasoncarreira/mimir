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
