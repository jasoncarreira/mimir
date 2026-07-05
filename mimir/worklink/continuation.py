"""Durable Worklink continuation sidecars for tool-budget exhaustion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .._atomic import atomic_write_json
from ..access_control import authorize_action
from ..models import AgentEvent, TurnContext, TurnRecord
from .run_state import load_run_state, run_state_path

CONTINUATION_KIND = "worklink_tool_budget_continuation"
CONTINUATION_VERSION = 1
CONTINUATION_PREFIX = "WORKLINK_CONTINUATION "

WORKLINK_HINT_EXTRA_KEYS = frozenset({
    "issue_id",
    "pr_url",
    "worktree",
    "poller_name",
    "run_id",
})
_MAX_RECENT_TURNS = 10
_MAX_CHANGED_PATHS = 25
_MAX_EXTERNAL_COMMANDS = 5
_MAX_EXTERNAL_COMMAND_LEN = 160
_MAX_LABEL_ACTIONS = 5
_MAX_LABEL_ACTION_LEN = 80

_RUN_ID_RE = re.compile(r"\bchainlink-\d+\b", re.IGNORECASE)
_PR_URL_RE = re.compile(r"https?://github\.com/[^\s)]+/pull/\d+", re.IGNORECASE)
_ISSUE_PATTERNS = (
    re.compile(r"\bchainlink\s*#(\d+)\b", re.IGNORECASE),
    re.compile(r"\bissue\s*#(\d+)\b", re.IGNORECASE),
    re.compile(r"\bissue[_\s-]?id\s*[:=]\s*['\"]?(\d+)\b", re.IGNORECASE),
)
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


Runner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def strip_worklink_hint_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop client-controlled Worklink continuation hint keys from ``extra``.

    Strips recursively so nested objects cannot smuggle authoritative-looking
    issue / PR / worktree hints through the generic ``POST /event`` ingress.
    """

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in WORKLINK_HINT_EXTRA_KEYS
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    if not extra:
        return {}
    return scrub(extra)


@dataclass(frozen=True)
class FactoryRunMetadata:
    run_id: str
    issue_id: int | None
    branch: str | None
    worktree: Path | None
    run_dir: Path
    pr_url: str | None


@dataclass(frozen=True)
class EvidenceMetadata:
    issue_id: int
    path: Path
    branch: str | None
    worktree: Path | None
    pr_url: str | None
    test_command: str | None


@dataclass(frozen=True)
class WorklinkContinuationResult:
    sidecar_path: Path
    idempotency_key: str
    payload: dict[str, Any]


def continuations_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "continuations"


def continuation_sidecar_path(home: Path, idempotency_key: str) -> Path:
    return continuations_dir(home) / f"{idempotency_key}.json"


def maybe_create_worklink_budget_continuation(
    *,
    home: Path,
    event: AgentEvent,
    ctx: TurnContext,
    record: TurnRecord,
    repo: Path | None = None,
    current_worktree: Path | None = None,
    run_id: str | None = None,
    current_labels: Sequence[str] | None = None,
    validation_command: str | None = None,
    validation_state: str | None = None,
    enable_external_comments: bool = True,
    chainlink_bin: str = "chainlink",
    gh_bin: str = "gh",
    runner: Runner | None = None,
) -> WorklinkContinuationResult | None:
    """Create/update a durable continuation sidecar for an exhausted turn.

    Returns ``None`` when the turn was not budget-exhausted or no Worklink /
    Chainlink context could be inferred.
    """

    if not getattr(ctx, "tool_call_budget_exhausted", False):
        return None

    runner = runner or _default_runner
    now = _utc_now_iso()
    configured_repo = _resolve_existing_dir(repo)
    validated_worktree = _resolve_existing_dir(current_worktree) or configured_repo
    repo_for_metadata = configured_repo or validated_worktree
    repo_slug = _repo_slug(configured_repo or validated_worktree, runner=runner)

    current_branch = _git_current_branch(validated_worktree, runner=runner)
    partial_work_state = _git_partial_work_state(validated_worktree, runner=runner)

    hint_strings = _collect_hint_strings(event=event, record=record, current_branch=current_branch)
    candidate_run_ids = _unique(
        ([run_id] if isinstance(run_id, str) and run_id.strip() else [])
        + [match.group(0) for text in hint_strings for match in _RUN_ID_RE.finditer(text)]
    )
    candidate_issue_ids = _unique_ints(
        [
            int(match.group(1))
            for text in hint_strings
            for pattern in _ISSUE_PATTERNS
            for match in pattern.finditer(text)
        ]
    )
    candidate_pr_urls = _unique(
        [match.group(0) for text in hint_strings for match in _PR_URL_RE.finditer(text)]
    )

    factory_runs = [
        run_meta
        for candidate in candidate_run_ids
        if (run_meta := _load_factory_run(repo_for_metadata, candidate)) is not None
    ]
    primary_factory = factory_runs[0] if factory_runs else None

    trusted_labels = _normalize_labels(current_labels)
    worklink_related = _has_worklink_context(
        hint_strings=hint_strings,
        factory_runs=factory_runs,
        labels=trusted_labels,
        branch=current_branch,
        issue_candidates=candidate_issue_ids,
        pr_candidates=candidate_pr_urls,
    )
    if not worklink_related:
        return None

    validated_issue_id, validated_repo, _factory_source = _validate_issue_from_factory(
        primary_factory, current_repo=repo_slug,
    )

    run_state = None
    run_state_file: Path | None = None
    run_state_test_command: str | None = None
    if validated_issue_id is not None:
        run_state = load_run_state(home, validated_issue_id)
        if run_state is not None:
            run_state_repo = _normalize_repo(run_state.repo or run_state.repo_url)
            if validated_repo is None:
                validated_repo = repo_slug or run_state_repo
            if validated_repo is not None and run_state_repo not in (None, validated_repo):
                run_state = None
            else:
                run_state_file = run_state_path(home, validated_issue_id)
                run_state_test_command = run_state.test_command

    if validated_issue_id is None:
        validated_issue_id, validated_repo, run_state, run_state_file, run_state_test_command = (
            _validate_issue_from_run_state(
                home,
                candidate_issue_ids,
                current_repo=repo_slug,
            )
        )

    evidence_records = (
        _load_evidence_records(home, validated_issue_id)
        if validated_issue_id is not None
        else []
    )
    validated_pr_url, evidence_test_command = _validate_pr_from_evidence(
        evidence_records,
        current_repo=validated_repo or repo_slug,
        current_worktree=validated_worktree,
        candidate_pr_urls=candidate_pr_urls,
    )
    if validated_issue_id is None:
        issue_from_evidence, repo_from_evidence, pr_from_evidence, evidence_test_command = (
            _validate_issue_from_evidence(
                home,
                candidate_issue_ids,
                current_repo=repo_slug,
                current_worktree=validated_worktree,
                candidate_pr_urls=candidate_pr_urls,
            )
        )
        if issue_from_evidence is not None:
            validated_issue_id = issue_from_evidence
            validated_repo = repo_from_evidence
            validated_pr_url = pr_from_evidence

    if validated_pr_url is None and primary_factory is not None:
        validated_pr_url = _validate_factory_pr(primary_factory, repo=validated_repo or repo_slug)

    if validated_repo is None:
        validated_repo = repo_slug

    effective_validation_command = (
        validation_command
        or run_state_test_command
        or evidence_test_command
    )
    effective_validation_state = _normalize_validation_state(
        validation_state,
        has_command=bool(effective_validation_command),
    )

    association = {
        "issue_id": validated_issue_id,
        "pr_url": validated_pr_url,
        "repo": validated_repo,
        "worktree": str(validated_worktree) if validated_worktree is not None else None,
        "branch": current_branch,
        "run_state_path": str(run_state_file) if run_state_file is not None else None,
        "factory_run_id": primary_factory.run_id if primary_factory is not None else None,
        "factory_run_dir": str(primary_factory.run_dir) if primary_factory is not None else None,
        "current_labels": trusted_labels or None,
    }

    next_commands = _recommended_commands(
        worktree=validated_worktree,
        branch=current_branch,
        issue_id=validated_issue_id,
        validation_command=effective_validation_command,
        run_state_present=run_state_file is not None,
    )
    label_actions = _label_actions(
        trusted_labels,
        run_state_present=run_state_file is not None,
    )

    idempotency_key, dedupe_scope, dedupe_material = _idempotency(
        issue_id=validated_issue_id,
        repo=validated_repo,
        pr_url=validated_pr_url,
        worktree=validated_worktree,
        branch=current_branch,
        source_id=event.source_id,
        turn_id=ctx.turn_id or record.turn_id,
    )

    path = continuation_sidecar_path(home, idempotency_key)
    existing = _load_json_dict(path)
    created_at = _truncate_str(existing.get("created_at"), 64) if existing else None
    existing_turns = existing.get("turns") if isinstance(existing, dict) else None
    turns = _merge_turns(
        existing_turns,
        turn_id=ctx.turn_id or record.turn_id,
        source_id=event.source_id,
    )
    occurrences = _occurrences(existing)
    existing_comment = existing.get("external_comment") if isinstance(existing, dict) else None

    payload: dict[str, Any] = {
        "kind": CONTINUATION_KIND,
        "version": CONTINUATION_VERSION,
        "priority": "high",
        "idempotency_key": idempotency_key,
        "dedupe_scope": dedupe_scope,
        "dedupe_material": dedupe_material,
        "created_at": created_at or now,
        "updated_at": now,
        "occurrences": occurrences,
        "turns": turns,
        "budget": {
            "count": int(getattr(ctx, "tool_call_count", 0) or 0),
            "budget": int(getattr(ctx, "tool_call_budget", 0) or 0),
            "denied_count": int(getattr(ctx, "tool_call_budget_denied_count", 0) or 0),
            "denied_tools": [
                _truncate_str(str(tool), 120)
                for tool in getattr(ctx, "tool_call_budget_denied_tools", ()) or ()
                if str(tool).strip()
            ][:10],
            "first_denied_at_count": getattr(ctx, "tool_call_budget_first_denied_at_count", None),
        },
        "source_event": {
            "trigger": _truncate_str(event.trigger or ctx.trigger, 80),
            "channel": _truncate_str(event.channel_id or ctx.channel_id, 160),
            "source": _truncate_str(event.source, 80),
            "source_id": _truncate_str(event.source_id, 160),
            "poller_name": _truncate_str(
                _mapping_str(event.extra, "poller_name") if isinstance(event.extra, Mapping) else None,
                80,
            ),
            "factory_run_id_hint": _truncate_str(candidate_run_ids[0], 120) if candidate_run_ids else None,
        },
        "association": association,
        "partial_work_state": partial_work_state,
        "validation": {
            "state": effective_validation_state,
            "commands": [effective_validation_command] if effective_validation_command else [],
        },
        "next": {
            "commands": next_commands,
            "labels_or_status_changes_needed": label_actions,
        },
        "external_comment": _initial_external_comment(existing_comment),
        "label_status_mutated": False,
    }

    atomic_write_json(path, payload)

    comment_state = _handle_external_comment(
        payload=payload,
        existing_comment=existing_comment,
        home=home,
        ctx=ctx,
        enable_external_comments=enable_external_comments,
        chainlink_bin=chainlink_bin,
        gh_bin=gh_bin,
        runner=runner,
        cwd=validated_worktree or configured_repo,
    )
    payload["external_comment"] = comment_state
    payload["updated_at"] = _utc_now_iso()
    atomic_write_json(path, payload)

    _emit_continuation_event(
        idempotency_key=idempotency_key,
        sidecar=str(path),
        issue_id=validated_issue_id,
        pr_url=validated_pr_url,
        repo=validated_repo,
        branch=current_branch,
        worktree=str(validated_worktree) if validated_worktree is not None else None,
        external_comment_posted=bool(comment_state.get("posted")),
        occurrences=occurrences,
        source_id=event.source_id,
        factory_run_id=primary_factory.run_id if primary_factory is not None else None,
    )
    return WorklinkContinuationResult(sidecar_path=path, idempotency_key=idempotency_key, payload=payload)


def _default_runner(
    args: Sequence[str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def _resolve_existing_dir(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.exists() and resolved.is_dir() else None


def _collect_hint_strings(
    *,
    event: AgentEvent,
    record: TurnRecord,
    current_branch: str | None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        out.append(text[:4000])

    def walk(value: Any, *, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, (int, float)):
            add(str(value))
            return
        if isinstance(value, Path):
            add(str(value))
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str):
                    add(key)
                walk(item, depth=depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item, depth=depth + 1)

    for raw in (
        event.content,
        event.channel_id,
        event.source,
        event.source_id,
        record.input,
        record.output,
        current_branch,
    ):
        add(raw)
    walk(event.extra)
    walk(record.events)
    return out


def _has_worklink_context(
    *,
    hint_strings: Sequence[str],
    factory_runs: Sequence[FactoryRunMetadata],
    labels: Sequence[str],
    branch: str | None,
    issue_candidates: Sequence[int],
    pr_candidates: Sequence[str],
) -> bool:
    if factory_runs or labels or issue_candidates or pr_candidates:
        return True
    if branch and ("worklink" in branch.lower() or "chainlink-" in branch.lower()):
        return True
    return any(
        "worklink" in text.lower() or "chainlink" in text.lower()
        for text in hint_strings
    )


def _load_factory_run(repo_root: Path | None, run_id: str) -> FactoryRunMetadata | None:
    if repo_root is None:
        return None
    run_id = run_id.strip()
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        return None
    root = (repo_root / ".opencode" / "factory").resolve()
    candidate_dir = (root / run_id).resolve(strict=False)
    if not _is_within(candidate_dir, root):
        return None
    run_json = candidate_dir / "run.json"
    data = _load_json_dict(run_json)
    if not data:
        return None
    return FactoryRunMetadata(
        run_id=run_id,
        issue_id=_parse_issue_from_text(_truncate_str(data.get("external_ref"), 200)),
        branch=_truncate_str(data.get("branch"), 200),
        worktree=_resolve_existing_dir(Path(str(data.get("worktree"))))
        if data.get("worktree")
        else None,
        run_dir=candidate_dir,
        pr_url=_normalize_pr_url(_truncate_str(data.get("pr_url"), 400)),
    )


def _validate_issue_from_factory(
    factory_run: FactoryRunMetadata | None,
    *,
    current_repo: str | None,
) -> tuple[int | None, str | None, FactoryRunMetadata | None]:
    if factory_run is None or factory_run.issue_id is None:
        return None, current_repo, None
    if current_repo is None:
        return None, None, None
    return factory_run.issue_id, current_repo, factory_run


def _validate_issue_from_run_state(
    home: Path,
    issue_ids: Sequence[int],
    *,
    current_repo: str | None,
) -> tuple[int | None, str | None, Any | None, Path | None, str | None]:
    for issue_id in issue_ids:
        state = load_run_state(home, issue_id)
        if state is None:
            continue
        run_repo = _normalize_repo(state.repo or state.repo_url)
        validated_repo = current_repo or run_repo
        if validated_repo is None:
            continue
        if run_repo not in (None, validated_repo):
            continue
        return issue_id, validated_repo, state, run_state_path(home, issue_id), state.test_command
    return None, current_repo, None, None, None


def _load_evidence_records(home: Path, issue_id: int | None) -> list[EvidenceMetadata]:
    if issue_id is None:
        return []
    root = home / "state" / "worklink" / "evidence"
    if not root.exists():
        return []
    out: list[EvidenceMetadata] = []
    for path in sorted(root.glob(f"{issue_id}-*.json")):
        data = _load_json_dict(path)
        if not data:
            continue
        tests = data.get("tests") if isinstance(data.get("tests"), Mapping) else {}
        worktree_raw = data.get("worktree")
        out.append(
            EvidenceMetadata(
                issue_id=issue_id,
                path=path,
                branch=_truncate_str(data.get("branch"), 200),
                worktree=_resolve_existing_dir(Path(str(worktree_raw))) if worktree_raw else None,
                pr_url=_normalize_pr_url(_truncate_str(data.get("pr_url"), 400)),
                test_command=_truncate_str(tests.get("cmd"), 400) if isinstance(tests, Mapping) else None,
            )
        )
    return out


def _validate_issue_from_evidence(
    home: Path,
    issue_ids: Sequence[int],
    *,
    current_repo: str | None,
    current_worktree: Path | None,
    candidate_pr_urls: Sequence[str],
) -> tuple[int | None, str | None, str | None, str | None]:
    candidate_prs = {
        normalized
        for url in candidate_pr_urls
        if (normalized := _normalize_pr_url(url)) is not None
    }
    for issue_id in issue_ids:
        records = _load_evidence_records(home, issue_id)
        if not records:
            continue
        for record in records:
            pr_repo = _repo_from_pr_url(record.pr_url)
            if current_worktree is not None and record.worktree == current_worktree:
                return issue_id, current_repo or pr_repo, record.pr_url, record.test_command
            if current_repo is not None and pr_repo == current_repo:
                if not candidate_prs or record.pr_url in candidate_prs or record.pr_url is None:
                    return issue_id, current_repo, record.pr_url, record.test_command
    return None, current_repo, None, None


def _validate_pr_from_evidence(
    records: Sequence[EvidenceMetadata],
    *,
    current_repo: str | None,
    current_worktree: Path | None,
    candidate_pr_urls: Sequence[str],
) -> tuple[str | None, str | None]:
    candidate_prs = {
        normalized
        for url in candidate_pr_urls
        if (normalized := _normalize_pr_url(url)) is not None
    }
    for record in records:
        if record.pr_url is None:
            continue
        pr_repo = _repo_from_pr_url(record.pr_url)
        if current_repo is not None and pr_repo != current_repo:
            continue
        if current_worktree is not None and record.worktree not in (None, current_worktree):
            continue
        if candidate_prs and record.pr_url not in candidate_prs:
            continue
        return record.pr_url, record.test_command
    return None, None


def _validate_factory_pr(factory_run: FactoryRunMetadata, *, repo: str | None) -> str | None:
    if factory_run.pr_url is None or repo is None:
        return None
    return factory_run.pr_url if _repo_from_pr_url(factory_run.pr_url) == repo else None


def _repo_slug(repo: Path | None, *, runner: Runner) -> str | None:
    if repo is None:
        return None
    result = runner(["git", "-C", str(repo), "config", "--get", "remote.origin.url"], None)
    if result.returncode != 0:
        return None
    return _normalize_repo(result.stdout)


def _normalize_repo(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("git@github.com:"):
        text = text.removeprefix("git@github.com:")
    elif text.startswith("https://github.com/"):
        text = text.rsplit("github.com/", 1)[1]
    elif text.startswith("http://github.com/"):
        text = text.rsplit("github.com/", 1)[1]
    text = text.removesuffix(".git").strip("/")
    return text.lower() if _REPO_RE.fullmatch(text) else None


def _normalize_pr_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    match = re.match(
        r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    owner, repo, number = match.groups()
    return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}"


def _repo_from_pr_url(pr_url: str | None) -> str | None:
    normalized = _normalize_pr_url(pr_url)
    if normalized is None:
        return None
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/\d+", normalized)
    return match.group(1) if match is not None else None


def _git_current_branch(worktree: Path | None, *, runner: Runner) -> str | None:
    if worktree is None:
        return None
    result = runner(["git", "-C", str(worktree), "branch", "--show-current"], None)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return _truncate_str(value, 200) if value else None


def _git_partial_work_state(worktree: Path | None, *, runner: Runner) -> dict[str, Any]:
    if worktree is None:
        return {
            "state": "unknown",
            "dirty": None,
            "changed_paths": [],
            "changed_path_count": None,
        }
    result = runner(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        None,
    )
    if result.returncode != 0:
        return {
            "state": "unknown",
            "dirty": None,
            "changed_paths": [],
            "changed_path_count": None,
        }
    raw_lines = [line.rstrip("\n") for line in result.stdout.splitlines() if line.strip()]
    changed_paths = [_porcelain_path(line) for line in raw_lines]
    trimmed = [path for path in changed_paths if path][:_MAX_CHANGED_PATHS]
    dirty = bool(raw_lines)
    return {
        "state": "dirty" if dirty else "clean",
        "dirty": dirty,
        "changed_paths": trimmed,
        "changed_path_count": len(changed_paths),
    }


def _porcelain_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return _truncate_str(path.strip(), 240) or "(unknown)"


def _recommended_commands(
    *,
    worktree: Path | None,
    branch: str | None,
    issue_id: int | None,
    validation_command: str | None,
    run_state_present: bool,
) -> list[str]:
    commands: list[str] = []
    if worktree is not None:
        commands.append(f"git -C {worktree} status --short")
        if branch:
            commands.append(f"git -C {worktree} branch --show-current")
    if run_state_present and issue_id is not None:
        command = f"mimir worklink run {issue_id} --reattach --home <MIMIR_HOME>"
        if worktree is not None:
            command += f" --repo {worktree}"
        commands.append(command)
    if validation_command:
        commands.append(validation_command)
    return _limit_unique(commands, _MAX_EXTERNAL_COMMANDS)


def _label_actions(labels: Sequence[str], *, run_state_present: bool) -> list[str]:
    label_set = set(labels)
    actions: list[str] = []
    if "worklink:in-progress" in label_set:
        if run_state_present:
            actions.append("reattach existing worklink run before changing labels")
        else:
            actions.append("preserve worklink:in-progress; use manual recovery or TTL reaper next")
    if "worklink:ready" in label_set:
        actions.append("preserve worklink:ready until continuation work resumes")
    if "worklink:review" in label_set:
        actions.append("preserve worklink:review; confirm PR and validation state before relabeling")
    if "worklink:rework" in label_set:
        actions.append("preserve worklink:rework; confirm follow-up edits and validation before relabeling")
    if not actions and run_state_present:
        actions.append("reattach existing worklink run before any label or status change")
    return [_truncate_str(item, _MAX_LABEL_ACTION_LEN) for item in actions[:_MAX_LABEL_ACTIONS]]


def _idempotency(
    *,
    issue_id: int | None,
    repo: str | None,
    pr_url: str | None,
    worktree: Path | None,
    branch: str | None,
    source_id: str | None,
    turn_id: str | None,
) -> tuple[str, str, Any]:
    if issue_id is not None and repo is not None:
        material = {"scope": "issue", "issue_id": issue_id, "repo": repo}
        return _hash_material(material), "issue", material
    if pr_url is not None:
        return _hash_material(pr_url), "pr", pr_url
    if worktree is not None and branch:
        material = {
            "scope": "worktree_branch",
            "worktree": str(worktree),
            "branch": branch,
        }
        return _hash_material(material), "worktree_branch", material
    if source_id:
        return _hash_material(source_id), "source_id", source_id
    material = turn_id or "unknown-turn"
    return _hash_material(material), "turn_id", material


def _hash_material(material: Any) -> str:
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _merge_turns(existing: Any, *, turn_id: str | None, source_id: str | None) -> list[dict[str, str | None]]:
    out: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, Mapping):
                continue
            existing_turn = _truncate_str(item.get("turn_id"), 160)
            existing_source = _truncate_str(item.get("source_id"), 160)
            key = (existing_turn, existing_source)
            if key in seen:
                continue
            seen.add(key)
            out.append({"turn_id": existing_turn, "source_id": existing_source})
    key = (_truncate_str(turn_id, 160), _truncate_str(source_id, 160))
    if key not in seen:
        out.append({"turn_id": key[0], "source_id": key[1]})
    return out[-_MAX_RECENT_TURNS:]


def _occurrences(existing: Any) -> int:
    if not isinstance(existing, Mapping):
        return 1
    try:
        previous = int(existing.get("occurrences", 0) or 0)
    except (TypeError, ValueError):
        previous = 0
    return max(1, min(previous + 1, 999))


def _initial_external_comment(existing: Any) -> dict[str, Any]:
    if isinstance(existing, Mapping):
        command = existing.get("command") if isinstance(existing.get("command"), list) else []
        return {
            "attempted": bool(existing.get("attempted")),
            "posted": bool(existing.get("posted")),
            "skipped_reason": _truncate_str(existing.get("skipped_reason"), 120),
            "error": _truncate_str(existing.get("error"), 300),
            "target": _truncate_str(existing.get("target"), 40),
            "target_ref": _truncate_str(existing.get("target_ref"), 300),
            "posted_at": _truncate_str(existing.get("posted_at"), 64),
            "command": [str(item) for item in command],
        }
    return {
        "attempted": False,
        "posted": False,
        "skipped_reason": None,
        "error": None,
        "target": None,
        "target_ref": None,
        "posted_at": None,
        "command": [],
    }


def _handle_external_comment(
    *,
    payload: Mapping[str, Any],
    existing_comment: Any,
    home: Path,
    ctx: TurnContext,
    enable_external_comments: bool,
    chainlink_bin: str,
    gh_bin: str,
    runner: Runner,
    cwd: Path | None,
) -> dict[str, Any]:
    state = _initial_external_comment(existing_comment)
    if state.get("posted"):
        state["skipped_reason"] = "already_posted"
        return state
    if not enable_external_comments:
        state["skipped_reason"] = "disabled"
        return state

    association = payload.get("association") if isinstance(payload.get("association"), Mapping) else {}
    issue_id = association.get("issue_id") if isinstance(association, Mapping) else None
    pr_url = association.get("pr_url") if isinstance(association, Mapping) else None
    repo = association.get("repo") if isinstance(association, Mapping) else None
    target = None
    target_ref = None
    command: list[str] | None = None
    if issue_id is not None and isinstance(repo, str) and repo:
        target = "issue"
        target_ref = str(issue_id)
        command = [chainlink_bin, "issue", "comment", str(issue_id)]
    elif isinstance(pr_url, str) and pr_url and _repo_from_pr_url(pr_url) == repo:
        target = "pr"
        target_ref = pr_url
        command = [gh_bin, "pr", "comment", pr_url]
    if command is None:
        state["skipped_reason"] = "no_validated_target"
        return state

    denial = _comment_authorization_denial(ctx)
    if denial is not None:
        state["skipped_reason"] = denial
        state["target"] = target
        state["target_ref"] = target_ref
        return state

    comment = _render_external_comment(payload)
    argv = (
        [chainlink_bin, "issue", "comment", str(issue_id), comment]
        if target == "issue"
        else [gh_bin, "pr", "comment", str(pr_url), "--body", comment]
    )
    result = runner(argv, cwd or home)
    state["attempted"] = True
    state["target"] = target
    state["target_ref"] = target_ref
    state["command"] = command
    if result.returncode == 0:
        state["posted"] = True
        state["posted_at"] = _utc_now_iso()
        state["skipped_reason"] = None
        state["error"] = None
        return state
    state["posted"] = False
    state["error"] = _truncate_str((result.stderr or result.stdout).strip() or "comment_failed", 300)
    return state


def _comment_authorization_denial(ctx: TurnContext | None) -> str | None:
    if ctx is None:
        return "missing_turn_context"
    if _turn_is_internal_continuation_trigger(ctx):
        return None
    if not bool(getattr(ctx, "access_control_enforced", False)):
        return "admin_access_control_required"
    decision = authorize_action(
        getattr(ctx, "author", None),
        getattr(ctx, "identity_resolver", None),
        admin=True,
        enforce=True,
    )
    return None if decision.allowed else (decision.denial_reason or "admin_required")


def _turn_is_internal_continuation_trigger(ctx: TurnContext) -> bool:
    # External issue/PR comments are allowed automatically only for synthetic /
    # server-owned continuation triggers. Interactive ``user_message`` turns must
    # cross the explicit admin authorization boundary below.
    return (getattr(ctx, "trigger", None) or "").strip() != "user_message"


def _render_external_comment(payload: Mapping[str, Any]) -> str:
    association = payload.get("association") if isinstance(payload.get("association"), Mapping) else {}
    worktree = association.get("worktree") if isinstance(association, Mapping) else None
    worktree_ref = _worktree_ref(Path(worktree)) if isinstance(worktree, str) and worktree else None
    branch = _truncate_str(association.get("branch"), 120) if isinstance(association, Mapping) else None
    validation = payload.get("validation") if isinstance(payload.get("validation"), Mapping) else {}
    next_block = payload.get("next") if isinstance(payload.get("next"), Mapping) else {}
    partial = payload.get("partial_work_state") if isinstance(payload.get("partial_work_state"), Mapping) else {}

    comment_payload = {
        "schema": "worklink_continuation.v1",
        "kind": CONTINUATION_KIND,
        "idempotency_key": payload.get("idempotency_key"),
        "priority": "high",
        "reason": "tool_call_budget_exhausted",
        "created_at": payload.get("created_at"),
        "occurrences": int(payload.get("occurrences") or 1),
        "association": {
            "repo": association.get("repo") if isinstance(association, Mapping) else None,
            "issue_id": association.get("issue_id") if isinstance(association, Mapping) else None,
            "pr_url": association.get("pr_url") if isinstance(association, Mapping) else None,
            "branch": branch,
            "worktree_ref": _truncate_str(worktree_ref, 80),
        },
        "sidecar": {
            "state_ref": f"worklink/continuations/{payload.get('idempotency_key')}.json",
        },
        "partial_work_state": {
            "dirty": partial.get("dirty") if isinstance(partial, Mapping) else None,
            "changed_path_count": partial.get("changed_path_count") if isinstance(partial, Mapping) else None,
        },
        "validation": {
            "state": _truncate_str(validation.get("state"), 20) or "unknown",
            "commands": _sanitize_commands(
                validation.get("commands") if isinstance(validation, Mapping) else [],
                worktree=worktree,
            ),
        },
        "next": {
            "commands": _sanitize_commands(
                next_block.get("commands") if isinstance(next_block, Mapping) else [],
                worktree=worktree,
            ),
            "labels_or_status_changes_needed": _sanitize_labels(
                next_block.get("labels_or_status_changes_needed")
                if isinstance(next_block, Mapping)
                else []
            ),
        },
    }
    return CONTINUATION_PREFIX + json.dumps(comment_payload, sort_keys=True, separators=(",", ":"))


def _sanitize_commands(values: Any, *, worktree: Any) -> list[str]:
    worktree_text = str(worktree) if isinstance(worktree, str) else None
    worktree_ref = _worktree_ref(Path(worktree_text)) if worktree_text else None
    out: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        command = raw.strip()
        if not command:
            continue
        if worktree_text and worktree_ref:
            command = command.replace(worktree_text, f"<worktree:{worktree_ref}>")
        out.append(_truncate_str(command, _MAX_EXTERNAL_COMMAND_LEN) or "")
        if len(out) >= _MAX_EXTERNAL_COMMANDS:
            break
    return out


def _sanitize_labels(values: Any) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        out.append(_truncate_str(text, _MAX_LABEL_ACTION_LEN) or "")
        if len(out) >= _MAX_LABEL_ACTIONS:
            break
    return out


def _worktree_ref(path: Path) -> str | None:
    name = path.name.strip()
    return name or None


def _normalize_labels(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    return sorted({value.strip() for value in values if isinstance(value, str) and value.strip()})


def _emit_continuation_event(**payload: Any) -> None:
    try:
        from ..event_logger import log_event_sync

        log_event_sync("worklink_continuation_created", **payload)
    except Exception:
        return


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_issue_from_text(value: str | None) -> int | None:
    if not value:
        return None
    for pattern in _ISSUE_PATTERNS:
        match = pattern.search(value)
        if match is not None:
            return int(match.group(1))
    return None


def _normalize_validation_state(value: str | None, *, has_command: bool) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"observed", "unrun", "unknown"}:
            return normalized
    return "unrun" if has_command else "unknown"


def _truncate_str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _mapping_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return _truncate_str(value, 200) if isinstance(value, str) else None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _limit_unique(values: Sequence[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _unique(values: Sequence[str | None]) -> list[str]:
    return _limit_unique([value for value in values if isinstance(value, str)], 200)


def _unique_ints(values: Sequence[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
