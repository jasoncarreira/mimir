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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import __version__
from .git_bootstrap import _redact, _run
from .memory_templates import bundled_defaults as bundled_core_defaults
from .models import AgentEvent
from .prompt_templates import (
    bundled_defaults as bundled_prompt_defaults,
    bundled_upgrade_prompts,
)
from .proposals import (
    OpenResult,
    ProposalResult,
    UPGRADE_PROPOSAL_LANE,
    abandon_proposal,
    default_branch_name,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
    _ensure_scratch_ignored,
)

log = logging.getLogger(__name__)

DEFAULTS_VENDOR_BRANCH = "mimir-defaults"
UPGRADE_STATE_DIR = Path(".mimir") / "upgrade-defaults"
LAST_SYNCED_VERSION_FILE = UPGRADE_STATE_DIR / "last-synced-version"
PENDING_PREVIOUS_REF = "refs/mimir/defaults-upgrade/previous"
VENDOR_WORKTREE_REL = Path("scratch") / "defaults-vendor"
UPGRADE_TRIGGER = "upgrade"
UPGRADE_CHANNEL_PREFIX = "upgrade:"
UPGRADE_PROMPT_TEMPLATE = "upgrade.md"
AUTO_SUBMIT_CLEAN_ENV = "MIMIR_DEFAULTS_UPGRADE_AUTO_SUBMIT_CLEAN"

# Version-specific upgrade prompts (chainlink #645): one-shot migration nudges
# dispatched on the ``upgrade-prompt:<version>`` synthetic channel.
UPGRADE_PROMPT_CHANNEL_PREFIX = "upgrade-prompt:"
# ``check_and_open_defaults_upgrade`` actions that mean this run advanced
# last-synced-version to the new version (a real bump was consumed). Upgrade
# prompts dispatch ONLY on these, so they fire once per bump — never on
# already_synced / proposal_exists / skip_* / baseline_initialized / error.
UPGRADE_PROMPT_DISPATCH_ACTIONS = frozenset({
    "no_changes",
    "vendor_updated_no_home_diff",
    "auto_submitted",
    "proposal_opened",
    "proposal_opened_conflicts",
})


@dataclass
class DefaultsUpgradeResult:
    """Outcome from one defaults-upgrade check."""

    ok: bool
    action: str
    version: str
    detail: str | None = None
    proposal: OpenResult | None = None
    conflicts: bool = False
    auto_submit: ProposalResult | None = None


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


def read_last_synced_version(home: Path) -> str | None:
    """Public read of the recorded last-synced defaults version (``None`` if
    unset). Callers capture this *before* ``check_and_open_defaults_upgrade``
    advances it, to know the version being upgraded *from*."""
    return _read_last_synced_version(home)


def _env_bool_value(raw: str | None, *, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    norm = raw.strip().lower()
    if norm in {"1", "true", "yes", "on", "y"}:
        return True
    if norm in {"0", "false", "no", "off", "n"}:
        return False
    log.warning("%s=%r is not a recognised boolean; using default %r", AUTO_SUBMIT_CLEAN_ENV, raw, default)
    return default


def _read_prompt_template(home: Path, name: str) -> str:
    """Read an operator-customized prompt template, falling back to bundled text."""
    target = home / "prompts" / name
    try:
        if target.is_file():
            return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("could not read upgrade prompt template %s: %s", target, exc)
    bundled = bundled_prompt_defaults().get(name, "")
    return bundled.strip()


SCHEDULER_YAML_REL = Path("scheduler.yaml")


def _shipped_default_files() -> dict[Path, str]:
    """Return shipped default files keyed by their home-relative paths.

    ``scheduler.yaml`` rides along as the bundled *template* so the vendor
    branch carries a per-version baseline of the default tick set. It is NOT
    line-merged like the prompts/core surfaces (``_apply_defaults_three_way``
    skips it); the job-aware ``_reconcile_scheduler_yaml`` uses those baselines
    to detect newly-added default ticks.
    """
    from .scheduler import bundled_scheduler_template_text

    out: dict[Path, str] = {}
    for name, text in bundled_core_defaults().items():
        out[Path("memory") / "core" / name] = text
    for name, text in bundled_prompt_defaults().items():
        out[Path("prompts") / name] = text
    template = bundled_scheduler_template_text()
    if template:
        out[SCHEDULER_YAML_REL] = template
    return dict(sorted(out.items(), key=lambda item: item[0].as_posix()))


def _prepare_vendor_worktree(home: Path, branch: str) -> tuple[Path | None, str | None]:
    """Create a temporary worktree with ``branch`` checked out.

    The vendor rewrite happens in ``scratch/defaults-vendor`` rather than the
    live home checkout so the operation never deletes or dirties runtime state
    files while constructing the defaults-only tree. Ensure ``scratch/`` is
    ignored before creating the worktree so a later home-level ``git add -A``
    cannot capture the embedded repository even if startup ordering changes.
    """
    ignore_err = _ensure_scratch_ignored(home)
    if ignore_err:
        return None, ignore_err
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


def _conflict_block(text: str) -> str:
    """Ensure a conflict-region side ends on its own line.

    The ``=======`` / ``>>>>>>>`` markers must each start a fresh line. Operator
    home files (``ours``) are read raw and are not guaranteed to end in a
    newline, so without this the marker glues onto the last content line and the
    result is not a well-formed conflict block.
    """
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


def _write_conflict(path: Path, ours: str, theirs: str, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<<<<<<< home\n{_conflict_block(ours)}"
        f"=======\n{_conflict_block(theirs)}"
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
    # ``git merge-file`` exits 0 when the merge is clean, otherwise the number
    # of conflict regions (saturated at 127); 255 (or a negative signal code)
    # means a genuine failure. A file the operator edited in several separated
    # spots that the new defaults also changed yields exit >= 2 — that is a
    # normal multi-conflict result, NOT an error, so keep the conflict-marked
    # stdout instead of discarding it and aborting the whole upgrade.
    if 0 <= res.returncode <= 127:
        return res.stdout or "", res.returncode > 0, None
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


def _merge_scheduler_defaults(
    *, base_text: str | None, their_text: str, home_text: str, version: str
) -> tuple[str | None, list[str]]:
    """Job-aware reconciliation of NEW default scheduled ticks (chainlink #666).

    ``scheduler.yaml`` is far more operator-customized than the prompt/core
    surfaces (edited crons/priorities, operator-added jobs), so a line-based
    3-way merge would clobber it. Instead, match jobs by ``name``:

    - ADD default jobs that are *new in the current template* (present in
      ``their_text``, absent from the previous-template ``base_text``) and not
      already present in the home — the main goal (e.g. a new memory-hygiene
      tick reaching an existing home).
    - PRESERVE operator-customized existing jobs and operator-added custom
      jobs: home entries are never modified or reordered.
    - NEVER remove a job, including a default the home dropped (a dropped
      default is in ``base_text``, so it is not "new" and is not re-added).

    The home file is appended to as TEXT (not re-serialized) so operator
    comments/formatting survive. Returns ``(new_home_text, added_names)``;
    ``new_home_text`` is None when there is nothing to add.

    ``base_text`` None means there is no prior template baseline (the run that
    first ships ``scheduler.yaml`` into the vendor branch). That is a deliberate
    no-op: without a baseline we cannot tell a genuinely-new default from one
    the home dropped before this feature existed, and the same release's upgrade
    prompt is the canonical first-delivery path. New ticks added in *future*
    versions reconcile normally.
    """
    if base_text is None:
        return None, []
    from .scheduler import load_jobs_from_text

    base_names = {j.name for j in load_jobs_from_text(base_text)}
    their_jobs = load_jobs_from_text(their_text)
    home_names = {j.name for j in load_jobs_from_text(home_text)}
    to_add = [
        j for j in their_jobs
        if j.name not in base_names and j.name not in home_names
    ]
    if not to_add:
        return None, []

    import yaml

    addition = yaml.safe_dump(
        [j.to_yaml_entry() for j in to_add],
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    banner = (
        f"# --- added by mimir {version} defaults upgrade: new default "
        f"tick(s) — edit cron/priority/channel or remove as needed ---\n"
    )
    stripped = home_text.rstrip("\n")
    if not stripped or stripped == "[]":
        new_text = banner + addition
    else:
        new_text = f"{stripped}\n\n{banner}{addition}"
    return new_text, [j.name for j in to_add]


def _reconcile_scheduler_yaml(
    worktree: Path,
    *,
    previous_ref: str,
    current_ref: str,
    version: str,
) -> tuple[bool, list[str]]:
    """Stage new-default-tick additions to ``scheduler.yaml`` in ``worktree``.

    Reads the previous/current bundled template from the vendor refs and the
    home file from the proposal worktree, then applies ``_merge_scheduler_defaults``
    and stages the result. Returns ``(changed, added_job_names)``.
    """
    rel = SCHEDULER_YAML_REL
    home_path = worktree / rel
    if not home_path.exists():
        # scheduler.yaml is not tracked in this home — don't fabricate one.
        return False, []
    their_text = _git_file(worktree, current_ref, rel)
    if their_text is None:
        return False, []  # template not yet in the vendor branch
    base_text = _git_file(worktree, previous_ref, rel)  # None on the introduction run
    try:
        home_text = home_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("scheduler reconcile: could not read %s: %s", home_path, exc)
        return False, []
    new_text, added = _merge_scheduler_defaults(
        base_text=base_text, their_text=their_text, home_text=home_text, version=version,
    )
    if new_text is None or not added:
        return False, []
    home_path.write_text(new_text, encoding="utf-8")
    add = _git(["add", rel.as_posix()], cwd=worktree)
    if add.returncode != 0:
        log.warning(
            "scheduler reconcile: git add failed: %s",
            _redact((add.stderr or add.stdout or "").strip()),
        )
        return False, []
    return True, added


def check_and_open_defaults_upgrade(
    home: Path,
    *,
    version: str | None = None,
    base: str = "main",
    vendor_branch: str = DEFAULTS_VENDOR_BRANCH,
    auto_submit_clean: bool | None = None,
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
      reconciled against home files via git's native ``merge-file`` 3-way;
    - optionally, a conflict-free proposal can be submitted immediately by
      setting ``auto_submit_clean`` (or ``MIMIR_DEFAULTS_UPGRADE_AUTO_SUBMIT_CLEAN``).
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

    # Job-aware scheduler.yaml reconciliation (chainlink #666): propose any
    # newly-added default ticks. Append-only and never conflicting, so it only
    # feeds has_diff — it cannot turn a clean upgrade into a conflicted one.
    sched_changed, sched_added = _reconcile_scheduler_yaml(
        opened.worktree,
        previous_ref=previous_ref,
        current_ref=vendor.current_ref,
        version=version,
    )
    if sched_added:
        log.info("scheduler defaults reconciled: added ticks %s", ", ".join(sched_added))
    has_diff = has_diff or sched_changed

    if not has_diff:
        abandon_proposal(home, lane=UPGRADE_PROPOSAL_LANE)
        _write_last_synced_version(home, version)
        _delete_ref(home, PENDING_PREVIOUS_REF)
        return DefaultsUpgradeResult(ok=True, action="vendor_updated_no_home_diff", version=version)

    if auto_submit_clean is None:
        import os
        auto_submit_clean = _env_bool_value(os.environ.get(AUTO_SUBMIT_CLEAN_ENV), default=False)
    sched_note = (
        f" New default scheduled tick(s) added to scheduler.yaml: {', '.join(sched_added)}."
        if sched_added
        else ""
    )
    if auto_submit_clean and not conflicts:
        submitted = finalize_proposal(
            home,
            title=f"Upgrade mimir defaults to {version}",
            rationale=(
                "Automatically proposed a conflict-free shipped-defaults upgrade for "
                "memory/core/ and prompts/." + sched_note + " Approval remains "
                "operator-controlled: merge the PR to apply these files to the live home."
            ),
            lane=UPGRADE_PROPOSAL_LANE,
        )
        if submitted.ok:
            _write_last_synced_version(home, version)
            _delete_ref(home, PENDING_PREVIOUS_REF)
            return DefaultsUpgradeResult(
                ok=True,
                action="auto_submitted",
                version=version,
                proposal=opened,
                conflicts=False,
                auto_submit=submitted,
            )
        # Leave the proposal open and spend a reconciliation turn rather than
        # strand a valid worktree just because PR creation/push failed.
        return DefaultsUpgradeResult(
            ok=True,
            action="proposal_opened",
            version=version,
            detail=submitted.detail or submitted.reason,
            proposal=opened,
            conflicts=False,
            auto_submit=submitted,
        )

    _write_last_synced_version(home, version)
    _delete_ref(home, PENDING_PREVIOUS_REF)
    return DefaultsUpgradeResult(
        ok=True,
        action="proposal_opened_conflicts" if conflicts else "proposal_opened",
        version=version,
        detail=sched_note.strip() or None,
        proposal=opened,
        conflicts=conflicts,
    )


async def enqueue_upgrade_reconciliation_turn(
    home: Path,
    result: DefaultsUpgradeResult,
    enqueue: Callable[[AgentEvent], Awaitable[bool]],
) -> bool:
    """Fire the agent-facing upgrade turn when reconciliation is needed.

    The S3 startup hook opens the upgrade proposal worktree; this S4-ish wake
    gives the agent the work item with a purpose-built prompt. We only spend a
    turn for proposals that still need reconciliation (conflicts) or for clean
    proposals that were not auto-submitted because the operator kept the HITL
    gate enabled.
    """
    if not result.ok or result.proposal is None or result.proposal.worktree is None:
        return False
    if result.action not in {"proposal_opened", "proposal_opened_conflicts"}:
        return False

    home = Path(home).resolve()
    prompt = _read_prompt_template(home, UPGRADE_PROMPT_TEMPLATE)
    if not prompt:
        prompt = (
            "# Upgrade defaults reconciliation\n\n"
            "Review the open upgrade-lane proposal worktree, reconcile any "
            "memory/core and prompts changes, resolve conflict markers, then "
            "submit_proposal with lane='upgrade'. After submitting, notify the "
            "operator so the PR doesn't sit unreviewed: send_message on the "
            "operator alert channel (pass an explicit channel_id — this is a "
            "non-interactive turn) with the PR URL and a one-line summary. "
            "Skip the notification if no operator alert channel is configured."
        )
    worktree = result.proposal.worktree
    branch = result.proposal.branch
    replacements = {
        "{version}": result.version,
        "{action}": result.action,
        "{branch}": branch or "(unknown)",
        "{worktree}": str(worktree),
        "{conflicts}": str(result.conflicts).lower(),
    }
    content = prompt
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    event = AgentEvent(
        trigger=UPGRADE_TRIGGER,
        channel_id=f"{UPGRADE_CHANNEL_PREFIX}{result.version}",
        content=content,
        source="system",
        extra={
            "version": result.version,
            "action": result.action,
            "proposal_branch": branch,
            "proposal_worktree": str(worktree),
            "conflicts": result.conflicts,
        },
    )
    return await enqueue(event)


def pending_upgrade_prompt_versions(
    previous: str | None, current: str | None
) -> list[str]:
    """Target versions whose upgrade prompt should run for ``previous -> current``.

    Returns the bundled ``upgrades/<v>.md`` versions with ``previous < v <=
    current``, ascending. Cumulative: a skip upgrade (0.6.4 -> 0.6.7) runs
    every intermediate prompt that exists. Empty when ``previous`` is None
    (fresh install — not an upgrade), a version is unparseable, or there is
    no forward movement.
    """
    if not previous or not current:
        return []
    try:
        prev_v = Version(previous)
        cur_v = Version(current)
    except InvalidVersion:
        return []
    if prev_v >= cur_v:
        return []
    matched: list[tuple[Version, str]] = []
    for name in bundled_upgrade_prompts():
        try:
            v = Version(name)
        except InvalidVersion:
            continue
        if prev_v < v <= cur_v:
            matched.append((v, name))
    return [name for _, name in sorted(matched)]


async def enqueue_upgrade_prompt_turns(
    home: Path,
    *,
    previous: str | None,
    current: str,
    action: str,
    enqueue: Callable[[AgentEvent], Awaitable[bool]],
) -> int:
    """Dispatch version-specific upgrade prompts for a consumed version bump.

    Fires only when ``action`` advanced last-synced-version (see
    ``UPGRADE_PROMPT_DISPATCH_ACTIONS``), so each prompt runs once per bump.
    Every matching ``upgrades/<v>.md`` becomes one turn on the
    ``upgrade-prompt:<v>`` synthetic channel, oldest target first. Returns the
    number enqueued; no match (or a non-bump action) is a clean no-op.
    Idempotent across restart: the next startup sees ``last-synced == current``
    and the bump action no longer fires.
    """
    if action not in UPGRADE_PROMPT_DISPATCH_ACTIONS:
        return 0
    versions = pending_upgrade_prompt_versions(previous, current)
    if not versions:
        return 0
    prompts = bundled_upgrade_prompts()
    enqueued = 0
    for ver in versions:
        text = (prompts.get(ver) or "").strip()
        if not text:
            continue
        content = (
            text.replace("{from}", previous or "(unknown)")
            .replace("{to}", current)
            .replace("{version}", ver)
        )
        event = AgentEvent(
            trigger=UPGRADE_TRIGGER,
            channel_id=f"{UPGRADE_PROMPT_CHANNEL_PREFIX}{ver}",
            content=content,
            source="system",
            extra={
                "upgrade_prompt_version": ver,
                "from_version": previous,
                "to_version": current,
            },
        )
        if await enqueue(event):
            enqueued += 1
    return enqueued


__all__ = (
    "AUTO_SUBMIT_CLEAN_ENV",
    "DEFAULTS_VENDOR_BRANCH",
    "LAST_SYNCED_VERSION_FILE",
    "UPGRADE_CHANNEL_PREFIX",
    "UPGRADE_PROMPT_CHANNEL_PREFIX",
    "UPGRADE_PROMPT_DISPATCH_ACTIONS",
    "UPGRADE_PROMPT_TEMPLATE",
    "UPGRADE_TRIGGER",
    "DefaultsUpgradeResult",
    "check_and_open_defaults_upgrade",
    "enqueue_upgrade_reconciliation_turn",
    "enqueue_upgrade_prompt_turns",
    "pending_upgrade_prompt_versions",
    "read_last_synced_version",
)
