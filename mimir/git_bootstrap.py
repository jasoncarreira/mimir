"""Idempotent git-init / clone bootstrap for ``/mimir-home``.

PR 4b of docs/internal/MIMIR_HOME_GIT_TRACKING.md established the working copy as a
git repo; PR 4d (this revision) replaces the in-URL token authentication
with a git credential helper so the PAT is no longer embedded in
``.git/config`` or visible to ``git remote -v``. The module gets called
from two places:

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
5. **Install the credential helper** (PR 4d): writes
   ``<home>/.git/credentials`` (chmod 600) with the
   ``https://x-access-token:<PAT>@<host>`` line, sets
   ``credential.helper "store --file=<path>"`` in local git config.
   The remote URL itself is the clean, token-free form.
6. Bootstrap commit if init'd fresh AND working tree non-empty.
7. **Ensure upstream tracking** (PR 4e): if ``branch.main`` lacks
   upstream config, either set it from an existing ``origin/main``
   or do an initial ``git push -u origin main`` to bootstrap the
   remote. Without this, a fresh init + empty remote leaves
   ``git pull`` and ``git push`` both broken until manually fixed.
8. On existing repo: ``git remote set-url`` to the clean URL
   (migrates legacy in-URL-token configs from PR 4b), refresh
   credentials, ensure upstream tracking, then ``git pull --ff-only``
   (logs ``git_pull_blocked`` on conflict and exits without raising —
   the agent's local commits stand).

Failure modes log algedonic events; the function never raises (callers
need to be able to call it from startup paths without tripping the
event loop).

## Decision matrix

|         | .git missing                       | .git present                |
|---------|------------------------------------|-----------------------------|
| repo+token set | ``git clone <token-url>`` to home  | refresh creds + ``git pull --ff-only`` |
| neither set    | ``git init`` + bootstrap commit    | no-op (still ensure hook)   |

## Token rotation

``bootstrap_git_repo`` rewrites ``<home>/.git/credentials`` from the
current ``GITHUB_TOKEN`` env var on every invocation. A container
restart after a ``GITHUB_TOKEN`` rotation in ``.env`` picks up the new
token without operator intervention. Mid-container rotation is out of
scope for v1 (spec §"`mimir setup` flow").

## Why a credential helper rather than in-URL token

Embedding the PAT in the remote URL (the original PR 4b shape) leaks
it to anyone running ``git remote -v``, anyone reading ``.git/config``,
and any bash output that captures git invocations. The credential
helper keeps the token in a single 0600 file at
``<home>/.git/credentials`` (under ``.git/``, so it's ignored by git
itself) and the remote URL stays the clean canonical form.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .redaction import redact_text as _redact

log = logging.getLogger(__name__)


# Committer identity for autonomous commits. The default email
# is a non-routable ``.local`` domain so:
#  - it has no associated GitHub account (commits don't render with
#    an avatar — the desired shape for a non-human committer)
#  - it doesn't leak operator infrastructure (an earlier default
#    embedded a real deployment domain)
# Operators override via ``MIMIR_GIT_USER_NAME`` /
# ``MIMIR_GIT_USER_EMAIL`` in their compose.env / `.env`.
DEFAULT_USER_NAME = "mimir"
DEFAULT_USER_EMAIL = "noreply@mimir-agent.local"


# Templates are shipped inside the package so they're locatable at
# runtime without depending on the docker source tree. The ``.gitignore``
# template is named without the leading dot so the source tree itself
# doesn't honor it; the bootstrap copy renames at install time.
_TEMPLATES_DIR = Path(__file__).parent / "templates" / "git"


# Username sent to GitHub alongside the PAT. GitHub accepts any
# non-empty value for PAT auth; ``x-access-token`` is the canonical
# form GitHub Apps use and is unambiguous in logs / debugging.
_PAT_USERNAME = "x-access-token"


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
    credentials_written: bool   # PR 4d: credential helper file written / refreshed
    legacy_token_url_migrated: bool  # PR 4d: stripped a PR4b-style in-URL token
    upstream_set: bool      # PR 4e: branch.main upstream tracking configured
    initial_push: bool      # PR 4e: ran ``git push -u`` to create remote main
    skipped: bool           # bootstrap was a no-op (e.g. already done)


# ─── public API ──────────────────────────────────────────────────────


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
        remote_configured=False, credentials_written=False,
        legacy_token_url_migrated=False, upstream_set=False,
        initial_push=False, skipped=False,
    )

    git_dir = home / ".git"

    # ── path 1: clone-from-remote when home is empty + env is set ────
    # We only clone when .git is missing AND home has no tracked
    # content — cloning into a non-empty dir requires extra dance
    # (clone elsewhere, move .git in). Defer that to the operator.
    if not git_dir.exists():
        if state_repo and github_token:
            # Clone is the one place we still inject the token into the
            # URL: ``.git/`` doesn't exist yet, so ``git config --local``
            # has nowhere to live, and we can't pre-stage the credential
            # helper before clone. The injection is transient — we
            # rewrite the remote to the clean URL immediately on
            # success. Argv-level exposure is bounded to one subprocess.
            transient_url = _inject_token_into_url(state_repo, github_token)
            if _is_dir_effectively_empty(home):
                ok = _clone(home, transient_url, log_event=log_event)
                if ok:
                    result.cloned = True
                    # Rewrite remote to the clean URL right away.
                    _run(
                        ["git", "remote", "set-url", "origin", state_repo],
                        cwd=home, check=False,
                    )
                    result.remote_configured = True
                    _apply_identity(home, user_name, user_email)
                    _ensure_gitignore(home, result)
                    _ensure_hook(home, result)
                    _install_credential_helper(
                        home, state_repo, github_token, result,
                    )
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
            # Install credential helper *before* adding the remote so
            # subsequent network operations can authenticate.
            _install_credential_helper(
                home, state_repo, github_token, result,
            )
            _run(
                ["git", "remote", "add", "origin", state_repo],
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
        # If we have a bootstrap commit and a remote, push -u to create
        # the remote ``main`` and set local tracking in one shot. This
        # closes the "init+empty-remote → no-upstream pull/push errors"
        # loop that bites the very first container start.
        if (state_repo and github_token and result.bootstrap_commit):
            _ensure_upstream_tracking(home, log_event, result)
            # Re-install credentials AFTER network ops. git's
            # ``credential-store`` helper invokes ``erase`` when the
            # remote returns 401/403/404 (the test path's fake URLs hit
            # this; production hits it too on a stale PAT or transient
            # auth failure), which silently truncates ``.git/credentials``.
            # The bootstrap function's contract is "after I return, the
            # credentials file contains the PAT" — re-install at the
            # end of the path seals that contract regardless of what
            # the upstream-tracking probe did. Idempotent + cheap (one
            # atomic write + two ``git config`` calls).
            _install_credential_helper(
                home, state_repo, github_token, result,
            )
        log_event(
            "git_bootstrap_ok",
            path=str(home),
            action="initialized",
        )
        return result

    # ── path 3: existing repo ────────────────────────────────────────
    # Idempotent: refresh identity + hook + gitignore + credential
    # helper + remote URL, then pull --ff-only. Migrates any PR4b-era
    # in-URL token by rewriting the remote to the clean form.
    _apply_identity(home, user_name, user_email)
    _ensure_gitignore(home, result)
    _ensure_hook(home, result)
    live_home_ok = _ensure_live_home_on_main(home, log_event)

    if state_repo and github_token:
        _install_credential_helper(
            home, state_repo, github_token, result,
        )

        existing = _run(
            ["git", "remote", "get-url", "origin"],
            cwd=home, check=False, capture=True,
        )
        if existing.returncode == 0:
            current_url = (existing.stdout or "").strip()
            if _url_has_embedded_token(current_url):
                result.legacy_token_url_migrated = True
            _run(
                ["git", "remote", "set-url", "origin", state_repo],
                cwd=home, check=False,
            )
        else:
            _run(
                ["git", "remote", "add", "origin", state_repo],
                cwd=home, check=False,
            )
        result.remote_configured = True

        # Ensure upstream tracking is wired up before pull. Handles the
        # "local main has no upstream" case (existing repo init'd before
        # remote ``main`` existed) by either setting tracking from an
        # existing remote ``main`` or by doing the initial ``push -u``.
        _ensure_upstream_tracking(home, log_event, result)

        # Try a fast-forward pull. If it fails, log + continue; the
        # agent's local commits stand and the next turn surfaces it.
        # Skip pull if we just did an initial push (nothing to pull —
        # remote is exactly what we just sent).
        if not result.initial_push and live_home_ok:
            fetch = _run(
                ["git", "fetch", "--all", "--tags", "--quiet"],
                cwd=home, check=False, capture=True,
            )
            if fetch.returncode == 0:
                # chainlink #65 (sub B): paired-positive emit. Surfaces
                # alongside any sticky ``git_pull_blocked`` line so the
                # operator can read recovery against the 24h failure
                # line. First-occurrence-only at the feedback layer.
                log_event("git_fetch_ok", path=str(home))
                pull = _run(
                    ["git", "pull", "--ff-only", "--quiet"],
                    cwd=home, check=False, capture=True,
                )
                if pull.returncode == 0:
                    result.pulled = True
                    log_event("git_pull_ok", path=str(home))
                else:
                    result.pull_blocked = True
                    log_event(
                        "git_pull_blocked",
                        path=str(home),
                        reason=_redact(
                            (pull.stderr or pull.stdout or "non-fast-forward").strip()[:500]
                        ),
                    )
            # fetch failure is silent — network outage shouldn't block start.

        # Re-install credentials AFTER all network ops. Same reason as
        # path 2's post-tracking re-install: ``credential-store`` erases
        # entries on 401/403/404, which silently truncates the file.
        # The bootstrap function's contract is "after I return, the
        # credentials file contains the PAT" — re-install seals that
        # contract regardless of what fetch/pull/ls-remote did. Idempotent.
        _install_credential_helper(
            home, state_repo, github_token, result,
        )

    log_event(
        "git_bootstrap_ok",
        path=str(home),
        action="reused",
        pulled=result.pulled,
        pull_blocked=result.pull_blocked,
        legacy_token_url_migrated=result.legacy_token_url_migrated,
    )
    return result



def _ensure_live_home_on_main(home: Path, log_event: callable) -> bool:
    """Startup guard: the live home working tree must stay on ``main``.

    Proposal edits are made in scratch worktrees, not by checking the live
    ``/mimir-home`` repo onto feature branches. If a previous turn left the
    live repo on some other branch, per-turn git_tracking would commit fresh
    sessions / memory writes to that feature branch and the prompt could load
    stale core content. Detect that shape at startup and switch back to main
    when the worktree is clean; otherwise emit a loud algedonic signal and
    leave the repo untouched for manual reconciliation.
    """
    branch = _run(
        ["git", "-C", str(home), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False, capture=True,
    )
    current = (branch.stdout or "").strip()
    if branch.returncode != 0 or current in ("", "main"):
        return True
    observed = "detached" if current == "HEAD" else current

    status = _run(
        ["git", "-C", str(home), "status", "--porcelain"],
        check=False, capture=True,
    )
    dirty = bool((status.stdout or "").strip()) if status.returncode == 0 else True
    if dirty:
        log_event(
            "git_home_invariant_violation",
            path=str(home),
            invariant="live_branch",
            observed=observed,
            expected="main",
            action="manual_reconcile_required",
            dirty=True,
        )
        return False

    checkout = _run(
        ["git", "-C", str(home), "checkout", "-q", "main"],
        check=False, capture=True,
    )
    if checkout.returncode == 0:
        log_event(
            "git_home_invariant_violation",
            path=str(home),
            invariant="live_branch",
            observed=observed,
            expected="main",
            action="repaired",
        )
        return True

    log_event(
        "git_home_invariant_violation",
        path=str(home),
        invariant="live_branch",
        observed=observed,
        expected="main",
        action="checkout_failed",
        reason=_redact(
            (checkout.stderr or checkout.stdout or "checkout failed")[:500]
        ),
    )
    return False

# ─── helpers ─────────────────────────────────────────────────────────


def _is_dir_effectively_empty(path: Path) -> bool:
    """True only when *path* is strictly empty (no entries) — the
    precondition for cloning into it (git refuses a non-empty target).

    chainlink #259: the body checks strict emptiness; an earlier
    docstring promised "empty OR known-safe scaffold", but no scaffold
    allowlist is implemented. Wording aligned to the actual behavior."""
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


# Re-block entries that every home's ``.gitignore`` must carry even after the
# template stops being reseeded. Existing homes never re-copy the template
# (operators may have hand-edited it), so a template addition otherwise reaches
# new installs only. ``_ensure_gitignore`` appends any missing entry to an
# existing file — idempotent, preserving operator edits — so the addition also
# lands on already-provisioned homes. Each entry: (ignore-line, comment).
_REQUIRED_GITIGNORE_ENTRIES: tuple[tuple[str, str], ...] = (
    (
        "state/worklink/transcripts/",
        "# Worklink backend transcripts are full sub-agent stdout dumps "
        "(MBs/attempt) —\n# debugging material, not durable state; tracking "
        "them grows .git/objects forever.",
    ),
)


def _append_missing_gitignore_entries(target: Path) -> bool:
    """Append any missing :data:`_REQUIRED_GITIGNORE_ENTRIES` to ``target``.

    Idempotent and edit-preserving: reads the existing file, appends only
    entries whose ignore-line is absent (matched trimmed, with or without a
    leading slash), and never rewrites existing content. Returns True iff it
    changed the file. Mirrors ``proposals._ensure_scratch_ignored``.
    """
    try:
        existing = target.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("could not read %s for gitignore upgrade: %s", target, exc)
        return False
    present = {line.strip().strip("/") for line in existing.splitlines()}
    missing = [
        (line, comment)
        for line, comment in _REQUIRED_GITIGNORE_ENTRIES
        if line.strip("/") not in present
    ]
    if not missing:
        return False
    try:
        with target.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for line, comment in missing:
                f.write(f"\n{comment}\n{line}\n")
    except OSError as exc:
        log.warning("could not upgrade %s: %s", target, exc)
        return False
    return True


def _ensure_gitignore(home: Path, result: BootstrapResult) -> None:
    """Copy the gitignore template into ``home/.gitignore`` if missing.

    Doesn't overwrite an existing gitignore — operators may have hand-
    edited it. The template ships under ``mimir/templates/git/gitignore``
    (no leading dot, so the source tree doesn't honor it itself). For an
    existing file, appends any missing required re-block entries in place so
    template additions reach already-provisioned homes (not just new
    installs), preserving operator edits."""
    target = home / ".gitignore"
    if target.exists():
        if _append_missing_gitignore_entries(target):
            result.gitignore_written = True
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


def ensure_workspace_hooks(workspace: Path) -> bool:
    """Install the pre-push staleness-gate hook to a source-code workspace
    repo (e.g., ``/workspace/mimir``).

    Idempotent — overwrites the existing hook so a template update
    propagates on the next server startup. Distinct from
    ``_ensure_hook``, which manages the pre-commit secret-scan hook for
    the state (home) repo: the pre-push hook is for source repos where
    branch staleness matters (squash-merge silently reverts landed work).

    Returns ``True`` if the hook was written, ``False`` on any soft failure
    (missing template, no ``.git/hooks`` dir). Callers should treat False
    as advisory — the workspace is still usable.

    Called unconditionally from ``server._on_startup`` for
    ``/workspace/mimir`` when that path exists; gate is independent of
    ``git_tracking_enabled`` since it protects pushes, not state commits.
    """
    hook_dir = workspace / ".git" / "hooks"
    if not hook_dir.is_dir():
        return False
    src = _TEMPLATES_DIR / "pre-push"
    if not src.is_file():
        log.warning("pre-push template missing at %s — staleness gate not installed", src)
        return False
    target = hook_dir / "pre-push"
    shutil.copyfile(src, target)
    target.chmod(0o755)
    log.debug("pre-push staleness-gate hook installed at %s", target)
    return True


def _install_credential_helper(
    home: Path,
    state_repo: str,
    github_token: str,
    result: BootstrapResult,
) -> None:
    """Write ``<home>/.git/credentials`` with the PAT and configure
    ``credential.helper`` to read from it.

    The helper file lives inside ``.git/`` so it's automatically
    excluded from working-tree operations. Mode 0600 keeps it readable
    only by the running user. Sets ``--local`` config to avoid
    polluting any global ``~/.gitconfig``.

    Idempotent: rewrites the file unconditionally because the token may
    have rotated since last bootstrap; the file size is tiny so the
    write cost is negligible. Same-content writes still bump mtime;
    that's fine — we don't expose mtime anywhere.
    """
    parsed = urllib.parse.urlparse(state_repo)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        # We only support HTTPS; ssh URLs need a different auth shape
        # and aren't covered by spec §"Locked answers" #2.
        log.warning(
            "credential helper not installed: state_repo is not https (%s)",
            parsed.scheme,
        )
        return

    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"

    # Credentials file format: one URL per line, ``user:pass@host``
    # encoded as a URL. ``store`` looks for an exact scheme+host match
    # (path is ignored), so a single line per host works for any number
    # of repos on that host.
    creds_path = home / ".git" / "credentials"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    quoted_user = urllib.parse.quote(_PAT_USERNAME, safe="")
    quoted_token = urllib.parse.quote(github_token, safe="")
    line = f"{parsed.scheme}://{quoted_user}:{quoted_token}@{host}\n"
    # Write atomically: tmp + rename, so a half-written file never
    # exists. Set restrictive mode on the tmp file before rename so
    # the final inode is born 0600 (rather than created 0644 then
    # chmod'd, which has a brief window).
    tmp_path = creds_path.with_suffix(creds_path.suffix + ".tmp")
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(line)
    except Exception:
        # Make sure the tmp file is gone if the write blew up.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    os.replace(str(tmp_path), str(creds_path))
    # Belt-and-suspenders chmod (some filesystems ignore mode in
    # ``os.open``; better to set it twice than miss it).
    try:
        os.chmod(str(creds_path), 0o600)
    except OSError:
        pass

    # Tell git to use this file. ``store --file=<abs-path>`` is the
    # canonical helper-with-arg syntax; git invokes it as
    # ``git credential-store --file=<path>``.
    #
    # Reset the helper chain first. git resolves ``credential.helper`` by
    # CONCATENATING values from system → global → local (a list, not a
    # single value). If the operator's global git config has a helper
    # like ``!gh auth git-credential``, mimir's ``--local`` setting
    # appends to that chain rather than replacing it — git would still
    # consult the global helper, and on a successful clone would write
    # *its* credentials to our store file too. Setting an empty value
    # at the head of the local chain clears the inherited entries
    # (documented behavior; see ``git-config(1)`` under
    # ``credential.helper``).
    #
    # ``--replace-all`` (not a bare set) is load-bearing because
    # bootstrap runs on EVERY container start. After the first run the
    # local ``credential.helper`` is already multi-valued
    # (``[empty, store]``), so a bare ``git config credential.helper ""``
    # fails with "cannot overwrite multiple values with a single value":
    # it leaks that error to stderr AND fails to clear, so the ``--add``
    # below appends another ``store`` entry every boot (observed: 273
    # accumulated entries on a long-lived dev container). ``--replace-all``
    # collapses any existing values to the single empty, making the pair
    # idempotent (``[empty, store]``) and self-healing on next bootstrap.
    helper_value = f"store --file={creds_path}"
    _run(
        ["git", "-C", str(home), "config", "--local", "--replace-all",
         "credential.helper", ""],
        check=False,
    )
    _run(
        ["git", "-C", str(home), "config", "--local", "--add",
         "credential.helper", helper_value],
        check=False,
    )

    result.credentials_written = True


def _ensure_upstream_tracking(
    home: Path,
    log_event: callable,
    result: BootstrapResult,
) -> None:
    """Make sure ``main`` has upstream tracking configured.

    Three cases:

    1. Tracking already configured → no-op.
    2. Remote has ``main`` but local lacks tracking →
       ``git branch --set-upstream-to=origin/main main`` (after a
       targeted fetch so ``origin/main`` exists locally as a ref).
    3. Remote is empty (no ``main`` ref) → ``git push -u origin main``
       to bootstrap the remote AND set tracking in one operation.

    Without this, a fresh ``init`` + remote pair leaves the local
    branch untracked: ``git pull`` rejects with "no tracking
    information for the current branch", and the next debounced push
    from ``git_tracking`` hits "no upstream branch". Both surface as
    algedonic negatives until the operator runs ``git push -u``
    manually. This helper closes the loop autonomously.

    Idempotent: subsequent invocations short-circuit at step 1 once
    tracking is set. Failures are logged but do not raise — bootstrap
    must not block startup on a remote write.
    """
    # Step 1: does ``main`` already have upstream config?
    upstream = _run(
        ["git", "-C", str(home), "rev-parse", "--abbrev-ref",
         "--symbolic-full-name", "main@{upstream}"],
        check=False, capture=True,
    )
    if upstream.returncode == 0 and (upstream.stdout or "").strip() == "origin/main":
        return  # already tracking the correct remote — nothing to do

    if upstream.returncode == 0 and (upstream.stdout or "").strip():
        log_event(
            "git_home_invariant_violation",
            path=str(home),
            invariant="main_upstream",
            observed=(upstream.stdout or "").strip(),
            expected="origin/main",
            action="repairing",
        )

    # Step 2: probe remote for ``main``. If reachable + present we
    # just set tracking; if reachable + absent we'll push -u.
    ls = _run(
        ["git", "-C", str(home), "ls-remote", "--heads", "origin", "main"],
        check=False, capture=True,
    )
    if ls.returncode != 0:
        # Remote unreachable (network / auth). Don't push, don't fail
        # — leave tracking unset and let the next bootstrap retry.
        return
    remote_has_main = bool((ls.stdout or "").strip())

    if remote_has_main:
        # Pull origin/main into the local refs cache so
        # set-upstream-to has something to point at.
        _run(
            ["git", "-C", str(home), "fetch", "--quiet", "origin", "main"],
            check=False, capture=True,
        )
        rc = _run(
            ["git", "-C", str(home), "branch",
             "--set-upstream-to=origin/main", "main"],
            check=False, capture=True,
        )
        if rc.returncode == 0:
            result.upstream_set = True
            log_event(
                "git_upstream_set",
                path=str(home),
                action="tracking_existing_remote",
            )
        return

    # Step 3: remote is empty → initial push -u. Bounded to whatever
    # commits the agent has accumulated locally (typically just the
    # bootstrap commit on first start).
    push = _run(
        ["git", "-C", str(home), "push", "-u", "origin", "main"],
        check=False, capture=True,
    )
    if push.returncode == 0:
        result.initial_push = True
        result.upstream_set = True
        log_event(
            "git_upstream_set",
            path=str(home),
            action="initial_push",
        )
    else:
        # Don't fail bootstrap. The next debounced push from
        # git_tracking will retry, and the operator can run
        # ``git push -u origin main`` manually if needed.
        log_event(
            "git_initial_push_failed",
            path=str(home),
            returncode=push.returncode,
            stderr=_redact((push.stderr or "")[:500]),
        )


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
            stderr=_redact(exc.stderr or "")[:500],
        )
        return False


def _inject_token_into_url(state_repo_url: str, github_token: str) -> str:
    """Internal-only: rewrite an HTTPS git URL to embed the PAT in the
    netloc.

    Used solely for the transient clone subprocess in path 1 (.git/
    doesn't exist yet, so credential helper can't be staged). Caller
    rewrites the remote to the clean URL immediately on success. Never
    persisted anywhere.

    Token is URL-encoded so PATs containing reserved chars (``+``, ``/``,
    ``=``) don't break the netloc parse on the receiving end.
    """
    parsed = urllib.parse.urlparse(state_repo_url)
    if parsed.scheme not in {"http", "https"}:
        return state_repo_url
    quoted = urllib.parse.quote(github_token, safe="")
    new_netloc = f"{quoted}@{parsed.hostname}"
    if parsed.port:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return parsed._replace(netloc=new_netloc).geturl()


def _url_has_embedded_token(url: str) -> bool:
    """Detect a PR4b-style in-URL token. Used during migration to
    decide whether to flag legacy_token_url_migrated. Looks for any
    userinfo component on an https URL — even if it's not a literal
    PAT, anything before ``@`` in the netloc shouldn't be there in
    the canonical clean form."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    # parsed.username is None when there's no userinfo. Any non-None
    # value (including empty string from ``://@host``) means there's an
    # auth component embedded.
    return parsed.username is not None


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Tiny wrapper so we get uniform timeout + capture behavior across
    helpers. 30s timeout matches PUSH_TIMEOUT_SECONDS in git_tracking
    so the bootstrap can't wedge startup on a slow remote.

    CR2 (external I/O) + PR #111 review fix: catch ``TimeoutExpired``
    and translate to a CompletedProcess-or-CalledProcessError that
    matches the caller's ``check`` setting.

    - ``check=True`` → raise ``CalledProcessError(returncode=124)``
      with a structured stderr suffix. Existing
      ``except CalledProcessError`` handlers catch the timeout
      uniformly.
    - ``check=False`` → return ``CompletedProcess(returncode=124)``
      so caller-side returncode inspection still works. Pre-PR-111-
      review, the timeout would raise here too — breaking sites
      like ``_existing_remote_url`` that probe ``rc != 0`` and
      treated CalledProcessError differently from a structured
      non-zero return.

    Pre-fix the timeout escaped as a fatal ``TimeoutExpired`` that
    the server's bare ``except Exception`` swallowed with a one-line
    ``git_bootstrap_failed`` event, leaving partial state.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=check,
            capture_output=capture,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        ) + f"\n[git_bootstrap _run timed out after 30s: {' '.join(cmd)}]"
        if check:
            raise subprocess.CalledProcessError(
                returncode=124,
                cmd=cmd,
                output=stdout,
                stderr=stderr,
            ) from exc
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
        )


__all__: tuple[str, ...] = (
    "BootstrapResult",
    "DEFAULT_USER_EMAIL",
    "DEFAULT_USER_NAME",
    "bootstrap_git_repo",
)
