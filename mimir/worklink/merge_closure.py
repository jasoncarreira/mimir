"""Conservative, durable reconciliation of merged Worklink leaf PRs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

import yaml

from ..redaction import redact_text
from ..repository_config import RepositoryConfig, RepositoryInventory
from .backends import WorklinkConfig
from .checkout import _default_runner
from .dispatch_failures import (
    active_failure_identities,
    current_failure_identity,
    dispatch_failure_state_dir,
    merge_reconciliation_transaction,
    record_merge_reconciliation_notice,
    resolve_failure_if_current,
    resolve_merge_reconciliation_notices,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_PR_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"pull/(?P<number>[1-9][0-9]*)/?"
)
_COMPLETION = re.compile(r"Closes chainlink #([1-9][0-9]*)\.", re.IGNORECASE)
_CLOSING_CLAUSE = re.compile(
    r"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\b"
    r"(?:\s*:\s*|\s+)(?:#\d+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)",
    re.IGNORECASE,
)
_FORBIDDEN_WORDS = (
    "no", "not", "never", "don't", "doesn't", "cannot", "can't", "won't",
    "partial", "partially", "incomplete", "unfinished", "remaining",
    "remaining-work", "follow-up", "followup", "todo", "wip", "refs",
    "stack", "stacked", "epic",
)
_FORBIDDEN = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(word) for word in _FORBIDDEN_WORDS) + r")(?![\w-])",
    re.IGNORECASE,
)
_EVIDENCE_NAME = re.compile(r"(?P<issue>[1-9][0-9]*)-(?P<attempt>[1-9][0-9]*)\.json")
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_COMPETING_LABELS = frozenset({"worklink:ready", "worklink:in-progress", "worklink:blocked"})
MAX_INCIDENT_RETIREMENTS_PER_SWEEP = 100


class ClosureReadError(RuntimeError):
    """A required tracker, forge, repository, or durable-state read was unsafe."""


@dataclass(frozen=True)
class IssueSnapshot:
    issue_id: int
    status: str
    labels: frozenset[str]
    comments: tuple[str, ...]
    parent_id: int | None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(frozen=True)
class CompletionDecision:
    qualifies: bool
    reason: str


@dataclass(frozen=True)
class PrSnapshot:
    url: str
    slug: str
    number: int
    body: str
    state: str
    merged: bool
    merged_at: str | None
    merge_commit_sha: str | None
    base_slug: str
    base_ref: str


@dataclass(frozen=True)
class AssociationDecision:
    pr_url: str | None
    evidence_paths: tuple[Path, ...]
    evidence_digests: tuple[str, ...]
    completed_evidence_paths: tuple[Path, ...]
    active_base_refs: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ClosureOutcome:
    issue_id: int
    pr_url: str
    merged_at: str
    merge_commit_sha: str


def _checked_json(result: subprocess.CompletedProcess[str], source: str) -> object:
    if result.returncode != 0:
        detail = redact_text((result.stderr or result.stdout or "command failed").strip())[:500]
        raise ClosureReadError(f"{source} failed: {detail}")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ClosureReadError(f"{source} returned malformed JSON") from exc


def _invoke(runner: Runner, args: Sequence[str], source: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClosureReadError(f"{source} failed: {type(exc).__name__}") from exc


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ClosureReadError(f"{field} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ClosureReadError(f"{field} must be a positive integer") from exc
    if parsed < 1 or str(value).strip() != str(parsed):
        raise ClosureReadError(f"{field} must be a positive integer")
    return parsed


def _origin_slug(value: str) -> str | None:
    match = re.fullmatch(
        r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
        value,
    )
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


def resolve_completion_repository(home: Path, *, runner: Runner) -> RepositoryConfig:
    """Resolve and attest the one configured writable completion repository."""
    inventory_path = home / "repositories.yaml"
    if not inventory_path.is_file():
        raise ClosureReadError("repository trust unavailable: repositories.yaml is required")
    try:
        inventory = RepositoryInventory.load(inventory_path)
        selected = WorklinkConfig.load(home / "worklink.yaml").repository
    except (OSError, RuntimeError, TypeError, ValueError, OverflowError, yaml.YAMLError) as exc:
        raise ClosureReadError(f"repository trust unavailable: {exc}") from exc
    if not inventory.declared or not selected:
        raise ClosureReadError("repository trust unavailable: Worklink repository is not declared")
    repository = inventory.repository(selected)
    if repository is None:
        raise ClosureReadError(f"repository trust unavailable: {selected!r} is not in inventory")
    if repository.mode != "rw":
        raise ClosureReadError("repository trust unavailable: selected repository is not writable")
    try:
        configured_paths = [
            Path(value).resolve()
            for name in ("WORKLINK_REPO", "MIMIR_WORKLINK_REPO")
            if (value := os.environ.get(name))
        ]
    except (OSError, RuntimeError) as exc:
        raise ClosureReadError(
            f"repository trust unavailable: configured repository path cannot be resolved: {exc}"
        ) from exc
    if not configured_paths or any(path != repository.root for path in configured_paths):
        raise ClosureReadError(
            "repository trust unavailable: WORKLINK_REPO/MIMIR_WORKLINK_REPO must select the declared root"
        )
    top = _invoke(
        runner, ["git", "-C", str(repository.root), "rev-parse", "--show-toplevel"],
        "git top-level read",
    )
    if top.returncode != 0:
        raise ClosureReadError("repository trust unavailable: git top-level read failed")
    try:
        actual_root = Path(top.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ClosureReadError("repository trust unavailable: invalid git top-level") from exc
    if actual_root != repository.root:
        raise ClosureReadError("repository trust unavailable: git top-level differs from inventory")
    origin = _invoke(
        runner, ["git", "-C", str(repository.root), "remote", "get-url", "origin"],
        "git origin read",
    )
    if origin.returncode != 0 or origin.stdout.strip() != repository.origin:
        raise ClosureReadError("repository trust unavailable: local origin differs from inventory")
    if _origin_slug(repository.origin) != repository.slug:
        raise ClosureReadError("repository trust unavailable: origin identity differs from inventory")
    return repository


def parse_issue_snapshot(payload: object, *, expected_issue_id: int) -> IssueSnapshot:
    if not isinstance(payload, dict):
        raise ClosureReadError("issue snapshot must be an object")
    aliases = [payload[name] for name in ("id", "number") if name in payload]
    if not aliases:
        raise ClosureReadError("issue snapshot is missing id")
    parsed_ids = [_positive_int(value, "issue id") for value in aliases]
    if any(value != expected_issue_id for value in parsed_ids):
        raise ClosureReadError("issue snapshot identity mismatch")
    status = payload.get("status")
    if not isinstance(status, str) or status.casefold() not in {"open", "closed"}:
        raise ClosureReadError("issue snapshot requires explicit open/closed status")
    normalized_status = status.casefold()
    if "closed" in payload:
        closed = payload["closed"]
        if not isinstance(closed, bool) or closed != (normalized_status == "closed"):
            raise ClosureReadError("issue snapshot status and closed flag disagree")
    raw_labels = payload.get("labels")
    if isinstance(raw_labels, dict):
        raw_labels = list(raw_labels.values())
    if not isinstance(raw_labels, list):
        raise ClosureReadError("issue snapshot requires labels")
    labels: set[str] = set()
    for item in raw_labels:
        value = item if isinstance(item, str) else (
            item.get("name", item.get("label")) if isinstance(item, dict) else None
        )
        if not isinstance(value, str) or not value.strip():
            raise ClosureReadError("issue snapshot contains an invalid label")
        labels.add(value.strip())
    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        raise ClosureReadError("issue snapshot requires comments")
    comments: list[str] = []
    for item in raw_comments:
        if isinstance(item, str):
            comments.append(item)
            continue
        if not isinstance(item, dict):
            raise ClosureReadError("issue snapshot contains an invalid comment")
        present = [item[key] for key in ("content", "text", "body") if key in item]
        values = [value for value in present if isinstance(value, str)]
        if not present or len(values) != len(present) or len(set(values)) != 1:
            raise ClosureReadError("issue snapshot contains an ambiguous comment")
        comments.append(values[0])
    if "parent_id" not in payload:
        raise ClosureReadError("issue snapshot requires explicit parent_id")
    parent_raw = payload["parent_id"]
    parent_id = None if parent_raw is None else _positive_int(parent_raw, "parent_id")
    if parent_id == expected_issue_id:
        raise ClosureReadError("issue cannot be its own parent")
    return IssueSnapshot(expected_issue_id, normalized_status, frozenset(labels), tuple(comments), parent_id)


def parse_completion_reference(body: str, *, expected_issue_id: int) -> CompletionDecision:
    if not isinstance(body, str):
        return CompletionDecision(False, "missing_pr_body")
    for character in body:
        category = unicodedata.category(character)
        if category == "Cf" or (category == "Cc" and not character.isspace()):
            return CompletionDecision(False, "completion_body_contains_control_character")
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    first = next((line.strip(" \t") for line in normalized.split("\n") if line.strip()), "")
    match = _COMPLETION.fullmatch(first)
    if match is None or int(match.group(1)) != expected_issue_id:
        return CompletionDecision(False, "missing_exact_completion_declaration")
    if len(re.findall(r"chainlink", normalized, flags=re.IGNORECASE)) != 1:
        return CompletionDecision(False, "ambiguous_chainlink_reference")
    remainder = normalized[len(normalized.split("\n", 1)[0]):]
    if _CLOSING_CLAUSE.search(remainder):
        return CompletionDecision(False, "conflicting_github_closing_clause")
    if _FORBIDDEN.search(normalized):
        return CompletionDecision(False, "completion_body_contains_refusal_language")
    return CompletionDecision(True, "qualified")


def _parse_pr_url(pr_url: str) -> tuple[str, int, str]:
    match = _PR_URL.fullmatch(pr_url)
    if match is None:
        raise ClosureReadError("PR association is not a canonical HTTPS GitHub pull URL")
    slug = f"{match.group('owner')}/{match.group('repo')}".lower()
    number = int(match.group("number"))
    return slug, number, f"https://github.com/{slug}/pull/{number}"


def read_pr_snapshot(pr_url: str, *, gh_bin: str, runner: Runner) -> PrSnapshot:
    slug, number, canonical_url = _parse_pr_url(pr_url)
    payload = _checked_json(
        _invoke(
            runner, [gh_bin, "api", f"repos/{slug}/pulls/{number}"],
            f"PR {canonical_url}",
        ),
        f"PR {canonical_url}",
    )
    if not isinstance(payload, dict):
        raise ClosureReadError("PR snapshot must be an object")
    # True == 1 and 1.0 == 1, so equality alone admits a bool or float
    # where the forge must have returned an integer. This is an identity check;
    # it has to reject a value that merely compares equal to the expected number.
    payload_number = payload.get("number")
    if (
        type(payload_number) is not int
        or payload_number != number
        or payload.get("html_url") != canonical_url
    ):
        raise ClosureReadError("PR snapshot identity mismatch")
    body = payload.get("body")
    state = payload.get("state")
    merged = payload.get("merged")
    base = payload.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    base_slug = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not isinstance(body, str) or state not in {"open", "closed"} or not isinstance(merged, bool):
        raise ClosureReadError("PR snapshot has invalid body/state/merged fields")
    if not isinstance(base_slug, str) or not isinstance(base_ref, str) or not base_ref:
        raise ClosureReadError("PR snapshot has invalid base identity")
    merged_at = payload.get("merged_at")
    merge_sha = payload.get("merge_commit_sha")
    if merged:
        if state != "closed" or not isinstance(merged_at, str) or not isinstance(merge_sha, str):
            raise ClosureReadError("merged PR snapshot is incomplete")
        try:
            parsed_time = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ClosureReadError("merged PR snapshot has invalid merged_at") from exc
        if parsed_time.tzinfo is None or _SHA.fullmatch(merge_sha) is None:
            raise ClosureReadError("merged PR snapshot has invalid merge identity")
    # GitHub may expose a synthetic test-merge SHA before a PR is merged.
    elif merged_at is not None:
        raise ClosureReadError("unmerged PR snapshot contains merge identity")
    return PrSnapshot(
        canonical_url, slug, number, body, state, merged, merged_at,
        merge_sha.lower() if isinstance(merge_sha, str) else None,
        base_slug.lower(), base_ref,
    )


def _comment_associations(comments: Sequence[str]) -> set[str]:
    candidates: set[str] = set()
    patterns = (
        re.compile(r"\bpr_url=(https://github\.com/[^\s]+)", re.IGNORECASE),
        re.compile(r"draft PR opened:\s*(https://github\.com/[^\s]+)", re.IGNORECASE),
    )
    for comment in comments:
        for pattern in patterns:
            for match in pattern.finditer(comment):
                try:
                    candidates.add(_parse_pr_url(match.group(1).rstrip(".,"))[2])
                except ClosureReadError:
                    raise ClosureReadError("historical comment contains an invalid PR association")
    return candidates


def discover_associations(home: Path, issue: IssueSnapshot) -> AssociationDecision:
    evidence_dir = home / "state" / "worklink" / "evidence"
    urls = _comment_associations(issue.comments)
    paths: list[Path] = []
    digests: list[str] = []
    completed: list[Path] = []
    base_refs: list[str] = []
    if evidence_dir.exists():
        if not evidence_dir.is_dir():
            raise ClosureReadError("evidence path is not a directory")
        for path in sorted(evidence_dir.iterdir()):
            if path.name.endswith(".json.closed-unmerged"):
                continue
            if not path.name.startswith(f"{issue.issue_id}-"):
                continue
            name = _EVIDENCE_NAME.fullmatch(path.name)
            if name is None:
                raise ClosureReadError("malformed active evidence filename")
            try:
                raw = path.read_bytes()
                payload = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise ClosureReadError(f"active evidence is unreadable: {path.name}") from exc
            if not isinstance(payload, dict):
                raise ClosureReadError("active evidence must be an object")
            evidence_issue = _positive_int(payload.get("issue"), "evidence issue")
            attempt = _positive_int(payload.get("attempt"), "evidence attempt")
            if evidence_issue != issue.issue_id or attempt != int(name.group("attempt")):
                raise ClosureReadError("active evidence identity mismatch")
            status = payload.get("status")
            if status not in {"completed", "blocked", "failed"}:
                raise ClosureReadError("active evidence has invalid status")
            active_base = payload.get("base_ref")
            if active_base is not None and (not isinstance(active_base, str) or not active_base):
                raise ClosureReadError("active evidence has invalid base_ref")
            if active_base is not None:
                base_refs.append(active_base)
            url = payload.get("pr_url")
            if url is not None:
                if not isinstance(url, str):
                    raise ClosureReadError("active evidence has invalid pr_url")
                urls.add(_parse_pr_url(url)[2])
            digest = hashlib.sha256(raw).hexdigest()
            paths.append(path)
            digests.append(digest)
            if status == "completed" and url is not None:
                completed.append(path)
    if len(urls) > 1:
        return AssociationDecision(
            None, tuple(paths), tuple(digests), tuple(completed), tuple(base_refs),
            "conflicting_pr_associations",
        )
    return AssociationDecision(
        next(iter(urls), None), tuple(paths), tuple(digests), tuple(completed),
        tuple(base_refs), None if urls else "missing_pr_association",
    )


def _list_open_issue_ids(runner: Runner) -> set[int]:
    binary = os.environ.get("CHAINLINK_BIN") or "chainlink"
    payload = _checked_json(
        _invoke(
            runner, [binary, "issue", "list", "--status", "open", "--json"],
            "open issue inventory",
        ),
        "open issue inventory",
    )
    rows = payload if isinstance(payload, list) else payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ClosureReadError("open issue inventory has invalid shape")
    issue_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ClosureReadError("open issue inventory contains an invalid row")
        aliases = [row[name] for name in ("id", "number") if name in row]
        if not aliases:
            raise ClosureReadError("open issue inventory row is missing id")
        parsed = [_positive_int(value, "listed issue id") for value in aliases]
        if len(set(parsed)) != 1:
            raise ClosureReadError("open issue inventory row has conflicting ids")
        issue_ids.add(parsed[0])
    return issue_ids


def _read_issue(issue_id: int, runner: Runner) -> IssueSnapshot:
    binary = os.environ.get("CHAINLINK_BIN") or "chainlink"
    return parse_issue_snapshot(
        _checked_json(
            _invoke(
                runner, [binary, "issue", "show", str(issue_id), "--json"],
                f"issue {issue_id}",
            ),
            f"issue {issue_id}",
        ),
        expected_issue_id=issue_id,
    )


def _lifecycle_reason(issue: IssueSnapshot) -> str | None:
    if not issue.is_open:
        return "issue_not_open"
    if "worklink:review" not in issue.labels:
        return "review_lifecycle_required"
    if "worklink:epic" in issue.labels:
        return "epic_not_leaf"
    competing = sorted(issue.labels & _COMPETING_LABELS)
    if competing:
        return f"competing_lifecycle_label:{','.join(competing)}"
    return None


def _intent_key(issue_id: int, repository: RepositoryConfig, pr: PrSnapshot) -> str:
    identity = json.dumps([1, issue_id, repository.slug, pr.number], separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _audit_text(key: str, issue_id: int, pr: PrSnapshot, base: str) -> str:
    return (
        f"WORKLINK_CLOSED v1 {key}\n"
        f"Chainlink #{issue_id} complete via PR {pr.url}.\n"
        f"Merged at {pr.merged_at}; merge commit {pr.merge_commit_sha}; completion base {base}."
    )


def _notice(
    state_dir: Path, *, issue_id: int | None, repository: str | None,
    pr_url: str | None, reason: str, detail: str,
) -> None:
    record_merge_reconciliation_notice(
        state_dir, issue_id=issue_id, repository=repository, pr_url=pr_url,
        reason=reason, detail=detail,
    )


def _validate_qualification(
    *, issue: IssueSnapshot, pr: PrSnapshot, repository: RepositoryConfig,
    association: AssociationDecision,
) -> str | None:
    if reason := _lifecycle_reason(issue):
        return reason
    if association.reason:
        return association.reason
    if association.pr_url != pr.url:
        return "association_changed"
    if pr.slug != repository.slug or pr.base_slug != repository.slug:
        return "repository_identity_mismatch"
    if pr.base_ref != repository.base_branch:
        return "completion_base_mismatch"
    if any(base_ref != repository.base_branch for base_ref in association.active_base_refs):
        return "completion_base_mismatch"
    if not pr.merged:
        return "pr_open" if pr.state == "open" else "pr_closed_unmerged"
    completion = parse_completion_reference(pr.body, expected_issue_id=issue.issue_id)
    return None if completion.qualifies else completion.reason


def _archive_closed_unmerged(association: AssociationDecision) -> None:
    for path in association.completed_evidence_paths:
        index = association.evidence_paths.index(path)
        expected = association.evidence_digests[index]
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ClosureReadError(f"evidence changed before archive: {path.name}") from exc
        if current != expected:
            raise ClosureReadError(f"evidence changed before archive: {path.name}")
        target = path.with_suffix(".json.closed-unmerged")
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ClosureReadError(f"closed-unmerged archive conflicts: {target.name}")
            path.unlink()
        else:
            os.replace(path, target)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _pending_intents(state_dir: Path) -> dict[str, dict[str, object]]:
    with merge_reconciliation_transaction(state_dir) as reconciliations:
        return {
            key: dict(value)
            for key, value in reconciliations["intents"].items()
            if not value.get("result_finalized")
        }


def _persist_intent(state_dir: Path, key: str, identity: dict[str, object]) -> dict[str, object]:
    with merge_reconciliation_transaction(state_dir) as reconciliations:
        intents = reconciliations["intents"]
        existing = intents.get(key)
        if existing is None:
            intents[key] = {**identity, "stage": "discovered", "result_finalized": False}
        elif any(existing.get(field) != value for field, value in identity.items()):
            raise ClosureReadError("persisted merge identity changed")
        return dict(intents[key])


def _update_intent(state_dir: Path, key: str, **changes: object) -> dict[str, object]:
    with merge_reconciliation_transaction(state_dir) as reconciliations:
        entry = reconciliations["intents"].get(key)
        if not isinstance(entry, dict):
            raise ClosureReadError("merge intent disappeared")
        entry.update(changes)
        return dict(entry)


def _run_tracker(runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    binary = os.environ.get("CHAINLINK_BIN") or "chainlink"
    try:
        return runner([binary, "issue", *args])
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([binary, "issue", *args], 1, "", str(exc))


def _revalidate(
    home: Path, issue_id: int, pr_url: str, *, repository: RepositoryConfig,
    gh_bin: str, chainlink_runner: Runner, gh_runner: Runner,
) -> tuple[IssueSnapshot, PrSnapshot, AssociationDecision]:
    issue = _read_issue(issue_id, chainlink_runner)
    association = discover_associations(home, issue)
    pr = read_pr_snapshot(pr_url, gh_bin=gh_bin, runner=gh_runner)
    reason = _validate_qualification(
        issue=issue, pr=pr, repository=repository, association=association,
    )
    if reason:
        raise ClosureReadError(reason)
    return issue, pr, association


def _process_intent(
    home: Path, state_dir: Path, key: str, *, repository: RepositoryConfig,
    gh_bin: str, chainlink_runner: Runner, gh_runner: Runner,
) -> ClosureOutcome | None:
    with merge_reconciliation_transaction(state_dir) as reconciliations:
        entry = dict(reconciliations["intents"][key])
    issue_id = int(entry["issue_id"])
    pr_url = str(entry["pr_url"])
    audit = str(entry["audit_text"])
    stage = str(entry["stage"])
    if entry.get("result_finalized"):
        return None
    try:
        captured_incident = current_failure_identity(state_dir, issue_id)
    except ValueError as exc:
        _notice(
            state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
            reason="incident_identity_unavailable", detail=str(exc),
        )
        return None
    try:
        issue, pr, association = _revalidate(
            home, issue_id, pr_url, repository=repository, gh_bin=gh_bin,
            chainlink_runner=chainlink_runner, gh_runner=gh_runner,
        )
    except ClosureReadError as exc:
        # A verified close is allowed to proceed to cleanup despite the normal
        # open-review lifecycle no longer applying.
        recovering_verified_close = stage in {"close_started", "closed_verified", "cleanup_pending"} or (
            stage == "uncertain" and entry.get("uncertainty") == "close_outcome_uncertain"
        )
        if not recovering_verified_close:
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="intent_revalidation_failed", detail=str(exc),
            )
            return None
        try:
            issue = _read_issue(issue_id, chainlink_runner)
            pr = read_pr_snapshot(pr_url, gh_bin=gh_bin, runner=gh_runner)
            association = discover_associations(home, issue)
        except ClosureReadError as recovery_exc:
            # The recovery path re-reads the same sources that just failed. An
            # unguarded second failure escapes _process_intent and aborts the
            # whole sweep, leaving no durable record of why.
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="close_recovery_read_failed", detail=str(recovery_exc),
            )
            return None
    persisted_identity = {
        "repository": repository.slug,
        "pr_url": pr.url,
        "base_ref": pr.base_ref,
        "merge_commit_sha": pr.merge_commit_sha,
        "merged_at": pr.merged_at,
        "source_digests": list(association.evidence_digests),
    }
    if any(entry.get(field) != value for field, value in persisted_identity.items()):
        _notice(
            state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
            reason="merge_identity_changed", detail="persisted merge or source identity changed",
        )
        return None
    if stage == "audit_started":
        if audit not in issue.comments:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="audit_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="audit_outcome_uncertain", detail="audit submission began but exact marker is absent",
            )
            return None
        entry = _update_intent(state_dir, key, stage="audit_confirmed")
        stage = str(entry["stage"])
    if stage == "discovered":
        _update_intent(state_dir, key, stage="audit_started")
        _run_tracker(chainlink_runner, "comment", str(issue_id), audit)
        try:
            issue = _read_issue(issue_id, chainlink_runner)
        except ClosureReadError:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="audit_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="audit_outcome_uncertain", detail="audit submission outcome could not be read",
            )
            return None
        if audit not in issue.comments:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="audit_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="audit_outcome_uncertain", detail="exact audit marker was not observed after submission",
            )
            return None
        _update_intent(state_dir, key, stage="audit_confirmed")
        stage = "audit_confirmed"
    if stage == "uncertain":
        # Positive audit evidence may resolve only audit uncertainty. A close
        # uncertainty is never replayed while open because it may be an operator reopen.
        if entry.get("uncertainty") == "audit_outcome_uncertain" and audit in issue.comments:
            _update_intent(state_dir, key, stage="audit_confirmed", uncertainty=None)
            stage = "audit_confirmed"
        elif entry.get("uncertainty") == "close_outcome_uncertain" and not issue.is_open and audit in issue.comments:
            _update_intent(state_dir, key, stage="closed_verified", uncertainty=None)
            stage = "closed_verified"
        else:
            return None
    if stage == "audit_confirmed":
        try:
            issue, pr, _ = _revalidate(
                home, issue_id, pr_url, repository=repository, gh_bin=gh_bin,
                chainlink_runner=chainlink_runner, gh_runner=gh_runner,
            )
        except ClosureReadError as exc:
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="pre_close_revalidation_failed", detail=str(exc),
            )
            return None
        if audit not in issue.comments:
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="audit_marker_missing", detail="confirmed audit marker is no longer present",
            )
            return None
        _update_intent(state_dir, key, stage="close_started")
        _run_tracker(chainlink_runner, "close", str(issue_id))
        try:
            issue = _read_issue(issue_id, chainlink_runner)
        except ClosureReadError:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="close_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="close_outcome_uncertain", detail="close submission outcome could not be read",
            )
            return None
        if issue.is_open or audit not in issue.comments:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="close_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="close_outcome_uncertain",
                detail="closed state with the exact audit marker was not observed; no replay",
            )
            return None
        _update_intent(state_dir, key, stage="closed_verified")
        stage = "closed_verified"
    elif stage == "close_started":
        if issue.is_open or audit not in issue.comments:
            _update_intent(state_dir, key, stage="uncertain", uncertainty="close_outcome_uncertain")
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="close_outcome_uncertain", detail="close began without positive closed audit evidence",
            )
            return None
        _update_intent(state_dir, key, stage="closed_verified")
        stage = "closed_verified"
    if stage in {"closed_verified", "cleanup_pending"}:
        try:
            issue = _read_issue(issue_id, chainlink_runner)
        except ClosureReadError as cleanup_exc:
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="cleanup_read_failed", detail=str(cleanup_exc),
            )
            return None
        if issue.is_open:
            _notice(
                state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                reason="cleanup_refused_reopened", detail="verified-closed issue was reopened; review label retained",
            )
            return None
        if "worklink:review" in issue.labels:
            _update_intent(state_dir, key, stage="cleanup_pending")
            result = _run_tracker(chainlink_runner, "unlabel", str(issue_id), "worklink:review")
            if result.returncode != 0:
                _notice(
                    state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                    reason="cleanup_failed", detail="review label removal failed",
                )
                return None
            try:
                issue = _read_issue(issue_id, chainlink_runner)
            except ClosureReadError as verify_exc:
                _notice(
                    state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                    reason="cleanup_read_failed", detail=str(verify_exc),
                )
                return None
            if issue.is_open or "worklink:review" in issue.labels:
                _notice(
                    state_dir, issue_id=issue_id, repository=repository.slug, pr_url=pr_url,
                    reason="cleanup_verification_failed", detail="review label absence was not verified",
                )
                return None
        if captured_incident is not None:
            resolve_failure_if_current(
                state_dir, issue_id, captured_incident[0], captured_incident[1],
            )
        _update_intent(state_dir, key, stage="finalized", result_finalized=True)
        resolve_merge_reconciliation_notices(state_dir, issue_id=issue_id, pr_url=pr_url)
        return ClosureOutcome(issue_id, pr.url, str(pr.merged_at), str(pr.merge_commit_sha))
    return None




def reconcile_merged_leaves(
    home: Path,
    *,
    gh_bin: str = "gh",
    dry_run: bool = False,
    chainlink_runner: Runner,
    gh_runner: Runner | None = None,
    git_runner: Runner | None = None,
) -> list[ClosureOutcome]:
    """Reconcile qualifying merged leaves with audit-first durable intent."""
    gh_runner = gh_runner or _default_runner
    git_runner = git_runner or _default_runner
    state_dir = dispatch_failure_state_dir(home)

    def sweep() -> list[ClosureOutcome]:
        try:
            repository = resolve_completion_repository(home, runner=git_runner)
        except ClosureReadError as exc:
            if not dry_run:
                _notice(
                    state_dir, issue_id=None, repository=None, pr_url=None,
                    reason="repository_trust_failed", detail=str(exc),
                )
            return []
        pending = {} if dry_run else _pending_intents(state_dir)
        pending_issue_ids = {
            int(entry["issue_id"]) for entry in pending.values()
            if isinstance(entry.get("issue_id"), int)
        }
        try:
            observed_incidents = [] if dry_run else active_failure_identities(
                state_dir,
                limit=MAX_INCIDENT_RETIREMENTS_PER_SWEEP,
                exclude_issue_ids=pending_issue_ids,
            )
            open_ids = _list_open_issue_ids(chainlink_runner)
        except (ClosureReadError, ValueError) as exc:
            if not dry_run:
                _notice(
                    state_dir, issue_id=None, repository=repository.slug, pr_url=None,
                    reason="tracker_inventory_failed", detail=str(exc),
                )
            return []
        if not dry_run:
            for issue_id, signature, occurrence_id in observed_incidents:
                if issue_id not in open_ids:
                    resolve_failure_if_current(
                        state_dir, issue_id, signature, occurrence_id,
                    )
        if not dry_run:
            resolve_merge_reconciliation_notices(
                state_dir, issue_id=None, pr_url=None,
            )
        issue_ids = open_ids | {
            int(entry["issue_id"]) for entry in pending.values()
            if isinstance(entry.get("issue_id"), int)
        }
        outcomes: list[ClosureOutcome] = []
        pending_by_issue = {
            int(entry["issue_id"]): key for key, entry in pending.items()
            if isinstance(entry.get("issue_id"), int)
        }
        for issue_id in sorted(issue_ids):
            association: AssociationDecision | None = None
            if intent_key := pending_by_issue.get(issue_id):
                outcome = _process_intent(
                    home, state_dir, intent_key, repository=repository, gh_bin=gh_bin,
                    chainlink_runner=chainlink_runner, gh_runner=gh_runner,
                )
                if outcome:
                    outcomes.append(outcome)
                continue
            try:
                issue = _read_issue(issue_id, chainlink_runner)
                if "worklink:review" not in issue.labels:
                    association = discover_associations(home, issue)
                    if association.pr_url is None and association.reason == "missing_pr_association":
                        if not dry_run:
                            resolve_merge_reconciliation_notices(
                                state_dir, issue_id=issue_id, pr_url=None,
                            )
                        continue
                    raise ClosureReadError("review_lifecycle_required")
                if lifecycle_reason := _lifecycle_reason(issue):
                    raise ClosureReadError(lifecycle_reason)
                association = discover_associations(home, issue)
                if association.reason:
                    raise ClosureReadError(association.reason)
                assert association.pr_url is not None
                pr = read_pr_snapshot(association.pr_url, gh_bin=gh_bin, runner=gh_runner)
                reason = _validate_qualification(
                    issue=issue, pr=pr, repository=repository, association=association,
                )
                if reason == "pr_open":
                    if not dry_run:
                        resolve_merge_reconciliation_notices(
                            state_dir, issue_id=issue_id, pr_url=pr.url,
                        )
                    continue
                if reason == "pr_closed_unmerged":
                    if not dry_run:
                        resolve_merge_reconciliation_notices(
                            state_dir, issue_id=issue_id, pr_url=pr.url,
                        )
                        _archive_closed_unmerged(association)
                    continue
                if reason:
                    raise ClosureReadError(reason)
                assert pr.merged_at is not None and pr.merge_commit_sha is not None
                key = _intent_key(issue_id, repository, pr)
                audit = _audit_text(key, issue_id, pr, repository.base_branch)
                if dry_run:
                    outcomes.append(
                        ClosureOutcome(issue_id, pr.url, pr.merged_at, pr.merge_commit_sha)
                    )
                    continue
                resolve_merge_reconciliation_notices(
                    state_dir, issue_id=issue_id, pr_url=pr.url,
                )
                identity = {
                    "intent_key": key,
                    "issue_id": issue_id,
                    "repository": repository.slug,
                    "pr_number": pr.number,
                    "pr_url": pr.url,
                    "base_ref": pr.base_ref,
                    "merge_commit_sha": pr.merge_commit_sha,
                    "merged_at": pr.merged_at,
                    "audit_text": audit,
                    "source_digests": list(association.evidence_digests),
                }
                _persist_intent(state_dir, key, identity)
                outcome = _process_intent(
                    home, state_dir, key, repository=repository, gh_bin=gh_bin,
                    chainlink_runner=chainlink_runner, gh_runner=gh_runner,
                )
                if outcome:
                    outcomes.append(outcome)
            except ClosureReadError as exc:
                if not dry_run:
                    detail = str(exc)
                    reason = (
                        detail if len(detail) <= 100 and re.fullmatch(r"[a-z0-9_:,.-]+", detail)
                        else "reconciliation_read_failed"
                    )
                    _notice(
                        state_dir, issue_id=issue_id, repository=repository.slug,
                        pr_url=association.pr_url if association is not None else None,
                        reason=reason, detail=detail,
                    )
        return outcomes

    if dry_run:
        return sweep()
    lock_path = home / "state" / "worklink" / "merge-closure.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return sweep()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
