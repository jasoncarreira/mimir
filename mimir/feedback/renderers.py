"""Event-line renderers for the algedonic-surfacing block.

Contains _sanitize_field, _render_event_line (all event kinds), and
_render_turn_error.  Purely functional — takes dicts, returns strings.
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt-injection hardening helper
# ---------------------------------------------------------------------------

_FIELD_MAX_LEN = 240  # per-field cap for sanitized event-payload strings
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_field(value: object, max_len: int = _FIELD_MAX_LEN) -> str:
    """Collapse whitespace, strip control chars, cap length.

    Applied to every event-payload field that surfaces in the rendered
    algedonic prompt block.  Prevents prompt-injection via external event
    sources: GitHub PR author names, poller error strings, bridge errors,
    shell intent prefixes, Discord/Slack display names.

    Operations (in order):
      1. Coerce to str.
      2. Collapse all whitespace runs (including \\n, \\r, \\t) → single space.
      3. Strip remaining C0 + DEL control characters (\\x00–\\x1f, \\x7f).
      4. Truncate to *max_len* chars with "…" suffix on overflow.
    """
    s = " ".join(str(value).split())  # step 1+2: coerce + collapse whitespace
    s = _CONTROL_CHARS_RE.sub("", s)  # step 3: strip residual controls
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"  # step 4: cap
    return s


# Render hooks: per-kind one-liner builders. Defaults to a generic
# "<kind>: <event-type-specific note>" if no specialized renderer fits.
def _render_event_line(rule_kind: str, ev: dict) -> str:
    if rule_kind == "tool_denied":
        tool = ev.get("tool") or ev.get("name") or "?"
        reason = _sanitize_field(ev.get("reason") or ev.get("error") or "")
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
    if rule_kind == "cross_turn_loop":
        # S2-2: cross-turn send flood — same content sent 3+ times in 24h.
        cid = ev.get("channel_id") or "?"
        count = ev.get("count", "?")
        return (
            f"cross-turn send loop: same message sent {count}× to {cid!r} in 24h — "
            f"check for repeated heartbeat alerts or autonomous send loops"
        )
    if rule_kind == "saga_query_error":
        return f"SAGA query failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "saga_feedback_error":
        return f"SAGA feedback failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "saga_consolidate_error":
        return f"SAGA consolidation failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "saga_decay_error":
        return f"SAGA decay failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "auto_dispatch_failed":
        bridge = ev.get("bridge") or "?"
        ch = ev.get("channel_id") or "?"
        err = _sanitize_field(ev.get("error") or "(no detail)")
        return (
            f"auto-dispatch reply failed via {bridge} on {ch}: {err}. "
            f"Your text was generated but not delivered. "
            f"Consider calling send_message explicitly next time."
        )
    if rule_kind == "saga_forget_error":
        dry = ev.get("dry_run")
        suffix = " (dry_run)" if dry else ""
        return f"SAGA forget failed{suffix}: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "synth_dispatch_fail":
        return f"SAGA synthesis dispatch failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
    if rule_kind == "synth_empty_window":
        return (
            f"SAGA synthesis ran with empty turn window "
            f"(session={ev.get('saga_session_id') or '?'}); "
            f"{_sanitize_field(ev.get('reason') or 'no detail')}"
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
            f"introspection report failed: {_sanitize_field(ev.get('error') or '(no detail)')}"
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
        err = _sanitize_field(ev.get("error") or "(no detail)")
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
        window = ev.get("window_type", "5h")
        confirm_count = ev.get("confirm_count")
        confirm_threshold = ev.get("confirm_threshold")
        confirm_str = (
            f" ({confirm_count}/{confirm_threshold} consecutive)"
            if isinstance(confirm_count, int) and isinstance(confirm_threshold, int)
            else ""
        )
        return (
            f"quota reading anomaly: {window} endpoint reported {rejected_str} "
            f"(kept previous {kept_str}); arbiter consults the last "
            f"trusted value. Glitch is transient — usually clears within "
            f"a poll cycle or two.{confirm_str}"
        )
    if rule_kind == "quota_anomaly_confirmed":
        # chainlink #231: after N consecutive anomalous 7d readings, the
        # detector accepted the new value as the real state.
        confirmed = ev.get("confirmed_utilization")
        consecutive = ev.get("consecutive_count")
        confirmed_str = (
            f"{confirmed * 100:.0f}%" if isinstance(confirmed, (int, float)) else "?"
        )
        n_str = str(consecutive) if isinstance(consecutive, int) else "?"
        return (
            f"quota reading anomaly confirmed: 7d endpoint reported "
            f"{confirmed_str} for {n_str} consecutive polls — accepted as "
            f"real (glitch hypothesis implausible); prior stuck-low value "
            f"replaced."
        )
    if rule_kind == "git_commit_failed":
        stage = ev.get("stage") or "?"
        err = _sanitize_field(ev.get("error") or "(no detail)")
        return (
            f"git commit failed at {stage}: {err}. "
            f"Tracked-state changes from this turn are NOT staged. "
            f"Investigate /mimir-home git status; next successful turn re-tries."
        )
    if rule_kind == "git_push_failed":
        reason = _sanitize_field(ev.get("reason") or "(no detail)")
        rc = ev.get("returncode")
        rc_suffix = f" [rc={rc}]" if rc is not None else ""
        return (
            f"git push to mimirbot-state failed: {reason}{rc_suffix}. "
            f"Local commits stand; the next debounced push catches up "
            f"once connectivity / auth is restored."
        )
    if rule_kind == "git_pull_blocked":
        reason = _sanitize_field(ev.get("reason") or "(no detail)")
        return (
            f"git pull blocked: {reason}. Container's local branch has "
            f"diverged from remote — operator must reconcile manually "
            f"(reset local to remote, or push divergent commits)."
        )
    if rule_kind == "shell_job_complete_enqueue_failed":
        job_id = ev.get("job_id") or "?"
        err = _sanitize_field(ev.get("error") or "(no detail)")
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
        err = _sanitize_field(ev.get("error") or "(no detail)")
        backoff_str = f" (next retry in {backoff}s)" if backoff else ""
        return (
            f"Discord bridge supervisor: {attempt} consecutive retry "
            f"attempts on connect — {err}{backoff_str}. Most likely "
            f"Discord-side gateway / API outage; the supervisor is "
            f"still retrying with exponential backoff. Inbound + "
            f"outbound Discord traffic is paused until reconnect."
        )
    if rule_kind == "discord_bridge_login_failure":
        err = _sanitize_field(ev.get("error") or "(no detail)")
        return (
            f"Discord bridge: token auth permanently rejected ({err}). "
            f"Operator must rotate the bot token in `.env` "
            f"(DISCORD_TOKEN) and restart the container — the bridge "
            f"supervisor has stopped retrying and Discord traffic is "
            f"down for the rest of this container's lifetime."
        )
    if rule_kind == "discord_bridge_intents_failure":
        err = _sanitize_field(ev.get("error") or "(no detail)")
        return (
            f"Discord bridge: privileged intents required ({err}). "
            f"Operator must enable members + message_content intents "
            f"in the Discord developer portal for this bot, then "
            f"restart the container."
        )
    if rule_kind == "slack_bridge_retry":
        attempt = ev.get("attempt", "?")
        backoff = ev.get("backoff_seconds")
        slack_err = _sanitize_field(ev.get("slack_error") or "")
        err = _sanitize_field(ev.get("error") or "(no detail)")
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
        slack_err = _sanitize_field(ev.get("slack_error") or "(unknown)")
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
        terminal = _sanitize_field(ev.get("terminal_reason") or "?")
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
        text = _sanitize_field(ev.get("text") or "", max_len=80)
        channel = ev.get("channel_id")
        # recipient_identity is LLM-extracted (like ``text`` above), so it must
        # be sanitized too — otherwise a newline / markup in it can break the
        # algedonic block's formatting or inject content (chainlink #312).
        recipient = _sanitize_field(ev.get("recipient_identity") or "", max_len=40)
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
        text = _sanitize_field(ev.get("text") or "", max_len=80)
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
        text = _sanitize_field(ev.get("text") or "", max_len=80)
        n = ev.get("snooze_count", "?")
        threshold = ev.get("threshold", "?")
        return (
            f"commitment {cid} snoozed {n}× (threshold {threshold}): "
            f"{text} — consider committing or dismissing rather than "
            f"snoozing again."
        )
    if rule_kind == "react":
        emoji = _sanitize_field(ev.get("emoji") or "?")
        author = _sanitize_field(ev.get("author") or "?")
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
        # _sanitize_field collapses whitespace (including newlines from
        # tracebacks) and strips control characters — chainlink #224.
        where = ev.get("where") or ev.get("source") or "?"
        msg = _sanitize_field(ev.get("error") or ev.get("message") or "(no detail)")
        return f"error in {where}: {msg}"
    if rule_kind == "algedonic_escalation":
        kind = ev.get("kind") or "?"
        count = ev.get("count")
        threshold = ev.get("threshold")
        count_str = str(count) if isinstance(count, int) else "?"
        thr_str = str(threshold) if isinstance(threshold, int) else "?"
        return (
            f"algedonic escalation: {kind} crossed threshold "
            f"({count_str}× ≥ {thr_str}× in 24h)"
        )
    if rule_kind == "proposed_changes_backlog":
        # Daily cron: surfaces pending count + oldest-age for the agent
        # to see between reflections. Either threshold can trigger; show
        # whichever values are present.
        count = ev.get("pending_count")
        oldest = ev.get("oldest_age_days")
        parts = []
        if isinstance(count, int):
            parts.append(f"{count} pending")
        if isinstance(oldest, int):
            parts.append(f"oldest {oldest}d old")
        detail = ", ".join(parts) if parts else "(no detail)"
        return f"proposed-changes backlog: {detail}"
    if rule_kind == "proposed_changes_backlog_error":
        msg = _sanitize_field(ev.get("error") or "(no detail)")
        return f"proposed-changes backlog check failed: {msg}"
    if rule_kind == "mimir_update_available":
        current = ev.get("current") or "?"
        latest = ev.get("latest") or "?"
        return (
            f"mimir update available: {current} → {latest} "
            f"(operator approves → I call request_mimir_update → "
            f"`docker compose restart` installs on boot)"
        )
    if rule_kind == "mimir_update_check_error":
        msg = _sanitize_field(ev.get("error") or "(no detail)")
        return f"mimir update-check failed: {msg}"
    if rule_kind == "mimir_update_applied":
        spec = ev.get("spec") or "?"
        return f"mimir update applied on restart: {spec}"
    if rule_kind == "mimir_update_digest":
        prior = ev.get("prior_version") or "?"
        new = ev.get("new_version") or "?"
        sched = ev.get("scheduler_delta") or []
        drift = ev.get("skills_drift") or []
        gaps = ev.get("env_gaps") or []
        parts: list[str] = []
        if sched:
            names = ", ".join(_sanitize_field(str(n)) for n in sched)
            parts.append(f"scheduler +{len(sched)} tick(s): {names}")
        if drift:
            names = ", ".join(_sanitize_field(str(n)) for n in drift)
            parts.append(f"skills drifted: {names}")
        if gaps:
            pairs = ", ".join(
                f"{_sanitize_field(str(g[0]))}/{_sanitize_field(str(g[1]))}"
                if isinstance(g, (list, tuple)) and len(g) >= 2
                else _sanitize_field(str(g))
                for g in gaps
            )
            parts.append(f"env missing: {pairs}")
        detail = "; ".join(parts) if parts else "nothing requires action"
        return f"[mimir v{prior}→{new}] {detail}"
    if rule_kind == "mimir_update_failed":
        spec = ev.get("spec") or "?"
        rc = ev.get("rc")
        raw_err = ev.get("error") or ev.get("stderr_tail") or ""
        err = _sanitize_field(raw_err, max_len=120) if raw_err else ""
        tail = f" ({err})" if err else ""
        rc_part = f" rc={rc}" if rc is not None else ""
        return (
            f"mimir update FAILED for {spec}{rc_part}{tail} — "
            f"running on prior version, flag cleared, operator can "
            f"re-approve via request_mimir_update once cause is known"
        )
    if rule_kind == "poller_circuit_tripped":
        # chainlink #196: include poller name so operator knows which poller
        # tripped; omit remaining_seconds (not present on the tripped event).
        name = ev.get("poller") or "?"
        failures = ev.get("consecutive_failures") or "?"
        return (
            f"poller circuit tripped: {name} ({failures} consecutive failures"
            f" — backing off {ev.get('backoff_seconds', 300)}s)"
        )
    if rule_kind == "poller_circuit_open":
        # chainlink #196: include poller name but NOT remaining_seconds.
        # Omitting remaining_seconds keeps the content string stable across
        # all suppressed-run events for the same poller, so content-level
        # dedup in build_feedback_block() collapses N same-poller events
        # into one entry.  The ×N count still shows total fires in the window.
        name = ev.get("poller") or "?"
        failures = ev.get("consecutive_failures") or "?"
        return (
            f"poller circuit-breaker open: {name} ({failures} consecutive"
            f" failures — runs suppressed)"
        )
    if rule_kind == "bash_async_refused":
        # chainlink #193: wait-on-pending guard refused a respawn.
        # Include running job id + intent prefix for operator audit
        # without requiring reading turn transcripts.
        running_job_id = ev.get("running_job_id") or "?"
        intent = _sanitize_field(ev.get("intent_prefix") or "?")
        channel = ev.get("channel_id") or "?"
        return (
            f"bash_async refused same-intent respawn on {channel}: "
            f"running job {running_job_id!r} — intent={intent!r}"
        )
    if rule_kind == "skill_frontmatter_error":
        # chainlink #201: SKILL.md frontmatter failed to parse — the skill
        # is silently omitted from the catalog until the SKILL.md is fixed.
        name = ev.get("skill_name") or "?"
        error = _sanitize_field(ev.get("error") or "(no detail)")
        return (
            f"skill SKILL.md malformed: {name!r} — {error} "
            f"(skill omitted from catalog until fixed)"
        )
    if rule_kind == "poller_missing_required_env":
        # chainlink #108: env_required check failed — one or more declared
        # required env vars are missing from the assembled subprocess env.
        # The poller run was skipped for this tick.
        name = ev.get("poller") or "?"
        missing = ev.get("missing") or []
        if isinstance(missing, list):
            missing_str = ", ".join(
                _sanitize_field(m) for m in missing
            ) if missing else "?"
        else:
            missing_str = _sanitize_field(missing)
        return (
            f"poller {name!r} skipped — missing required env: {missing_str} "
            f"(add to pass_env + provision the var)"
        )
    if rule_kind == "pr_merge_blocked":
        # chainlink #214: pre-merge CHANGES_REQUESTED gate refused the merge.
        # Include PR number + blocking reviewer(s) so the operator can see
        # which review is blocking without reading turn transcripts.
        # Author names come from GitHub API responses and must be sanitized
        # before surfacing in the prompt — chainlink #224.
        pr_num = ev.get("pr") or ev.get("pr_number") or "?"
        blocking = ev.get("blocking_reviewers") or ev.get("blocking") or []
        if isinstance(blocking, list) and blocking:
            authors = ", ".join(
                _sanitize_field(b.get("author", "?") if isinstance(b, dict) else str(b))
                for b in blocking
            )
            return (
                f"pr_merge blocked: PR #{pr_num} has CHANGES_REQUESTED "
                f"from {authors} — resolve before merge"
            )
        return f"pr_merge blocked: PR #{pr_num} has CHANGES_REQUESTED review"
    if rule_kind == "gave_up":
        # A poller abandoned a retried action after exhausting its budget
        # (chainlink #299). The concrete event type names what was given up
        # on (``pr_review_request_gave_up`` → "pr review request"); the event
        # may carry ``attempts`` and a target (``repo``+``number`` / ``url`` /
        # ``detail``). Render a one-liner for the agent's negative algedonic
        # block, sanitizing any GitHub-sourced target (chainlink #224) and
        # degrading gracefully when fields are absent — emitters vary by poller.
        et = ev.get("type")
        what = "a retried action"
        if isinstance(et, str) and et.endswith("_gave_up"):
            stripped = et[: -len("_gave_up")].replace("_", " ").strip()
            if stripped:
                what = stripped
        attempts = ev.get("attempts")
        suffix = f" after {attempts} attempts" if attempts else ""
        repo, num = ev.get("repo"), ev.get("number")
        raw_target = (
            f"{repo}#{num}" if repo and num
            else (ev.get("url") or ev.get("detail") or "")
        )
        line = f"poller gave up on {what}{suffix}"
        if raw_target:
            return f"{line} — {_sanitize_field(str(raw_target))}"
        return line
    return rule_kind


def _render_turn_error(rec: dict) -> str:
    err = _sanitize_field(rec.get("error") or "(no detail)")
    return f"turn error: {err}"


# ---------------------------------------------------------------------------
# S2-2: cross-turn send_message loop detection
#
# The within-turn LoopDetector (loop_detector.py) catches near-duplicate
# sends inside a single run_turn.  This function is the cross-turn analog:
# it scans events.jsonl for ``send_message_sent`` events in the 24h window
# and detects (channel_id × content_hash) pairs that appear 3+ times.
#
# A heartbeat that keeps sending the same alert every tick — flooding the
# operator channel — is the canonical trigger.  The detection fires once per
# 24h window per (channel_id, content_hash) pair (dedup via prior
# ``cross_turn_send_duplicate`` events), so a sustained loop surfaces as a
# single persistent algedonic negative rather than a new entry every tick.
#
# Beer framing: S2 cross-job variety-absorber operating across turn
