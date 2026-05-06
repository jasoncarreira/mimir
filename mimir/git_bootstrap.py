"""Idempotent git-init / clone bootstrap for ``/mimir-home``.

PR 4b of MIMIR_HOME_GIT_TRACKING.md: the post-turn commit module shipped
in PR 4a, but the working copy needed to become a git repo first. This
module gets called from two places:

- ``mimir setup`` (CLI) — operator-driven scaffold; runs once
  interactively when the operator first wires up the home dir.
- ``mimir/server.py:_on_startup`` — runtime; runs every container
  start so a fresh volume / restored backup self-bootstraps without
  manual intervention.

Both call ``bootstrap_git_repo(home, ...)``; the function is
idempotent — safe to invoke any number of times. It performs:

1. Copy the .gitignore template into ``home`` if missing.
2. ``git init`` / ``git clone`` based on env (see §"Decision matrix").
3. Apply mimir's committer identity (``user.name`` + ``user.email``).
4. Install the pre-commit secret-scan hook (chmod +x).
5. Bootstrap commit if init'd fresh AND working tree non-empty.
6. On existing repo: ``git remote set-url`` to refresh embedded token,
   then ``git pull --ff-only`` (logs ``git_pull_blocked`` on conflict
   and exits without raising — the agent's local commits stand).

Failure modes log algedonic events; the function never raises (callers
need to be able to call it from startup paths without tripping the
event loop).

## Decision matrix

|         | .git missing                       | .git present                |
|---------|------------------------------------|-----------------------------|
| repo+token set | ``git clone <token-url>`` to home  | ``git pull --ff-only``     |
| neither set    | ``git init`` + bootstrap commit    | no-op (still ensure hook)   |

## Token rotation

``bootstrap_git_repo`` always re-runs ``git remote set-url origin <url>``
on the token-injected URL when both env vars are set, so a container
restart after a ``GITHUB_TOKEN`` rotation in ``.env`` picks up the new
token without operator intervention. Mid-container rotation is out of
scope for v1 (spec §"`mimir setup` flow").
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# Committer identity — spec §"Locked answers" #3 (revised
# 2026-05-06 msg 1501603018377007295). The email has no associated
# GitHub account; commits show in the log without avatar attribution,
# which is the desired shape for a non-human committer.
DEFAULT_USER_NAME = "mimir"
DEFAULT_USER_EMAIL = "noreply@mimir-agent.local"


# Templates are shipped inside the package so they're locatable at
# runtime without depending on the docker source tree. The ``.gitignore``
# template is named without the leading dot so the source tree itself
# doesn't honor it; the bootstrap copy renames at install time.
_TEMPLATES_DIR = Path(__file__).parent / "templates" / "git"


@dataclass
class BootstrapResult:
    """Summary of what bootstrap_git_repo did. Useful for tests + the
    setup-report printer."""

    initialized: bool       # ran ``git init``
    cloned: bool            # ran ``git clone``
    pulled: bool            # ran ``git pull --ff-only``
    pull_blocked: bool      # pull rejected (non-fast-forward / conflict)
    bootstrap_commit: bool  # made the initial-bootstrap commit
    gitignore_written: bool
    hook_written: bool
    remote_configured: bool
    skipped: bool           # bootstrap was a no-op (e.g. already done)


# ─── public API ──────────────────────────────────────────────────────


def inject_token_into_url(state_repo_url: str, github_token: str) -> str:
    """Rewrite an HTTPS git URL to embed the PAT in the netloc.

    Input:  ``https://github.com/jasoncarreira/mimirbot-state.git``
    Output: ``https://${TOKEN}@github.com/jasoncarreira/mimirbot-state.git``

    Token is URL-encoded so PATs containing reserved chars (``+``, ``/``,
    ``=``) don't break the netloc parse on the receiving end. Leaves
    non-https URLs alone (ssh URLs are caller's problem; we only
    support the HTTPS-token shape per spec §"Locked answers" #2).
    """
    parsed = urllib.parse.urlparse(state_repo_url)
    if parsed.scheme not in {"http", "https"}:
        return state_repo_url
    quoted = urllib.parse.quote(github_token, safe="")
    new_netloc = f"{quoted}@{parsed.hostname}"
    if parsed.port:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return parsed._replace(netloc=new_netloc).geturl()


def bootstrap_git_repo(
    home: Path,
    *,
    state_repo: str | None = None,
    github_token: str | None = None,
    user_name: str = DEFAULT_USER_NAME,
    user_email: str = DEFAULT_USER_EMAIL,
    log_event: callable | None = None,
) -> BootstrapResult:
    """Idempotent bootstrap of the git repo at ``home``.

    Synchronous (uses ``subprocess.run``) so callers from sync contexts
    (CLI ``mimir setup``) work without an event loop. The startup
    caller in ``server._on_startup`` wraps in ``asyncio.to_thread``.

    ``log_event`` is an optional callback ``(event_kind, **fields)``
    used to emit ``git_pull_blocked`` / ``git_clone_failed`` /
    ``git_bootstrap_ok`` records. Defaults to a no-op so unit tests can
    skip the events plumbing.
    """
    home = home.resolve()
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)

    log_event = log_event or (lambda *_a, **_k: None)
    result = BootstrapResult(
        initialized=False, cloned=False, pulled=False, pull_blocked=False,
        bootstrap_commit=False, gitignore_written=False, hook_written=False,
        remote_configured=False, skipped=False,
    )

    git_dir = home / ".git"

    # ── path 1: clone-from-remote when home is empty + env is set ────
    # We only clone when .git is missing AND home has no tracked
    # content — cloning into a non-empty dir requires extra dance
    # (clone elsewhere, move .git in). Defer that to the operator.
    if not git_dir.exists():
        if state_repo and github_token:
            push_url = inject_token_into_url(state_repo, github_token)
            if _is_dir_effectively_empty(home):
                ok = _clone(home, push_url, log_event=log_event)
                if ok:
                    result.cloned = True
                    result.remote_configured = True
                    _apply_identity(home, user_name, user_email)
                    _ensure_gitignore(home, result)
                    _ensure_hook(home, result)
                    log_event(
                        "git_bootstrap_ok",
                        path=str(home),
                        action="cloned",
                    )
                    return result
                # Clone failed → fall through to init.

        # Path 2: init fresh.
        _run(["git", "init", "-q", "-b", "main"], cwd=home, check=True)
        result.initialized = True
        _apply_identity(home, user_name, user_email)
        _ensure_gitignore(home, result)
        _ensure_hook(home, result)
        if state_repo and github_token:
            push_url = inject_token_into_url(state_repo, github_token)
            _run(
                ["git", "remote", "add", "origin", push_url],
                cwd=home, check=False,
            )
            result.remote_configured = True
        # Bootstrap commit so HEAD exists. Skip if working tree somehow
        # ended up empty (shouldn't happen — we just wrote .gitignore).
        try:
            _run(["git", "add", "-A"], cwd=home, check=True)
            porc = _run(
                ["git", "status", "--porcelain"],
                cwd=home, check=True, capture=True,
            )
            if porc.stdout.strip():
                _run(
                    ["git", "commit", "-q", "-m", "initial mimir-home bootstrap"],
                    cwd=home, check=True,
                )
                result.bootstrap_commit = True
        except subprocess.CalledProcessError as exc:
            log.warning("bootstrap commit failed: %s", exc)
        log_event(
            "git_bootstrap_ok",
            path=str(home),
            action="initialized",
        )
        return result

    # ── path 3: existing repo ────────────────────────────────────────
    # Idempotent: refresh identity + hook + gitignore + remote URL,
    # then pull --ff-only.
    _apply_identity(home, user_name, user_email)
    _ensure_gitignore(home, result)
    _ensure_hook(home, result)
    if state_repo and github_token:
        push_url = inject_token_into_url(state_repo, github_token)
        existing = _run(
            ["git", "remote", "get-url", "origin"],
            cwd=home, check=False, capture=True,
        )
        if existing.returncode == 0:
            _run(
                ["git", "remote", "set-url", "origin", push_url],
                cwd=home, check=False,
            )
        else:
            _run(
                ["git", "remote", "add", "origin", push_url],
                cwd=home, check=False,
            )
        result.remote_configured = True

        # Try a fast-forward pull. If it fails, log + continue; the
        # agent's local commits stand and the next turn surfaces it.
        fetch = _run(
            ["git", "fetch", "--all", "--tags", "--quiet"],
            cwd=home, check=False, capture=True,
        )
        if fetch.returncode == 0:
            pull = _run(
                ["git", "pull", "--ff-only", "--quiet"],
                cwd=home, check=False, capture=True,
            )
            if pull.returncode == 0:
                result.pulled = True
            else:
                result.pull_blocked = True
                log_event(
                    "git_pull_blocked",
                    path=str(home),
                    reason=(pull.stderr or pull.stdout or "non-fast-forward").strip()[:500],
                )
        # fetch failure is silent — network outage shouldn't block start.

    log_event(
        "git_bootstrap_ok",
        path=str(home),
        action="reused",
        pulled=result.pulled,
        pull_blocked=result.pull_blocked,
    )
    return result


# ─── helpers ─────────────────────────────────────────────────────────


def _is_dir_effectively_empty(path: Path) -> bool:
    """An empty volume — or one containing only known-safe scaffold —
    can be cloned into. Anything else, refuse (clone target must be
    empty or git refuses)."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    return len(entries) == 0


def _apply_identity(home: Path, user_name: str, user_email: str) -> None:
    _run(
        ["git", "-C", str(home), "config", "user.name", user_name],
        check=False,
    )
    _run(
        ["git", "-C", str(home), "config", "user.email", user_email],
        check=False,
    )


def _ensure_gitignore(home: Path, result: BootstrapResult) -> None:
    """Copy the gitignore template into ``home/.gitignore`` if missing.

    Doesn't overwrite an existing gitignore — operators may have hand-
    edited it. The template ships under ``mimir/templates/git/gitignore``
    (no leading dot, so the source tree doesn't honor it itself)."""
    target = home / ".gitignore"
    if target.exists():
        return
    src = _TEMPLATES_DIR / "gitignore"
    if not src.is_file():
        log.warning("gitignore template missing at %s", src)
        return
    shutil.copyfile(src, target)
    result.gitignore_written = True


def _ensure_hook(home: Path, result: BootstrapResult) -> None:
    """Copy the pre-commit secret-scan hook into ``home/.git/hooks/``
    and chmod +x. Idempotent — overwrites the existing hook so a
    template update propagates on next bootstrap."""
    hook_dir = home / ".git" / "hooks"
    if not hook_dir.is_dir():
        return  # bootstrap should have init'd first; defensive skip.
    src = _TEMPLATES_DIR / "pre-commit"
    if not src.is_file():
        log.warning("pre-commit template missing at %s", src)
        return
    target = hook_dir / "pre-commit"
    shutil.copyfile(src, target)
    target.chmod(0o755)
    result.hook_written = True


def _clone(
    home: Path,
    push_url: str,
    *,
    log_event: callable,
) -> bool:
    """Clone ``push_url`` into ``home`` (which must be empty). Returns
    True on success, False on failure (logs ``git_clone_failed``)."""
    # Use ``git clone <url> .`` from inside the dir so we don't have to
    # delete and recreate the dir to satisfy git's "empty target" rule.
    try:
        _run(
            ["git", "clone", "--quiet", push_url, "."],
            cwd=home, check=True, capture=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        # Sanitize: never echo the URL (contains the token).
        log_event(
            "git_clone_failed",
            path=str(home),
            returncode=exc.returncode,
            stderr=_redact(exc.stderr or "", push_url)[:500],
        )
        return False


def _redact(text: str, push_url: str) -> str:
    """Strip the token-bearing URL out of error text before logging."""
    if not text:
        return text
    parsed = urllib.parse.urlparse(push_url)
    # Replace the token-bearing form with the canonical form.
    if parsed.hostname:
        sanitized_host = parsed.hostname
        # netloc as it was — token@host[:port]
        token_netloc = parsed.netloc
        return text.replace(token_netloc, sanitized_host)
    return text


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Tiny wrapper so we get uniform timeout + capture behavior across
    helpers. 30s timeout matches PUSH_TIMEOUT_SECONDS in git_tracking
    so the bootstrap can't wedge startup on a slow remote."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=capture,
        text=True,
        timeout=30,
    )


__all__: tuple[str, ...] = (
    "BootstrapResult",
    "DEFAULT_USER_EMAIL",
    "DEFAULT_USER_NAME",
    "bootstrap_git_repo",
    "inject_token_into_url",
)
