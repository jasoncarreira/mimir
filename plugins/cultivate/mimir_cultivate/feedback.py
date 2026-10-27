"""Cultivate feedback vocabulary and sanitized event renderers."""

from __future__ import annotations

from mimir.feedback._models import ValenceGroup
from mimir.feedback.rules import FeedbackExtension
from mimir.prompt_safety import sanitize_prompt_field as sanitize


def _tool_pin(ev: dict) -> str:
    tool = sanitize(ev.get("tool_name") or "?")
    pin = sanitize(ev.get("dedupe_key") or "?")
    reason = sanitize(ev.get("reason") or "(no detail)")
    return f"Worklink tool-pin dedupe check failed for {tool} ({pin}): {reason} — issue creation skipped"


def _orphaned(ev: dict) -> str:
    issue = sanitize(ev.get("issue_id") or "?")
    label = sanitize(ev.get("resulting_label") or "?")
    branch = sanitize(ev.get("branch") or "?")
    if ev.get("publication_outcome") == "determined-unpublished" or ev.get("unpublished_commits"):
        return (f"Worklink run #{issue} died; lock released and {label} applied "
                f"because branch {branch} has unpublished commits — recover the checkout")
    if ev.get("publication_outcome") == "undetermined":
        return f"Worklink run #{issue} died; publication status was undetermined and the issue moved to {label}"
    return f"Worklink run #{issue} died; lock released and issue returned to {label}"


def _orphan_reconcile_failed(ev: dict) -> str:
    issue = sanitize(ev.get("issue_id") or "?")
    reason = sanitize(ev.get("reason") or "unknown failure")
    return f"Worklink orphan reconciliation failed for #{issue}: {reason}; run state retained for operator recovery"


def _spawn(kind: str, ev: dict) -> str:
    job_id = sanitize(ev.get("job_id") or "?")
    agent = sanitize(ev.get("agent") or "?")
    cost = ev.get("cost_usd")
    cost_str = f" cost=${cost:.2f}" if isinstance(cost, (int, float)) else ""
    duration = ev.get("duration_ms")
    if isinstance(duration, (int, float)):
        secs = duration / 1000.0
        duration_str = f" {secs:.0f}s" if secs >= 1 else f" {secs:.2f}s"
    else:
        duration_str = ""
    if kind == "spawn_ok":
        return f"claude_code spawn {job_id} ({agent}){duration_str}{cost_str} completed cleanly"
    terminal = sanitize(ev.get("terminal_reason") or "?")
    if kind == "spawn_auth_fail":
        status = ev.get("api_error_status")
        status_str = f" [HTTP {sanitize(status)}]" if status is not None else ""
        return (f"claude_code spawn {job_id} ({agent}){duration_str} auth-failed"
                f"{status_str} terminal={terminal}. Token revoked / refresh-failed / quota-exhausted server-side.")
    return (f"claude_code spawn {job_id} ({agent}){duration_str}{cost_str} "
            f"work-failed terminal={terminal}. Spawn loop ended without a clean PR — "
            f"see logs/bash-jobs/{job_id}.out.")


def _pr_merge_blocked(ev: dict) -> str:
    number = sanitize(ev.get("pr") or ev.get("pr_number") or "?")
    blocking = ev.get("blocking_reviewers") or ev.get("blocking") or []
    if isinstance(blocking, list) and blocking:
        authors = ", ".join(sanitize(b.get("author", "?") if isinstance(b, dict) else str(b)) for b in blocking)
        return f"pr_merge blocked: PR #{number} has CHANGES_REQUESTED from {authors} — resolve before merge"
    return f"pr_merge blocked: PR #{number} has CHANGES_REQUESTED review"


def _auto_review_skipped(ev: dict) -> str:
    repo = sanitize(ev.get("repo") or "?")
    number = sanitize(ev.get("number") or "?")
    return (f"automatic review skipped for {repo}#{number}: PR author is not "
            "a verified collaborator or active organization member; review deliberately")


def _github_issue_notice(ev: dict) -> str:
    repo = sanitize(ev.get("repo") or "?")
    number = sanitize(ev.get("number") or "?")
    title = sanitize(ev.get("title") or "")
    author = sanitize(ev.get("author") or "unknown")
    url = sanitize(ev.get("url") or "")
    return (f"GitHub issue {repo}#{number}: {title} (by @{author}) {url}; "
            "issues aren't handled on service turns yet")


EXTENSION = FeedbackExtension(
    rules={
        "worklink_claimed": ("positive", "worklink_claimed"),
        "worklink_evidence": ("positive", "worklink_evidence"),
        "worklink_transition": ("positive", "worklink_transition"),
        "worklink_continuation_created": ("negative", "worklink_continuation"),
        "worklink_attempts_exhausted": ("negative", "worklink_attempts_exhausted"),
        "worklink_run_orphaned": ("negative", "worklink_run_orphaned"),
        "worklink_run_orphan_reconcile_failed": ("negative", "worklink_run_orphan_reconcile_failed"),
        "worklink_tool_pin_dedupe_check_failed": ("negative", "worklink_tool_pin_dedupe_check_failed"),
        "pr_auto_review_skipped_untrusted_author": ("negative", "pr_auto_review_skipped_untrusted_author"),
        "github_issue_notice": ("negative", "github_issue_notice"),
        "poller_review_missed_submission": ("negative", "review_missed_submission"),
        "github_identity_degraded": ("negative", "github_identity_degraded"),
        "pr_merge_blocked_by_changes_requested": ("negative", "pr_merge_blocked"),
        "claude_code_spawn_completed": ("positive", "spawn_ok"),
        "claude_code_spawn_auth_failed": ("negative", "spawn_auth_fail"),
        "claude_code_spawn_work_failed": ("negative", "spawn_work_fail"),
    },
    renderers={
        "worklink_tool_pin_dedupe_check_failed": _tool_pin,
        "worklink_run_orphaned": _orphaned,
        "worklink_run_orphan_reconcile_failed": _orphan_reconcile_failed,
        "spawn_ok": lambda ev: _spawn("spawn_ok", ev),
        "spawn_auth_fail": lambda ev: _spawn("spawn_auth_fail", ev),
        "spawn_work_fail": lambda ev: _spawn("spawn_work_fail", ev),
        "pr_merge_blocked": _pr_merge_blocked,
        "pr_auto_review_skipped_untrusted_author": _auto_review_skipped,
        "github_issue_notice": _github_issue_notice,
    },
    valence_groups={
        "spawn": ValenceGroup(
            label="spawn", positive_kinds=frozenset({"spawn_ok"}),
            negative_kinds=frozenset({"spawn_auth_fail", "spawn_work_fail"}),
            verb_map={"spawn_ok": "completed", "spawn_auth_fail": "auth-failed", "spawn_work_fail": "work-failed"},
        ),
    },
    first_only_kinds=frozenset({"spawn_ok", "worklink_run_orphaned", "worklink_run_orphan_reconcile_failed"}),
    agent_self_events=frozenset({
        "claude_code_spawn_completed", "claude_code_spawn_auth_failed", "claude_code_spawn_work_failed",
    }),
)


def feedback_extension() -> FeedbackExtension:
    return EXTENSION
