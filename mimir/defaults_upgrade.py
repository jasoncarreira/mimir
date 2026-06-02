"""Version-triggered upgrade proposals for shipped prompts/core defaults.

Mimir homes keep operator-editable copies of bundled prompt templates under
``prompts/`` and bundled core-memory defaults under ``memory/core/``. Setup and
startup intentionally seed-if-missing so operator edits are never overwritten in
place. This module supplies the upgrade path: keep a git vendor branch with the
*shipped defaults only*, then use git's native 3-way merge-file machinery to
reconcile new package defaults against the operator's home files in an upgrade
lane proposal.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .git_bootstrap import _redact, _run
from .memory_templates import bundled_defaults as bundled_core_defaults
from .prompt_templates import bundled_defaults as bundled_prompt_defaults
from .proposals import (
    OpenResult,
    UPGRADE_PROPOSAL_LANE,
    abandon_proposal,
    default_branch_name,
    list_open_proposals,
    open_proposal,
)

log = logging.getLogger(__name__)

DEFAULTS_VENDOR_BRANCH = "mimir-defaults"
UPGRADE_STATE_DIR = Path(".mimir") / "upgrade-defaults"
LAST_SYNCED_VERSION_FILE = UPGRADE_STATE_DIR / "last-synced-version"
PENDING_PREVIOUS_REF = "refs/mimir/defaults-upgrade/previous"
VENDOR_WORKTREE_REL = Path("scratch") / "defaults-vendor"


@dataclass
class DefaultsUpgradeResult:
    """Outcome from one defaults-upgrade check."""

    ok: bool
    action: str
    version: str
    detail: str | None = None
    proposal: OpenResult | None = None
    conflicts: bool = False


@dataclass
class VendorSyncResult:
    """Outcome of rewriting the vendor defaults branch."""

    changed: bool
    previous_ref: str | None
    current_ref: str | None
    error: str | None = None


def _git(args: list[str], cwd: Path):
    return _run(["git", *args], cwd=cwd, capture=True)


def _has_origin_remote(home: Path) -> bool:
    res = _git(["remote", "get-url", "origin"], cwd=home)
    return res.returncode == 0 and bool((res.stdout or "").strip())


def _branch_ref(repo: Path, branch: str) -> str | None:
    res = _git(["rev-parse", "--verify", branch], cwd=repo)
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def _set_ref(repo: Path, ref: str, target: str) -> str | None:
    res = _git(["update-ref", ref, target], cwd=repo)
    if res.returncode != 0:
        return _redact((res.stderr or res.stdout or "git update-ref failed").strip())
    return None


def _delete_ref(repo: Path, ref: str) -> None:
    _git(["update-ref", "-d", ref], cwd=repo)


def _state_file(home: Path) -> Path:
    return home / LAST_SYNCED_VERSION_FILE


def _read_last_synced_version(home: Path) -> str | None:
    path = _state_file(home)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("could not read defaults-upgrade state %s: %s", path, exc)
        return None
    return text or None


def _write_last_synced_version(home: Path, version: str) -> None:
    path = _state_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{version}\n", encoding="utf-8")


def _shipped_default_files() -> dict[Path, str]:
    """Return shipped default files keyed by their home-relative paths."""
    out: dict[Path, str] = {}
    for name, text in bundled_core_defaults().items():
        out[Path("memory") / "core" / name] = text
    for name, text in bundled_prompt_defaults().items():
        out[Path("prompts") / name] = text
    return dict(sorted(out.items(), key=lambda item: item[0].as_posix()))


def _prepare_vendor_worktree(home: Path, branch: str) -> tuple[Path | None, str | None]:
    """Create a temporary worktree with ``branch`` checked out.

    The vendor rewrite happens in ``scratch/defaults-vendor`` rather than the
    live home checkout so the operation never deletes or dirties runtime state
    files while constructing the defaults-only tree.
    """
    _git(["worktree", "prune"], cwd=home)
    wt = home / VENDOR_WORKTREE_REL
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=home)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)

    if _branch_ref(home, branch) is not None:
        add = _git(["worktree", "add", str(wt), branch], cwd=home)
        if add.returncode != 0:
            return None, _redact((add.stderr or add.stdout or "git worktree add failed").strip())
    else:
        add = _git(["worktree", "add", "--detach", str(wt), "HEAD"], cwd=home)
        if add.returncode != 0:
            return None, _redact((add.stderr or add.stdout or "git worktree add failed").strip())
        orphan = _git(["checkout", "--orphan", branch], cwd=wt)
        if orphan.returncode != 0:
            return None, _redact((orphan.stderr or orphan.stdout or "git checkout --orphan failed").strip())
    return wt, None


def _wipe_worktree_to_defaults(wt: Path) -> str | None:
    """Remove all tracked/untracked files from the vendor worktree."""
    tracked = _git(["ls-files", "--error-unmatch", "."], cwd=wt)
    if tracked.returncode == 0:
        rm = _git(["rm", "-q", "-f", "-r", "--", "."], cwd=wt)
        if rm.returncode != 0:
            return _redact((rm.stderr or rm.stdout or "git rm failed").strip())

    for child in wt.iterdir():
        if child.name == ".git":  # linked worktree marker file
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                return f"could not remove {child.name}: {exc}"
    return None


def _write_defaults_tree(root: Path, files: dict[Path, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _ensure_vendor_branch(
    home: Path,
    *,
    branch: str = DEFAULTS_VENDOR_BRANCH,
    version: str,
) -> VendorSyncResult:
    """Rewrite ``branch`` to the installed package's shipped defaults."""
    previous_ref = _branch_ref(home, branch)
    wt, err = _prepare_vendor_worktree(home, branch)
    if err or wt is None:
        return VendorSyncResult(False, previous_ref, previous_ref, err)
    try:
        err = _wipe_worktree_to_defaults(wt)
        if err:
            return VendorSyncResult(False, previous_ref, previous_ref, err)
        _write_defaults_tree(wt, _shipped_default_files())
        add = _git(["add", "-A"], cwd=wt)
        if add.returncode != 0:
            return VendorSyncResult(
                False, previous_ref, previous_ref,
                _redact((add.stderr or add.stdout or "git add failed").strip()),
            )

        diff = _git(["diff", "--cached", "--quiet"], cwd=wt)
        if diff.returncode == 0:
            return VendorSyncResult(False, previous_ref, _branch_ref(wt, "HEAD"), None)
        commit = _git(["commit", "-q", "-m", f"mimir defaults {version}"], cwd=wt)
        if commit.returncode != 0:
            return VendorSyncResult(
                False, previous_ref, previous_ref,
                _redact((commit.stderr or commit.stdout or "git commit failed").strip()),
            )
        return VendorSyncResult(True, previous_ref, _branch_ref(wt, "HEAD"), None)
    finally:
        _git(["worktree", "remove", "--force", str(wt)], cwd=home)
        _git(["worktree", "prune"], cwd=home)


def _git_file(repo: Path, ref: str, rel: Path) -> str | None:
    res = _git(["show", f"{ref}:{rel.as_posix()}"], cwd=repo)
    if res.returncode != 0:
        return None
    return res.stdout or ""


def _git_files(repo: Path, ref: str) -> set[Path]:
    res = _git(["ls-tree", "-r", "--name-only", ref], cwd=repo)
    if res.returncode != 0:
        return set()
    return {Path(line) for line in (res.stdout or "").splitlines() if line.strip()}


def _write_conflict(path: Path, ours: str, theirs: str, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<<<<<<< home\n{ours}"
        f"=======\n{theirs}"
        f">>>>>>> {label}\n",
        encoding="utf-8",
    )


def _merge_file(ours: str, base: str, theirs: str, *, label: str, cwd: Path) -> tuple[str, bool, str | None]:
    with tempfile.TemporaryDirectory(dir=cwd) as td:
        tdir = Path(td)
        ours_p = tdir / "ours"
        base_p = tdir / "base"
        theirs_p = tdir / "theirs"
        ours_p.write_text(ours, encoding="utf-8")
        base_p.write_text(base, encoding="utf-8")
        theirs_p.write_text(theirs, encoding="utf-8")
        res = _run(
            [
                "git", "merge-file", "-p",
                "-L", "home",
                "-L", "previous defaults",
                "-L", label,
                str(ours_p), str(base_p), str(theirs_p),
            ],
            cwd=cwd,
            capture=True,
        )
    if res.returncode in (0, 1):
        return res.stdout or "", res.returncode == 1, None
    return ours, False, _redact((res.stderr or res.stdout or "git merge-file failed").strip())


def _apply_defaults_three_way(
    worktree: Path,
    *,
    previous_ref: str,
    current_ref: str,
    label: str = DEFAULTS_VENDOR_BRANCH,
) -> tuple[bool, bool, str | None]:
    """Apply shipped-default changes to ``worktree`` with explicit 3-way bases."""
    paths = _git_files(worktree, previous_ref) | _git_files(worktree, current_ref)
    changed = False
    conflicts = False
    for rel in sorted(paths, key=lambda p: p.as_posix()):
        is_surface = rel.parts[:2] == ("memory", "core") or rel.parts[:1] == ("prompts",)
        if not is_surface:
            continue
        path = worktree / rel
        base_text = _git_file(worktree, previous_ref, rel)
        their_text = _git_file(worktree, current_ref, rel)
        ours_exists = path.exists()
        ours_text = path.read_text(encoding="utf-8") if ours_exists else None

        if their_text is None:
            if base_text is None or ours_text is None:
                continue
            if ours_text == base_text:
                rm = _git(["rm", "-q", "--", rel.as_posix()], cwd=worktree)
                if rm.returncode != 0 and path.exists():
                    path.unlink()
                    _git(["add", "-A", rel.as_posix()], cwd=worktree)
            else:
                _write_conflict(path, ours_text, "", label=label)
                _git(["add", rel.as_posix()], cwd=worktree)
                conflicts = True
            changed = True
            continue

        if base_text is None:
            if ours_text is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(their_text, encoding="utf-8")
                _git(["add", rel.as_posix()], cwd=worktree)
                changed = True
            elif ours_text != their_text:
                _write_conflict(path, ours_text, their_text, label=label)
                _git(["add", rel.as_posix()], cwd=worktree)
                changed = True
                conflicts = True
            continue

        if ours_text == their_text or their_text == base_text:
            continue
        if ours_text == base_text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(their_text, encoding="utf-8")
            _git(["add", rel.as_posix()], cwd=worktree)
            changed = True
            continue
        if ours_text is None:
            ours_text = ""

        merged, had_conflict, err = _merge_file(ours_text, base_text, their_text, label=label, cwd=worktree)
        if err:
            return False, False, err
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged, encoding="utf-8")
        _git(["add", rel.as_posix()], cwd=worktree)
        changed = True
        conflicts = conflicts or had_conflict

    diff = _git(["diff", "--cached", "--quiet"], cwd=worktree)
    return diff.returncode != 0 or changed, conflicts, None


def check_and_open_defaults_upgrade(
    home: Path,
    *,
    version: str | None = None,
    base: str = "main",
    vendor_branch: str = DEFAULTS_VENDOR_BRANCH,
) -> DefaultsUpgradeResult:
    """Update the defaults vendor branch and open an upgrade-lane proposal.

    Safe/idempotent startup hook:
    - homes without an ``origin`` remote skip (setup's seed-if-missing remains the
      behavior until there is somewhere to send proposal PRs);
    - the installed package version is compared to ``.mimir/upgrade-defaults``;
    - the vendor branch is rewritten to shipped ``prompts/`` + ``memory/core/``;
    - the first run bootstraps the vendor branch as the baseline and records the
      current version (there is no prior defaults tree to merge from yet);
    - subsequent changed versions open an upgrade-lane proposal with new defaults
      reconciled against home files via git's native ``merge-file`` 3-way.
    """
    home = Path(home).resolve()
    version = version or __version__
    if not (home / ".git").exists():
        return DefaultsUpgradeResult(ok=True, action="skip_no_git", version=version)
    if not _has_origin_remote(home):
        return DefaultsUpgradeResult(ok=True, action="skip_no_remote", version=version)
    last = _read_last_synced_version(home)
    if last == version:
        return DefaultsUpgradeResult(ok=True, action="already_synced", version=version)

    if list_open_proposals(home, lane=UPGRADE_PROPOSAL_LANE):
        return DefaultsUpgradeResult(
            ok=True,
            action="proposal_exists",
            version=version,
            detail="upgrade proposal already open; not overwriting it",
        )

    vendor = _ensure_vendor_branch(home, branch=vendor_branch, version=version)
    if vendor.error:
        return DefaultsUpgradeResult(ok=False, action="error", version=version, detail=vendor.error)
    pending_previous = _branch_ref(home, PENDING_PREVIOUS_REF)
    if vendor.previous_ref is None and pending_previous is None:
        _write_last_synced_version(home, version)
        return DefaultsUpgradeResult(ok=True, action="baseline_initialized", version=version)
    previous_ref = pending_previous or vendor.previous_ref
    if previous_ref is None:
        return DefaultsUpgradeResult(ok=False, action="error", version=version, detail="previous defaults ref missing")
    if vendor.current_ref is None:
        return DefaultsUpgradeResult(ok=False, action="error", version=version, detail="vendor branch ref missing after sync")
    if not vendor.changed and pending_previous is None:
        _write_last_synced_version(home, version)
        return DefaultsUpgradeResult(ok=True, action="no_changes", version=version)
    if vendor.changed and pending_previous is None:
        err = _set_ref(home, PENDING_PREVIOUS_REF, previous_ref)
        if err:
            return DefaultsUpgradeResult(ok=False, action="error", version=version, detail=err)

    proposal_branch = default_branch_name(f"defaults-{version}", lane=UPGRADE_PROPOSAL_LANE)
    opened = open_proposal(home, base=base, branch=proposal_branch, lane=UPGRADE_PROPOSAL_LANE)
    if not opened.ok or opened.worktree is None:
        return DefaultsUpgradeResult(
            ok=False,
            action="error",
            version=version,
            detail=opened.detail or opened.reason,
            proposal=opened,
        )

    has_diff, conflicts, merge_error = _apply_defaults_three_way(
        opened.worktree,
        previous_ref=previous_ref,
        current_ref=vendor.current_ref,
        label=vendor_branch,
    )
    if merge_error:
        abandon_proposal(home, lane=UPGRADE_PROPOSAL_LANE)
        return DefaultsUpgradeResult(ok=False, action="error", version=version, detail=merge_error, proposal=opened)
    if not has_diff:
        abandon_proposal(home, lane=UPGRADE_PROPOSAL_LANE)
        _write_last_synced_version(home, version)
        _delete_ref(home, PENDING_PREVIOUS_REF)
        return DefaultsUpgradeResult(ok=True, action="vendor_updated_no_home_diff", version=version)

    _write_last_synced_version(home, version)
    _delete_ref(home, PENDING_PREVIOUS_REF)
    return DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened_conflicts" if conflicts else "proposal_opened",
        version=version,
        proposal=opened,
        conflicts=conflicts,
    )


__all__ = (
    "DEFAULTS_VENDOR_BRANCH",
    "LAST_SYNCED_VERSION_FILE",
    "DefaultsUpgradeResult",
    "check_and_open_defaults_upgrade",
)
