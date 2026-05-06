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
# ignored. ``react_received`` is plumbed but not currently emitted by
# the bridges (FUTURE: wire reaction handlers in slack/discord — see
# V0.4.md §2 open question).
_EVENT_RULES: dict[str, tuple[Polarity, str]] = {
    "error": ("negative", "error"),
    "tool_call_denied": ("negative", "tool_denied"),
    "tool_call_budget_warning": ("negative", "tool_budget"),
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
}


# Kinds where only the most recent occurrence in the window should
# render. Cron-fired events (weekly maintenance) re-appear on every
# heartbeat in the 24h algedonic window, otherwise — same line × 24
# would crowd out genuinely-new positive signals like react_received
# or saga_feedback_sent. Tail-first iteration means the first-seen
# event for a kind in this set IS the most recent.
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
    # PR 4a (git_tracking): a stuck network outage / auth issue / dirty
    # tree will re-emit on every turn until resolved. Dedup so the
    # operator-visible signal is the *most recent* failure, not 50
    # copies of the same error in the 24h window.
    "git_commit_failed",
    "git_push_failed",
    "git_pull_blocked",
}


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
    if rule_kind == "unknown_channel":
        return f"send_message to unknown channel {ev.get('channel_id', '?')}"
    if rule_kind == "saga_feedback":
        n = ev.get("n_atoms")
        return f"saga_feedback_sent ({n} atoms credited)"
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
    the tail of both files. Files may not exist (fresh home, never
    logged) — handled gracefully.
    """

    events_path: Path
    turns_path: Path
    # Per-call defaults; overridable on the .recent / .recent_block calls.
    default_window_hours: int = 24
    default_limit_per_polarity: int = 5

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

        # 1) events.jsonl — known event-type rules.
        for ev in _iter_jsonl_reverse(self.events_path):
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
            target.append(
                FeedbackSignal(
                    ts=ts,
                    polarity=polarity,
                    kind=kind,
                    channel_id=ev.get("channel_id"),
                    content=_render_event_line(kind, ev),
                )
            )
            # Early exit when both sides full.
            if len(negatives) >= limit and len(positives) >= limit:
                break

        # 2) turns.jsonl — error / result_is_error are turn-level negatives
        # the events stream might not capture.
        if len(negatives) < limit:
            for rec in _iter_jsonl_reverse(self.turns_path):
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
                negatives.append(
                    FeedbackSignal(
                        ts=ts,
                        polarity="negative",
                        kind="turn_error",
                        channel_id=rec.get("channel_id"),
                        content=_render_turn_error(rec),
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


def pending_forget_candidates_count(events_path: Path) -> int | None:
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
    """
    latest_decay_ts: str | None = None
    latest_decay_count: int | None = None
    latest_forget_ts: str | None = None
    for ev in tail_jsonl_records(events_path):
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
