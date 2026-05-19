"""v0.4 §2: algedonic surfacing.

Surfaces recent self-feedback signals (errors, denials, loop hits;
positive engagement) into the agent's turn prompt. Beer's framing:
algedonic channel — pain/pleasure signals that bypass the regulatory
hierarchy and feed back to S5. In mimir, that means the agent's own
errors and successes get a guaranteed prompt slot independent of the
inbound message context.

Source of truth is ``logs/events.jsonl`` + ``logs/turns.jsonl`` — no
parallel state to keep coherent. The reader scans tail-first, stops at
the time window or the per-polarity cap, whichever hits first."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ._jsonl_tail import tail_jsonl_records
from .jsonl_snapshot import JsonlSnapshot, iter_snapshot_or_tail

log = logging.getLogger(__name__)


Polarity = Literal["negative", "positive"]


@dataclass(frozen=True)
class FeedbackSignal:
    ts: str
    polarity: Polarity
    kind: str  # short tag: "tool_denied", "error", "saga_feedback", ...
    channel_id: str | None
    content: str  # one-line rendered description


# Event-type → (polarity, short-tag) mapping. Anything not listed is
# ignored.
_EVENT_RULES: dict[str, tuple[Polarity, str]] = {
    "error": ("negative", "error"),
    "tool_call_denied": ("negative", "tool_denied"),
    "tool_call_budget_warning": ("negative", "tool_budget"),
    # Gap 4 fix: budget_gate.py emits these two names, not the legacy
    # "tool_call_budget_warning". All three are aliased to the same
    # short-tag so the algedonic block surfaces budget pressure
    # regardless of which path fired.
    "tool_call_budget_denied": ("negative", "tool_budget"),
    "tool_call_budget_soft_warning": ("negative", "tool_budget"),
    "send_message_loop_hard_stop": ("negative", "loop_stop"),
    "send_message_loop_warning": ("negative", "loop_warn"),
    "saga_query_error": ("negative", "saga_query_error"),
    "saga_feedback_error": ("negative", "saga_feedback_error"),
    "saga_consolidate_error": ("negative", "saga_consolidate_error"),
    "saga_decay_error": ("negative", "saga_decay_error"),
    "saga_forget_error": ("negative", "saga_forget_error"),
    "saga_synthesis_dispatch_failed": ("negative", "synth_dispatch_fail"),
    "saga_synthesis_empty_window": ("negative", "synth_empty_window"),
    # CR#19: synthesis-turn post-check; agent ran the synthesis turn
    # but skipped step 3 (saga_end_session). Without the boundary
    # atom the next session has no "what were we doing last time?"
    # record. Negative so the agent's next turn surfaces it and the
    # behavior self-corrects.
    "saga_synthesis_skipped_boundary": ("negative", "synth_skip_boundary"),
    "cost_rate_alert": ("negative", "cost_rate"),
    # chainlink #13: under billing-mode=quota, cost spikes don't
    # suppress (the binding constraint is plan-window quota, not
    # dollars-spent). Emit as positive-bucket "noticed, no action
    # needed" so the message doesn't read as "scale back".
    "cost_rate_advisory": ("positive", "cost_rate_advisory"),
    "rate_limit_warning": ("negative", "rate_limit_warn"),
    "rate_limit_rejected": ("negative", "rate_limit_reject"),
    "rate_limit_off_pace": ("negative", "rate_limit_off_pace"),
    "scheduled_tick_dropped": ("negative", "tick_dropped"),
    "scheduled_tick_suppressed": ("negative", "tick_suppressed"),
    "heartbeat_health_degraded": ("negative", "heartbeat_health"),
    "introspection_report_error": ("negative", "introspection_error"),
    "predictions_pending_review": ("negative", "predictions_pending"),
    "send_message_unknown_channel": ("negative", "unknown_channel"),
    "auto_dispatch_failed": ("negative", "auto_dispatch_failed"),
    # OAuth usage poller — plan-window quota probe runs on a cron and
    # surfaces refresh / logged-out / age-warn signals algedonically so
    # the agent can route operator-actionable alerts through.
    "oauth_usage_failed": ("negative", "oauth_usage_failed"),
    "oauth_logged_out": ("negative", "oauth_logged_out"),
    "oauth_refresh_token_age_warn": ("negative", "oauth_refresh_age_warn"),
    "oauth_usage_ok": ("positive", "oauth_usage_ok"),
    "oauth_refresh_ok": ("positive", "oauth_refresh_ok"),
    # Bind-mount health probe — detects VirtioFS stale-inode failures
    # and self-restarts. Stale + restart-triggered surfaces as a
    # negative; persistent-after-N-restarts is a louder negative
    # operator signal; recovery after a self-restart is positive.
    "bind_mount_stale_detected": ("negative", "bind_mount_stale"),
    "bind_mount_stale_persistent": ("negative", "bind_mount_persistent"),
    "bind_mount_recovered": ("positive", "bind_mount_recovered"),
    # CR#22 layer a: OAuth poller rejected an implausible 5h reading
    # (large jump unmatched by 7d response). Negative because the
    # operator should know the upstream endpoint is glitching, even
    # though the arbiter is now insulated.
    "quota_reading_anomalous": ("negative", "quota_anomaly"),
    # PR 4a (git_tracking): post-turn commit/push failures. All three
    # are first-occurrence-only (see ``_FIRST_OCCURRENCE_ONLY_KINDS``)
    # because once git breaks every subsequent turn re-emits — without
    # dedup the failure crowds out everything else in the 24h window.
    "git_commit_failed": ("negative", "git_commit_failed"),
    "git_push_failed": ("negative", "git_push_failed"),
    "git_pull_blocked": ("negative", "git_pull_blocked"),
    # PR 56 (shell-jobs): the cross-thread bridge from waiter thread
    # back to the dispatcher raised. Means a finished async shell job
    # never woke the spawning channel — the operator sees a missing
    # wake-up before they'd notice without surfacing here.
    "shell_job_complete_enqueue_failed": ("negative", "shell_job_complete_enqueue_failed"),
    # chainlink #65 (sub B): paired positive counterparts for the
    # ntfy / git / shell-job failure kinds above. First-occurrence-only
    # dedup makes failures sticky for 24h regardless of recovery —
    # without these, the operator sees ``ntfy_post_failed at 02:14``
    # but has no way to tell whether the channel recovered at 02:15
    # or is still broken at 21:00. Paired positives let them read the
    # contrast ("old failure + recent success = transient, recovered")
    # without a separate success-cancels-failure pass. See chainlink
    # #36 comment 42 for the design call.
    "ntfy_post_ok": ("positive", "ntfy_post_ok"),
    "git_push_ok": ("positive", "git_push_ok"),
    "git_pull_ok": ("positive", "git_pull_ok"),
    "git_fetch_ok": ("positive", "git_fetch_ok"),
    "shell_job_complete_enqueue_ok": ("positive", "shell_job_complete_enqueue_ok"),
    # Discord bridge supervisor — surfaces sustained Discord-side
    # degradation (5xx at token-auth, gateway disconnect storms) that
    # would otherwise live only in container logs. ``discord_bridge_retry``
    # fires after 3 consecutive retry attempts; the login/intents
    # failures are operator-actionable (token rotation / intent enable
    # in dev portal) and surface immediately on first occurrence.
    "discord_bridge_retry": ("negative", "discord_bridge_retry"),
    "discord_bridge_login_failure": ("negative", "discord_bridge_login_failure"),
    "discord_bridge_intents_failure": ("negative", "discord_bridge_intents_failure"),
    # Slack bridge supervisor — same shape as Discord. Retry fires on
    # sustained transient outages; auth/scope failures are
    # operator-actionable and surface immediately.
    "slack_bridge_retry": ("negative", "slack_bridge_retry"),
    "slack_bridge_auth_failure": ("negative", "slack_bridge_auth_failure"),
    "slack_bridge_scope_failure": ("negative", "slack_bridge_scope_failure"),
    # Positive — agent's own contribution-credit pass to SAGA is the
    # one signal currently emitted regardless of bridge reaction wiring.
    "saga_feedback_sent": ("positive", "saga_feedback"),
    # Plumbed for when bridges emit inbound reactions; harmless when absent.
    "react_received": ("positive", "react"),
    # Cron success signals — surface so the agent knows the maintenance
    # crons ran (and where their output landed). Especially important
    # for the introspection report, which produces a markdown file the
    # agent should know about so it can Read it.
    "saga_consolidate_ok": ("positive", "saga_consolidate_ok"),
    "saga_decay_ok": ("positive", "saga_decay_ok"),
    "saga_forget_ok": ("positive", "saga_forget_ok"),
    "introspection_report_ok": ("positive", "introspection_ok"),
    # Wiki health — emitted by `mimir wiki backlinks` when the wiki has
    # orphan pages or dangling links. Clean wikis emit nothing (no spam
    # for the no-action-needed case). First-occurrence-only: a chronic
    # orphan count would otherwise re-render every turn and crowd out
    # acute signals; the agent sees it once per 24h window and clears
    # the orphans on a lint pass.
    "wiki_backlinks_unhealthy": ("negative", "wiki_health"),
    # chainlink #60: spawn_claude_code completion signals. ``_completed``
    # fires on a clean spawn exit; ``_auth_failed`` on 4xx + is_error
    # (token revoked, refresh failed, server-side quota exhausted);
    # ``_work_failed`` on the spawned agent loop ending badly
    # (max-turns / max-budget / errored / unparseable JSON).
    "claude_code_spawn_completed": ("positive", "spawn_ok"),
    "claude_code_spawn_auth_failed": ("negative", "spawn_auth_fail"),
    "claude_code_spawn_work_failed": ("negative", "spawn_work_fail"),
    # Commitments Phase 2b — periodic due-check sweep emits these.
    # ``commitment_due``: positive — "informational follow-through
    # nudge." The commitment is in its due window and the agent should
    # act. Not a miss; not a failure. Mimir's positive signals are
    # gentler than negative; this fits the same shape as the
    # "saga_feedback_sent" positive line.
    # ``commitment_expired``: negative — the window fully elapsed
    # without the agent acting. This IS a miss; surface it loudly so
    # the next session-end synthesis can reason about why.
    "commitment_due": ("positive", "commitment_due"),
    "commitment_expired": ("negative", "commitment_expired"),
    # Phase 2b: pileup signal. Fires when any single commitment's
    # snooze_count crosses the threshold (default 3). Surfaces
    # "I keep punting this thing" as a feedback loop so the next
    # session-end synthesis reflects on overcommitment / avoidance.
    # Negative polarity — pileups are an avoidance smell, not a
    # neutral event.
    "commitment_snooze_pileup": ("negative", "commitment_snooze_pileup"),
}


# Kinds where only the most recent occurrence in the window should
# render. Cron-fired events (weekly maintenance) re-appear on every
# heartbeat in the 24h algedonic window, otherwise — same line × 24
# would crowd out genuinely-new positive signals like react_received
# or saga_feedback_sent. Tail-first iteration means the first-seen
# event for a kind in this set IS the most recent.
# CR2 (memory & retrieval) fix: kinds whose polarity is decided
# dynamically per event (rather than statically by their entry in
# ``_RULE``) MUST NOT appear in ``_FIRST_OCCURRENCE_ONLY_KINDS``. The
# kind-level dedup is global (not per-polarity), so a polarity-dynamic
# kind would suppress the OTHER polarity bucket's matching event after
# the first occurrence — silently dropping signal. Today only
# ``react_received`` is polarity-dynamic and it's NOT in the first-only
# set; the assertion below pins that invariant so a future change
# can't break it without seeing the test fail.
_POLARITY_DYNAMIC_KINDS: frozenset[str] = frozenset({"react"})


_FIRST_OCCURRENCE_ONLY_KINDS: set[str] = {
    "saga_consolidate_ok",
    "saga_decay_ok",
    "saga_forget_ok",
    "introspection_ok",
    "introspection_error",
    "heartbeat_health",
    "predictions_pending",
    # chainlink #13: cost_rate_advisory fires with the same cooldown
    # cadence as cost_rate_alert (60 min default), so multiple within
    # the 24h window add no information — the latest is the live state.
    "cost_rate_advisory",
    # OAuth poller fires every few minutes — without dedup the
    # success line would crowd out everything else in the window.
    # Failure / logged-out / age-warn dedup too: re-emitting per-poll
    # adds no information, the latest one is what the agent acts on.
    "oauth_usage_ok",
    "oauth_usage_failed",
    "oauth_logged_out",
    "oauth_refresh_age_warn",
    # CR#22 layer a: an endpoint glitch keeps re-reporting the same
    # bogus 5h reading every 3-min poll until it self-corrects. The
    # algedonic block only needs the latest one.
    "quota_anomaly",
    "oauth_refresh_ok",
    # Health probe runs every minute — without dedup the line would
    # crowd out everything else in the 24h algedonic window. Tail-first
    # iteration means the first occurrence in the set IS the most recent.
    "bind_mount_stale",
    "bind_mount_persistent",
    "bind_mount_recovered",
    # PR 4a (git_tracking): a stuck network outage / auth issue / dirty
    # tree will re-emit on every turn until resolved. Dedup so the
    # operator-visible signal is the *most recent* failure, not 50
    # copies of the same error in the 24h window.
    "git_commit_failed",
    "git_push_failed",
    "git_pull_blocked",
    # PR 56: a broken bridge fires on every async shell job that
    # completes. Same shape as git_*_failed — dedup so the operator
    # sees the latest failure, not N stale copies.
    "shell_job_complete_enqueue_failed",
    # chainlink #65 (sub B): paired positives. Latest-only on both
    # polarities — a healthy ntfy/git/shell pipeline fires success
    # on every poll/turn, which would otherwise crowd out the rest
    # of the algedonic block. The most-recent success is the live
    # state; older copies add no information.
    "ntfy_post_ok",
    "git_push_ok",
    "git_pull_ok",
    "git_fetch_ok",
    "shell_job_complete_enqueue_ok",
    # Discord bridge supervisor: a sustained outage fires
    # ``discord_bridge_retry`` every backoff tick (5s → 5min cap).
    # Without dedup the algedonic block would fill with retry lines
    # for the duration of the outage. Latest-only surfaces "still
    # retrying after N attempts" — the most recent attempt count is
    # the operator-actionable signal.
    "discord_bridge_retry",
    "slack_bridge_retry",
    # Wiki health: chronic orphan/dangling counts would re-render every
    # turn the bot runs `mimir wiki backlinks`. Latest-only is right —
    # the agent sees "wiki has N orphans" once per 24h window and
    # decides whether to do a lint pass.
    "wiki_health",
    # chainlink #60: a busy spawn day can fire ``spawn_ok`` many times.
    # Latest-only on the success side keeps positive signals from
    # crowding out other shapes (saga_feedback, react_received, etc.).
    # Failures stay un-deduped — each spawn_auth_fail / spawn_work_fail
    # is a distinct incident with a distinct job_id worth seeing.
    "spawn_ok",
    # 2026-05-09: ``saga_feedback_sent`` fires once per turn that
    # retrieves SAGA atoms (the post-message contribution-credit pass).
    # During poller-heavy windows (github-poller fires ~5/min, each
    # wakeup runs a turn, each turn credits atoms) this floods the 24h
    # algedonic window with 5+ identical-shape lines that say nothing
    # the operator can act on — "yes the credit pass ran again" isn't
    # information. Same shape as ``oauth_usage_ok`` (every-3-min poll)
    # which is already deduped. Latest-only surfaces "the credit pass
    # is alive" without crowding out genuinely-new positive signals
    # like react_received or PR-merged events. Note: dedup keys on
    # rule_kind (line 128 maps event ``saga_feedback_sent`` → kind
    # ``saga_feedback``), not the raw event type.
    "saga_feedback",
    # Commitments Phase 2b. The poller fires every 5 min and the same
    # commitment (eg. "review PR #111") could surface in N sweeps if
    # the agent hasn't acted. Latest-only keeps the algedonic block
    # focused on "most-recently-due commitment" + "most-recent expiry"
    # — the Phase 3 prompt-builder block carries the full pending list
    # so no information is lost; the algedonic line is the attention-
    # grabber.
    "commitment_due",
    "commitment_expired",
    # Phase 2b: snooze pileup fires from the poller every tick while
    # any commitment is above threshold. Latest-only at the algedonic
    # layer means one line surfaces — the most recently flagged
    # commitment. The full pending list lives in the Phase 3 prompt
    # block so the agent can see all the offenders if needed.
    "commitment_snooze_pileup",
}

# CR2 (memory & retrieval) invariant: polarity-dynamic kinds (their
# polarity is decided per-event, not by static rule) MUST NOT appear
# in _FIRST_OCCURRENCE_ONLY_KINDS, because the first-only dedup is
# global (kind-level) — a polarity-dynamic kind would suppress
# matching events of the OTHER polarity after the first occurrence,
# silently dropping signal.
assert _POLARITY_DYNAMIC_KINDS.isdisjoint(_FIRST_OCCURRENCE_ONLY_KINDS), (
    "Polarity-dynamic kinds are incompatible with _FIRST_OCCURRENCE_ONLY_KINDS "
    "(global kind-level dedup would silently suppress the other-polarity "
    "events). Make seen_first_only per-polarity or remove the kind from "
    f"the first-only set. Conflict: "
    f"{_POLARITY_DYNAMIC_KINDS & _FIRST_OCCURRENCE_ONLY_KINDS}"
)


# Render hooks: per-kind one-liner builders. Defaults to a generic
# "<kind>: <event-type-specific note>" if no specialized renderer fits.
def _render_event_line(rule_kind: str, ev: dict) -> str:
    if rule_kind == "tool_denied":
        tool = ev.get("tool") or ev.get("name") or "?"
        reason = ev.get("reason") or ev.get("error") or ""
        suffix = f": {reason}" if reason else ""
        return f"tool_denied {tool}{suffix}"
    if rule_kind == "tool_budget":
        used = ev.get("count")
        cap = ev.get("budget")
        return f"tool_budget_warning ({used}/{cap})"
    if rule_kind == "loop_stop":
        return f"send_message_loop_hard_stop after {ev.get('count', '?')}"
    if rule_kind == "loop_warn":
        return f"send_message_loop_warning at {ev.get('count', '?')}"
    if rule_kind == "saga_query_error":
        return f"SAGA query failed: {ev.get('error') or '(no detail)'}"
    if rule_kind == "saga_feedback_error":
        return f"SAGA feedback failed: {ev.get('error') or '(no detail)'}"
    if rule_kind == "saga_consolidate_error":
        return f"SAGA consolidation failed: {ev.get('error') or '(no detail)'}"
    if rule_kind == "saga_decay_error":
        return f"SAGA decay failed: {ev.get('error') or '(no detail)'}"
    if rule_kind == "auto_dispatch_failed":
        bridge = ev.get("bridge") or "?"
        ch = ev.get("channel_id") or "?"
        err = ev.get("error") or "(no detail)"
        return (
            f"auto-dispatch reply failed via {bridge} on {ch}: {err}. "
            f"Your text was generated but not delivered. "
            f"Consider calling send_message explicitly next time."
        )
    if rule_kind == "saga_forget_error":
        dry = ev.get("dry_run")
        suffix = " (dry_run)" if dry else ""
        return f"SAGA forget failed{suffix}: {ev.get('error') or '(no detail)'}"
    if rule_kind == "synth_dispatch_fail":
        return f"SAGA synthesis dispatch failed: {ev.get('error') or '(no detail)'}"
    if rule_kind == "synth_empty_window":
        return (
            f"SAGA synthesis ran with empty turn window "
            f"(session={ev.get('saga_session_id') or '?'}); "
            f"{ev.get('reason') or 'no detail'}"
        )
    if rule_kind == "cost_rate":
        rate = ev.get("rate_now_usd_per_hour")
        threshold = ev.get("threshold_usd_per_hour")
        reason = ev.get("reason") or "?"
        rate_str = f"${rate:.2f}/hr" if isinstance(rate, (int, float)) else "?"
        thr_str = f"${threshold:.2f}/hr" if isinstance(threshold, (int, float)) else "?"
        return f"cost rate alert: {rate_str} exceeds {thr_str} ({reason})"
    if rule_kind == "cost_rate_advisory":
        # chainlink #13: quota-mode "FYI" — the spike triggered our
        # spike_ratio math, but cost isn't the binding constraint, so
        # the message is informational. NOT phrased as "scale back" —
        # plan quota will gate independently if it needs to.
        rate = ev.get("rate_now_usd_per_hour")
        baseline = ev.get("baseline_usd_per_hour")
        rate_str = f"${rate:.2f}/hr" if isinstance(rate, (int, float)) else "?"
        if isinstance(baseline, (int, float)) and baseline > 0 and isinstance(rate, (int, float)):
            ratio = rate / baseline
            return (
                f"cost rate noted: {rate_str} ({ratio:.1f}× weekly baseline). "
                f"Advisory under quota billing mode — plan-window quota is the "
                f"binding constraint."
            )
        return (
            f"cost rate noted: {rate_str}. Advisory under quota billing mode — "
            f"plan-window quota is the binding constraint."
        )
    if rule_kind in ("rate_limit_warn", "rate_limit_reject"):
        rl_type = ev.get("rate_limit_type") or "?"
        util = ev.get("utilization")
        util_str = (
            f"{util * 100:.0f}% used"
            if isinstance(util, (int, float))
            else "n/a"
        )
        verb = "approaching" if rule_kind == "rate_limit_warn" else "hit"
        return f"plan limit {verb} ({rl_type} — {util_str})"
    if rule_kind == "rate_limit_off_pace":
        rl_type = ev.get("rate_limit_type") or "?"
        on_pace = ev.get("on_pace_utilization")
        on_pace_str = (
            f"projects {on_pace * 100:.0f}% by reset"
            if isinstance(on_pace, (int, float))
            else "off pace"
        )
        return f"plan window off pace ({rl_type} — {on_pace_str})"
    if rule_kind == "tick_dropped":
        return f"scheduled_tick dropped: {ev.get('reason') or '(no reason)'}"
    if rule_kind == "tick_suppressed":
        return f"scheduled_tick suppressed by arbiter: {ev.get('reason') or '(no reason)'}"
    if rule_kind == "heartbeat_health":
        rate = ev.get("success_rate")
        thr = ev.get("threshold")
        rate_str = f"{rate * 100:.0f}%" if isinstance(rate, (int, float)) else "?"
        thr_str = f"{thr * 100:.0f}%" if isinstance(thr, (int, float)) else "?"
        return (
            f"heartbeat pipeline degraded: success rate {rate_str} "
            f"(threshold {thr_str}, {ev.get('successful', '?')}/"
            f"{ev.get('fired', '?')} fired)"
        )
    if rule_kind == "introspection_error":
        return (
            f"introspection report failed: {ev.get('error') or '(no detail)'}"
        )
    if rule_kind == "predictions_pending":
        n = ev.get("count")
        n_str = str(int(n)) if isinstance(n, (int, float)) else "?"
        return (
            f"{n_str} predictions past horizon — run `mimir predictions "
            f"review` to score them"
        )
    if rule_kind == "wiki_health":
        orphans = ev.get("orphan_count")
        dangling = ev.get("dangling_count")
        parts: list[str] = []
        if isinstance(orphans, (int, float)) and orphans > 0:
            parts.append(f"{int(orphans)} orphan{'' if orphans == 1 else 's'}")
        if isinstance(dangling, (int, float)) and dangling > 0:
            parts.append(
                f"{int(dangling)} dangling link{'' if dangling == 1 else 's'}"
            )
        summary = " / ".join(parts) if parts else "issues detected"
        return (
            f"wiki health: {summary} — "
            f"see state/wiki/orphans.md, state/wiki/dangling-links.md"
        )
    if rule_kind == "saga_consolidate_ok":
        result = ev.get("result") or {}
        if isinstance(result, dict):
            merged = result.get("atoms_merged")
            retired = result.get("atoms_retired")
            clusters = result.get("clusters_processed")
            duration = result.get("duration_s")
            parts: list[str] = []
            if isinstance(clusters, (int, float)):
                parts.append(f"{int(clusters)} clusters")
            if isinstance(merged, (int, float)):
                parts.append(f"{int(merged)} merged")
            if isinstance(retired, (int, float)):
                parts.append(f"{int(retired)} retired")
            if isinstance(duration, (int, float)):
                parts.append(f"{duration:.1f}s")
            detail = ", ".join(parts) if parts else "no detail"
            return f"saga consolidation ran ({detail})"
        return "saga consolidation ran"
    if rule_kind == "saga_decay_ok":
        # Note: forget-candidate count is surfaced separately as a
        # persistent line in the ## Self-state block (see
        # ``pending_forget_candidates_count``) — it stays visible until
        # the agent acts (a saga_forget_ok event newer than this decay
        # event clears it). Keeping it off the per-run line avoids
        # duplicating the same count adjacent to itself.
        result = ev.get("result") or {}
        if isinstance(result, dict):
            updated = result.get("atoms_retrievability_updated")
            faded = result.get("atoms_faded")
            dormanted = result.get("atoms_dormanted")
            compacted = result.get("atoms_compacted")
            duration = result.get("elapsed_seconds")
            parts: list[str] = []
            if isinstance(updated, (int, float)):
                parts.append(f"{int(updated)} retrievability updates")
            transitions_total = 0
            if isinstance(faded, (int, float)):
                transitions_total += int(faded)
            if isinstance(dormanted, (int, float)):
                transitions_total += int(dormanted)
            if transitions_total:
                parts.append(f"{transitions_total} state transitions")
            if isinstance(compacted, (int, float)) and compacted:
                parts.append(f"{int(compacted)} compacted")
            if isinstance(duration, (int, float)):
                parts.append(f"{duration:.1f}s")
            detail = ", ".join(parts) if parts else "no detail"
            return f"saga decay ran ({detail})"
        return "saga decay ran"
    if rule_kind == "saga_forget_ok":
        dry = ev.get("dry_run")
        actions = ev.get("actions_taken")
        total = ev.get("total_candidates")
        bits: list[str] = []
        if isinstance(total, (int, float)):
            bits.append(f"{int(total)} candidates reviewed")
        if isinstance(actions, (int, float)):
            bits.append(f"{int(actions)} acted on")
        detail = ", ".join(bits) if bits else "no detail"
        suffix = " (dry_run)" if dry else ""
        return f"saga forget ran{suffix} ({detail})"
    if rule_kind == "introspection_ok":
        # The output file path is the load-bearing detail — the agent
        # should be able to grep this line and Read the report.
        out = ev.get("output") or "(no path)"
        rate = ev.get("pipeline_success_rate")
        if isinstance(rate, (int, float)):
            tail = f", scheduled-tick success {rate * 100:.0f}%"
        else:
            tail = ""
        return f"introspection report ready: {out}{tail}"
    if rule_kind == "oauth_usage_ok":
        recorded = ev.get("recorded") or {}
        if isinstance(recorded, dict) and recorded:
            five = recorded.get("five_hour", {})
            seven = recorded.get("seven_day", {})
            parts: list[str] = []
            five_util = five.get("utilization") if isinstance(five, dict) else None
            seven_util = seven.get("utilization") if isinstance(seven, dict) else None
            if isinstance(five_util, (int, float)):
                parts.append(f"5h {five_util * 100:.0f}%")
            if isinstance(seven_util, (int, float)):
                parts.append(f"7d {seven_util * 100:.0f}%")
            detail = ", ".join(parts) if parts else f"{len(recorded)} windows"
            return f"oauth usage poll ok ({detail})"
        return "oauth usage poll ok"
    if rule_kind == "oauth_usage_failed":
        stage = ev.get("stage") or "?"
        err = ev.get("error") or "(no detail)"
        status = ev.get("status")
        suffix = f" [HTTP {status}]" if status is not None else ""
        return f"oauth usage poll failed at {stage}{suffix}: {err}"
    if rule_kind == "oauth_logged_out":
        stage = ev.get("stage") or "?"
        return (
            f"OAuth logged out (refresh failed at {stage}). "
            f"Operator action: re-run ``claude /login`` and copy "
            f"~/.claude/.credentials.json into the mimir homedir."
        )
    if rule_kind == "oauth_refresh_age_warn":
        age = ev.get("age_days")
        thr = ev.get("warn_threshold_days")
        age_str = f"{age:.1f}d" if isinstance(age, (int, float)) else "?"
        thr_str = f"{thr}d" if isinstance(thr, (int, float)) else "?"
        return (
            f"OAuth credentials are {age_str} old (warn threshold {thr_str}). "
            f"Consider re-running ``claude /login`` before the refresh "
            f"token expires."
        )
    if rule_kind == "oauth_refresh_ok":
        return "oauth access token refreshed"
    if rule_kind == "bind_mount_stale":
        recent = ev.get("recent_restarts")
        cap = ev.get("max_restarts_per_hour")
        count_str = (
            f"count: {int(recent) + 1}/{cap} in last 60min"
            if isinstance(recent, (int, float)) and isinstance(cap, (int, float))
            else "auto-restart triggered"
        )
        home = ev.get("home") or "/mimir-home"
        return (
            f"Bind mount stale-inode detected ({home}); "
            f"auto-restart triggered ({count_str})"
        )
    if rule_kind == "bind_mount_persistent":
        recent = ev.get("recent_restarts")
        n_str = f"{int(recent)}" if isinstance(recent, (int, float)) else "?"
        return (
            f"Bind mount stale-inode persists despite {n_str} auto-restarts in "
            f"last 60min; operator action needed (try ``docker compose down "
            f"&& up``)"
        )
    if rule_kind == "bind_mount_recovered":
        return "Bind mount healthy again after auto-restart"
    if rule_kind == "quota_anomaly":
        # CR#22 layer a: cross-window sanity rejected a 5h spike that
        # didn't match the 7d trajectory. Surface what got rejected so
        # the operator can see the suspect reading + know the kept value.
        kept = ev.get("kept_utilization")
        rejected = ev.get("rejected_utilization")
        kept_str = f"{kept * 100:.0f}%" if isinstance(kept, (int, float)) else "?"
        rejected_str = (
            f"{rejected * 100:.0f}%"
            if isinstance(rejected, (int, float)) else "?"
        )
        return (
            f"quota reading anomaly: 5h endpoint reported {rejected_str} "
            f"(kept previous {kept_str}); arbiter consults the last "
            f"trusted value. Glitch is transient — usually clears within "
            f"a poll cycle or two."
        )
    if rule_kind == "git_commit_failed":
        stage = ev.get("stage") or "?"
        err = ev.get("error") or "(no detail)"
        return (
            f"git commit failed at {stage}: {err}. "
            f"Tracked-state changes from this turn are NOT staged. "
            f"Investigate /mimir-home git status; next successful turn re-tries."
        )
    if rule_kind == "git_push_failed":
        reason = ev.get("reason") or "(no detail)"
        rc = ev.get("returncode")
        rc_suffix = f" [rc={rc}]" if rc is not None else ""
        return (
            f"git push to mimirbot-state failed: {reason}{rc_suffix}. "
            f"Local commits stand; the next debounced push catches up "
            f"once connectivity / auth is restored."
        )
    if rule_kind == "git_pull_blocked":
        reason = ev.get("reason") or "(no detail)"
        return (
            f"git pull blocked: {reason}. Container's local branch has "
            f"diverged from remote — operator must reconcile manually "
            f"(reset local to remote, or push divergent commits)."
        )
    if rule_kind == "shell_job_complete_enqueue_failed":
        job_id = ev.get("job_id") or "?"
        err = ev.get("error") or "(no detail)"
        return (
            f"shell job {job_id} finished but the wake-up event "
            f"failed to enqueue: {err}. The job's exit + output are on "
            f"disk under logs/bash-jobs/; the spawning channel did NOT "
            f"resume. Investigate dispatcher state."
        )
    # chainlink #65 (sub B): paired success renderers for the
    # ntfy / git / shell-job families. Brief past-tense, one line
    # each — kept terse so the positive block doesn't bloat. The
    # contrast against the paired failure line is the signal; the
    # algedonic block's outer rendering already carries timestamps,
    # so per-line "at HH:MM UTC" is redundant and would be asymmetric
    # with the existing git_push_failed / shell_job_complete_enqueue_failed
    # renderers (which don't include timestamp suffixes either).
    if rule_kind == "ntfy_post_ok":
        return "ntfy post succeeded"
    if rule_kind == "git_push_ok":
        return "git push to mimirbot-state succeeded"
    if rule_kind == "git_pull_ok":
        return "git pull --ff-only succeeded"
    if rule_kind == "git_fetch_ok":
        return "git fetch succeeded"
    if rule_kind == "shell_job_complete_enqueue_ok":
        job_id = ev.get("job_id") or "?"
        return f"shell job {job_id} wake-up enqueued"
    if rule_kind == "discord_bridge_retry":
        attempt = ev.get("attempt", "?")
        backoff = ev.get("backoff_seconds")
        err = ev.get("error") or "(no detail)"
        backoff_str = f" (next retry in {backoff}s)" if backoff else ""
        return (
            f"Discord bridge supervisor: {attempt} consecutive retry "
            f"attempts on connect — {err}{backoff_str}. Most likely "
            f"Discord-side gateway / API outage; the supervisor is "
            f"still retrying with exponential backoff. Inbound + "
            f"outbound Discord traffic is paused until reconnect."
        )
    if rule_kind == "discord_bridge_login_failure":
        err = ev.get("error") or "(no detail)"
        return (
            f"Discord bridge: token auth permanently rejected ({err}). "
            f"Operator must rotate the bot token in `.env` "
            f"(DISCORD_TOKEN) and restart the container — the bridge "
            f"supervisor has stopped retrying and Discord traffic is "
            f"down for the rest of this container's lifetime."
        )
    if rule_kind == "discord_bridge_intents_failure":
        err = ev.get("error") or "(no detail)"
        return (
            f"Discord bridge: privileged intents required ({err}). "
            f"Operator must enable members + message_content intents "
            f"in the Discord developer portal for this bot, then "
            f"restart the container."
        )
    if rule_kind == "slack_bridge_retry":
        attempt = ev.get("attempt", "?")
        backoff = ev.get("backoff_seconds")
        slack_err = ev.get("slack_error") or ""
        err = ev.get("error") or "(no detail)"
        backoff_str = f" (next retry in {backoff}s)" if backoff else ""
        slack_err_str = f" slack-error={slack_err}" if slack_err else ""
        return (
            f"Slack bridge supervisor: {attempt} consecutive retry "
            f"attempts on connect — {err}{slack_err_str}{backoff_str}. "
            f"Most likely Slack-side Socket Mode / API outage; the "
            f"supervisor is still retrying with exponential backoff. "
            f"Inbound + outbound Slack traffic is paused until reconnect."
        )
    if rule_kind == "slack_bridge_auth_failure":
        slack_err = ev.get("slack_error") or "(unknown)"
        return (
            f"Slack bridge: terminal auth failure ({slack_err}). "
            f"Operator must rotate the Slack bot/app tokens in `.env` "
            f"(SLACK_BOT_TOKEN / SLACK_APP_TOKEN) and restart the "
            f"container — the bridge supervisor has stopped retrying "
            f"and Slack traffic is down for the rest of this "
            f"container's lifetime."
        )
    if rule_kind == "slack_bridge_scope_failure":
        return (
            f"Slack bridge: missing OAuth scope. Operator must add the "
            f"required scope in the Slack app dashboard (api.slack.com → "
            f"OAuth & Permissions), reinstall the app to refresh the "
            f"token, then restart the container."
        )
    if rule_kind in ("spawn_ok", "spawn_auth_fail", "spawn_work_fail"):
        job_id = ev.get("job_id") or "?"
        agent_name = ev.get("agent") or "?"
        cost = ev.get("cost_usd")
        cost_str = f" cost=${cost:.2f}" if isinstance(cost, (int, float)) else ""
        duration = ev.get("duration_ms")
        if isinstance(duration, (int, float)):
            secs = duration / 1000.0
            duration_str = f" {secs:.0f}s" if secs >= 1 else f" {secs:.2f}s"
        else:
            duration_str = ""
        if rule_kind == "spawn_ok":
            return (
                f"claude_code spawn {job_id} ({agent_name}){duration_str}{cost_str} "
                f"completed cleanly"
            )
        terminal = ev.get("terminal_reason") or "?"
        if rule_kind == "spawn_auth_fail":
            status = ev.get("api_error_status")
            status_str = f" [HTTP {status}]" if status is not None else ""
            return (
                f"claude_code spawn {job_id} ({agent_name}){duration_str} "
                f"auth-failed{status_str} terminal={terminal}. "
                f"Token revoked / refresh-failed / quota-exhausted server-side."
            )
        # spawn_work_fail
        return (
            f"claude_code spawn {job_id} ({agent_name}){duration_str}{cost_str} "
            f"work-failed terminal={terminal}. "
            f"Spawn loop ended without a clean PR — see logs/bash-jobs/{job_id}.out."
        )
    if rule_kind == "unknown_channel":
        return f"send_message to unknown channel {ev.get('channel_id', '?')}"
    if rule_kind == "saga_feedback":
        n = ev.get("n_atoms")
        return f"saga_feedback_sent ({n} atoms credited)"
    if rule_kind == "commitment_due":
        # Phase 2b. Surface the commitment ID + channel + text so the
        # agent can match the prompt-builder block's structured list
        # against the algedonic nudge. The Phase 3 prompt block
        # carries the canonical pending list; this is the
        # attention-grabber on the most-recently-due one.
        cid = ev.get("commitment_id") or "?"
        text = (ev.get("text") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        channel = ev.get("channel_id")
        recipient = ev.get("recipient_identity")
        scope_parts: list[str] = []
        if channel:
            scope_parts.append(f"chan={channel}")
        if recipient:
            scope_parts.append(f"@{recipient}")
        scope = f" [{', '.join(scope_parts)}]" if scope_parts else ""
        return f"commitment due {cid}{scope}: {text}"
    if rule_kind == "commitment_expired":
        # Phase 2b — the actual miss. Negative polarity so it surfaces
        # in the agent's "things to notice" block. Same shape as the
        # _due line above; the polarity is the load-bearing difference.
        cid = ev.get("commitment_id") or "?"
        text = (ev.get("text") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        channel = ev.get("channel_id")
        scope = f" [chan={channel}]" if channel else ""
        return (
            f"commitment EXPIRED {cid}{scope}: {text} — "
            f"due window elapsed without follow-through. "
            f"Reflect at next session boundary."
        )
    if rule_kind == "commitment_snooze_pileup":
        # Phase 2b — "this commitment keeps getting punted." Operator
        # framing: snooze counts starting to rise is an avoidance /
        # overcommitment smell worth reflecting on at the next session
        # boundary. Negative polarity; first-occurrence-only dedup so
        # the line surfaces ONCE in the algedonic block even as the
        # poller fires it on every tick.
        cid = ev.get("commitment_id") or "?"
        text = (ev.get("text") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        n = ev.get("snooze_count", "?")
        threshold = ev.get("threshold", "?")
        return (
            f"commitment {cid} snoozed {n}× (threshold {threshold}): "
            f"{text} — consider committing or dismissing rather than "
            f"snoozing again."
        )
    if rule_kind == "react":
        emoji = ev.get("emoji") or "?"
        author = ev.get("author") or "?"
        target_age = ev.get("target_age_minutes")
        age_suffix = ""
        if isinstance(target_age, (int, float)):
            if target_age < 1:
                age_suffix = " on just-sent message"
            elif target_age < 60:
                age_suffix = f" on message from {int(target_age)}m ago"
            elif target_age < 1440:
                age_suffix = f" on message from {int(target_age / 60)}h ago"
            else:
                age_suffix = f" on message from {int(target_age / 1440)}d ago"
        return f'react("{emoji}") from {author}{age_suffix}'
    if rule_kind == "error":
        # Generic error event; surface .where + .error if present.
        # Collapse whitespace so multi-line tracebacks don't break the
        # markdown bullet structure of the Recent feedback signals
        # block.
        where = ev.get("where") or ev.get("source") or "?"
        msg = ev.get("error") or ev.get("message") or "(no detail)"
        msg = " ".join(str(msg).split())
        return f"error in {where}: {msg}"
    return rule_kind


def _render_turn_error(rec: dict) -> str:
    err = rec.get("error") or "(no detail)"
    return f"turn error: {err}"


@dataclass
class FeedbackLog:
    """Tails events.jsonl + turns.jsonl, surfaces recent feedback signals.

    No persistent state of its own — every call to ``recent`` re-reads
    the tail of both files (or a cached snapshot, if injected). Files
    may not exist (fresh home, never logged) — handled gracefully.

    ``events_snapshot`` / ``turns_snapshot`` (CR#10): when provided by
    the constructing Agent, ``recent`` iterates the cached snapshot
    instead of streaming the file each call. Falls back to direct tail
    when None for back-compat with tests / direct-call paths.
    """

    events_path: Path
    turns_path: Path
    # Per-call defaults; overridable on the .recent / .recent_block calls.
    default_window_hours: int = 24
    default_limit_per_polarity: int = 5
    events_snapshot: JsonlSnapshot | None = None
    turns_snapshot: JsonlSnapshot | None = None

    def recent(
        self,
        *,
        window_hours: int | None = None,
        limit_per_polarity: int | None = None,
    ) -> tuple[list[FeedbackSignal], list[FeedbackSignal]]:
        """Return (negative, positive), each reverse-chronological and
        capped at ``limit_per_polarity``. Records older than
        ``window_hours`` are dropped."""
        window_hours = window_hours if window_hours is not None else self.default_window_hours
        limit = (
            limit_per_polarity
            if limit_per_polarity is not None
            else self.default_limit_per_polarity
        )

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=window_hours)
        cutoff_iso = cutoff.isoformat()

        negatives: list[FeedbackSignal] = []
        positives: list[FeedbackSignal] = []
        # Kinds whose first occurrence (most recent, since we walk
        # tail-first) we've already added — subsequent occurrences are
        # skipped. Stops weekly cron events from re-appearing on every
        # heartbeat for 24h.
        seen_first_only: set[str] = set()
        # Per-polarity content-level dedup: if two distinct events
        # would render to the same line text within the same polarity
        # bucket, keep only the first (= most recent under tail-first
        # iteration) and skip subsequent identical content. Catches
        # the "tool_denied Read: path_outside_home × 3" case where
        # kind-level dedup would over-collapse (Read and Write share
        # rule_kind ``tool_denied`` but different rendered content);
        # also handles any future event family where the (kind, args)
        # tuple naturally produces colliding lines. Distinct payloads
        # under the same kind (e.g. spawn job_ids, tool_denied with
        # different reasons) keep their separate lines.
        seen_content: dict[str, set[str]] = {"negative": set(), "positive": set()}

        # 1) events.jsonl — known event-type rules.
        for ev in iter_snapshot_or_tail(self.events_snapshot, self.events_path):
            ts = ev.get("timestamp")
            if not isinstance(ts, str) or ts < cutoff_iso:
                # tail-first scan: as soon as a record predates the window
                # we can stop; jsonl is appended in chronological order.
                if isinstance(ts, str):
                    break
                continue
            evtype = ev.get("type")
            rule = _EVENT_RULES.get(evtype) if isinstance(evtype, str) else None
            if rule is None:
                continue
            polarity, kind = rule
            # react_received carries its own polarity (positive/negative/
            # neutral) classified per-emoji at bridge level. Use that
            # over the default-positive rule mapping. ``"neutral"`` skips
            # the event from both polarity buckets — surfaced via the
            # generic line below if there's render space, otherwise
            # silently dropped (it's informational, not pain/pleasure).
            if evtype == "react_received":
                ev_polarity = ev.get("polarity")
                if ev_polarity in ("positive", "negative"):
                    polarity = ev_polarity
                elif ev_polarity == "neutral":
                    continue
            # First-occurrence-only kinds: skip duplicates. Tail-first
            # iteration means we keep the most recent.
            if kind in _FIRST_OCCURRENCE_ONLY_KINDS:
                if kind in seen_first_only:
                    continue
                seen_first_only.add(kind)
            target = negatives if polarity == "negative" else positives
            if len(target) >= limit:
                continue
            content = _render_event_line(kind, ev)
            # Content-level dedup: if a prior (more recent) event
            # already produced this exact line text in this polarity
            # bucket, skip — see ``seen_content`` comment above for
            # rationale. Render cost is low and we're in tail-first
            # iteration, so the *kept* item is always the most recent.
            polarity_bucket = seen_content[polarity]
            if content in polarity_bucket:
                continue
            polarity_bucket.add(content)
            target.append(
                FeedbackSignal(
                    ts=ts,
                    polarity=polarity,
                    kind=kind,
                    channel_id=ev.get("channel_id"),
                    content=content,
                )
            )
            # Early exit when both sides full.
            if len(negatives) >= limit and len(positives) >= limit:
                break

        # 2) turns.jsonl — error / result_is_error are turn-level negatives
        # the events stream might not capture.
        if len(negatives) < limit:
            for rec in iter_snapshot_or_tail(self.turns_snapshot, self.turns_path):
                ts = rec.get("ts")
                if not isinstance(ts, str) or ts < cutoff_iso:
                    if isinstance(ts, str):
                        break
                    continue
                has_error = rec.get("error") or rec.get("result_is_error")
                if not has_error:
                    continue
                if len(negatives) >= limit:
                    break
                content = _render_turn_error(rec)
                # Same content-level dedup as the events loop above:
                # skip turn-error signals whose rendered line text
                # already appeared in the negative bucket.
                if content in seen_content["negative"]:
                    continue
                seen_content["negative"].add(content)
                negatives.append(
                    FeedbackSignal(
                        ts=ts,
                        polarity="negative",
                        kind="turn_error",
                        channel_id=rec.get("channel_id"),
                        content=content,
                    )
                )

        return negatives, positives

    def recent_block(
        self,
        *,
        window_hours: int | None = None,
        limit_per_polarity: int | None = None,
    ) -> str | None:
        """Returns the rendered block (without the leading ``## `` header
        — that's added by ``build_turn_prompt``), or ``None`` if both
        polarities are empty (skip the section to avoid empty headers
        in the prompt)."""
        negatives, positives = self.recent(
            window_hours=window_hours,
            limit_per_polarity=limit_per_polarity,
        )
        return render_feedback_block(
            negatives,
            positives,
            window_hours=window_hours
            if window_hours is not None
            else self.default_window_hours,
        )


# VSM: algedonic — bypass channel for self-feedback signals; surfaces
#                  recent error / denial / loop / saga_feedback / react
#                  events directly into the next turn's prompt without
#                  the embed-and-retrieve detour.
# loop_id: 2.1
def render_feedback_block(
    negatives: list[FeedbackSignal],
    positives: list[FeedbackSignal],
    *,
    window_hours: int = 24,
) -> str | None:
    """Markdown body for the Recent feedback signals section. Returns
    ``None`` when both lists are empty so the caller can skip rendering
    the section header entirely."""
    if not negatives and not positives:
        return None
    parts: list[str] = []
    if negatives:
        parts.append(f"Negative (last {window_hours}h):")
        parts.extend(_format_lines(negatives))
    if positives:
        if parts:
            parts.append("")  # blank line between subsections
        parts.append(f"Positive (last {window_hours}h):")
        parts.extend(_format_lines(positives))
    return "\n".join(parts)


def _format_lines(signals: list[FeedbackSignal]) -> list[str]:
    out: list[str] = []
    for sig in signals:
        # YYYY-MM-DDTHH:MM:SS+00:00 → "YYYY-MM-DD HH:MM" for compactness.
        ts = _short_ts(sig.ts)
        ch = f" [{sig.channel_id}]" if sig.channel_id else ""
        out.append(f"- {ts} — {sig.content}{ch}")
    return out


def _short_ts(ts: str) -> str:
    # Tolerate non-ISO inputs; just truncate to 16 chars so we get
    # "YYYY-MM-DD HH:MM" out of "YYYY-MM-DDTHH:MM:SS+00:00".
    cleaned = ts.replace("T", " ")
    return cleaned[:16] if len(cleaned) >= 16 else cleaned


# Backwards-compatible alias for the streaming tail reader. Older code
# in this module called ``_iter_jsonl_reverse``; new code should import
# ``tail_jsonl_records`` directly.
_iter_jsonl_reverse = tail_jsonl_records


def pending_forget_candidates_count(
    events_path: Path, *,
    snapshot: JsonlSnapshot | None = None,
) -> int | None:
    """Return the count from the most recent ``saga_decay_ok`` event,
    iff that event flagged >0 candidates AND no ``saga_forget_ok``
    event has occurred since. Otherwise None — meaning the persistent
    "forget candidates pending" line should not render.

    The clear rule is intentionally binary on event presence, not on
    counts: a saga_forget_ok event newer than the latest saga_decay_ok
    clears the block regardless of how many atoms were actually
    forgotten or how many fresh candidates exist. The next decay run
    re-establishes the count if it's still non-zero. Per-call count
    arithmetic is fragile (forget can be partial, ranges can shift)
    so we don't try.

    ``snapshot`` (CR#10) — iterate the cached snapshot when provided.
    """
    latest_decay_ts: str | None = None
    latest_decay_count: int | None = None
    latest_forget_ts: str | None = None
    for ev in iter_snapshot_or_tail(snapshot, events_path):
        evtype = ev.get("type")
        ts = ev.get("timestamp")
        if not isinstance(ts, str):
            continue
        if evtype == "saga_decay_ok" and latest_decay_ts is None:
            latest_decay_ts = ts
            result = ev.get("result") or {}
            if isinstance(result, dict):
                cands = result.get("forgetting_candidates")
                if isinstance(cands, (int, float)):
                    latest_decay_count = int(cands)
        elif evtype == "saga_forget_ok" and latest_forget_ts is None:
            latest_forget_ts = ts
        if latest_decay_ts is not None and latest_forget_ts is not None:
            break

    if latest_decay_count is None or latest_decay_count <= 0:
        return None
    if latest_forget_ts is not None and latest_forget_ts > latest_decay_ts:
        return None
    return latest_decay_count
