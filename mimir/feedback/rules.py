"""Event-rule registry and escalation helpers.

Contains _EVENT_RULES, _VALENCE_GROUPS, arousal + escalation thresholds,
and the helpers that count/detect/emit escalation events.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Literal

from ._models import Polarity, ValenceGroup
from .._jsonl_tail import tail_jsonl_records
from ..jsonl_snapshot import JsonlSnapshot, iter_window_records

log = logging.getLogger(__name__)


# Event-type → (polarity, short-tag) mapping. Anything not listed is
# ignored.
_EVENT_RULES: dict[str, tuple[Polarity, str]] = {
    "error": ("negative", "error"),
    # Change-proposal PR opened by the agent (chainlink #337/#339/#344).
    # Positive: it supersedes the "open proposal" nudge the prompt renders while
    # a proposal is in flight (the nudge auto-clears once the worktree is gone).
    "proposal_pr_opened": ("positive", "proposal_pr_opened"),
    "proposal_branch_cleaned": ("positive", "proposal_branch_cleaned"),
    "proposal_cleanup_failed": ("negative", "proposal_cleanup_failed"),
    # chainlink #353: a prose note under a tracked root (memory/state/prompts)
    # is git-ignored, so the write was silently dropped and won't persist.
    "git_ignored_note_skipped": ("negative", "ignored_write"),
    "tool_call_denied": ("negative", "tool_denied"),
    "tool_error": ("negative", "tool_error"),
    "background_task_failed": ("negative", "background_task_failed"),
    "scheduler_loop_lag": ("negative", "scheduler_loop_lag"),
    # chainlink #682: ``scheduler_loop_lag_host`` (the loop was woken late while
    # idle/descheduled — a VM/host scheduling hiccup, not a mimir hot path) is
    # deliberately ABSENT here. classify() returns None for it, so every feedback
    # read-path skips it: host hiccups stay queryable in the event log but don't
    # inflate the negative algedonic ×N count.
    "scheduler_loop_lag_monitor_failed": ("negative", "scheduler_loop_lag_monitor_failed"),
    "worklink_claimed": ("positive", "worklink_claimed"),
    "worklink_evidence": ("positive", "worklink_evidence"),
    "worklink_transition": ("positive", "worklink_transition"),
    "worklink_attempts_exhausted": ("negative", "worklink_attempts_exhausted"),
    "tool_call_budget_warning": ("negative", "tool_budget"),
    # Gap 4 fix: budget_gate.py emits these two names, not the legacy
    # "tool_call_budget_warning". All three are aliased to the same
    # short-tag so the algedonic block surfaces budget pressure
    # regardless of which path fired.
    "tool_call_budget_denied": ("negative", "tool_budget"),
    "tool_call_budget_soft_warning": ("negative", "tool_budget"),
    "prohibited_action_blocked": ("negative", "prohibited_blocked"),
    "send_message_loop_hard_stop": ("negative", "loop_stop"),
    "send_message_loop_warning": ("negative", "loop_warn"),
    # S2-2: cross-turn loop — same message sent 3+ times in 24h to the
    # same channel.  Surfaces when FeedbackLog._detect_cross_turn_send_loops
    # detects a flood that persisted across multiple turns.
    "cross_turn_send_duplicate": ("negative", "cross_turn_loop"),
    "saga_query_error": ("negative", "saga_query_error"),
    "saga_feedback_error": ("negative", "saga_feedback_error"),
    "saga_consolidate_error": ("negative", "saga_consolidate_error"),
    "saga_decay_error": ("negative", "saga_decay_error"),
    "saga_forget_error": ("negative", "saga_forget_error"),
    "saga_synthesis_dispatch_failed": ("negative", "synth_dispatch_fail"),
    "saga_synthesis_empty_window": ("negative", "synth_empty_window"),
    # CR#19: synthesis-turn post-check; agent ran the synthesis turn
    # but skipped step 3 (saga_end_session). Without the sessions row
    # the next session has no "what were we doing last time?" record.
    # Negative so the agent's next turn surfaces it and the behavior
    # self-corrects.
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
    # Priority-banded suppression: a poller fire was shed under
    # resource pressure (same homeostat gate as scheduled ticks, keyed
    # by the poller's declared priority). Negative for the same reason
    # tick_suppressed is — the agent should see its autonomous
    # feeds going quiet and why.
    "poller_fire_suppressed": ("negative", "poller_suppressed"),
    "heartbeat_health_degraded": ("negative", "heartbeat_health"),
    "introspection_report_error": ("negative", "introspection_error"),
    "predictions_pending_review": ("negative", "predictions_pending"),
    "send_message_unknown_channel": ("negative", "unknown_channel"),
    # 0.3.0: the bridge reported a soft delivery failure (SendResult.sent=False
    # — disconnected client, bad channel) on a send_message. With auto-dispatch
    # gone this is the sole reply path, so a non-delivery is operator-relevant.
    "send_message_failed": ("negative", "send_failed"),
    # chainlink #408: a <react> directive inside a send_message body
    # failed (bridge declined or raised). Previously a silent
    # except-pass — but the prompt tells the agent directive failures
    # surface in feedback, and a failed ack means the user saw nothing.
    "send_message_directive_failed": ("negative", "directive_failed"),
    # 0.3.0: interactive turn produced final text but never called the
    # send_message tool — the reply is stuck as reasoning and the user got
    # nothing. Surfaces in the next turn's feedback panel so the agent
    # self-corrects (and reflection can catch a recurring pattern).
    "interactive_turn_no_send_message": ("negative", "no_reply"),
    # Resend-nudge re-prompted the agent to deliver, and it STILL didn't call
    # send_message — the recovery failed, so the reply is lost. A stronger
    # negative than the plain no-send (the in-band correction didn't take).
    "resend_nudge_failed": ("negative", "resend_failed"),
    # Poller framework — health signals emitted by skill-side poller
    # subprocesses (via the ``"signal": "<event_type>"`` JSONL shape;
    # see ``mimir/pollers.py`` output contract). These surface
    # external-state breakage (OAuth token revoked, upstream service
    # outage, rate-limit cliff) algedonically without spawning a turn
    # per signal. ``poller_signal`` is the generic catch-all when the
    # poller doesn't have a more specific classification.
    "poller_oauth_expired":  ("negative", "poller_oauth_expired"),
    "poller_auth_failed":    ("negative", "poller_auth_failed"),
    "poller_service_outage": ("negative", "poller_service_outage"),
    "poller_rate_limited":   ("negative", "poller_rate_limited"),
    "poller_signal":         ("negative", "poller_signal"),
    # Framework-emitted: ``poller_nonzero_exit`` fires whenever a poller
    # subprocess exits non-zero. Independent of skill-emitted signals
    # (those can fire WITH a successful exit). Negative so the operator
    # sees recurring failures even when the poller didn't classify the
    # root cause itself.
    "poller_nonzero_exit":   ("negative", "poller_nonzero_exit"),
    # Circuit-breaker events (chainlink #94): emitted by the pollers
    # framework when consecutive failures trip the breaker (tripped) or
    # when a run is skipped because the circuit is still open (open).
    # ``tripped`` is the load-bearing signal — operator sees it once per
    # trip.  ``open`` repeats every suppressed fire but carries a
    # ``remaining_seconds`` countdown so the operator can see the backoff
    # window draining in events.jsonl.
    "poller_circuit_tripped": ("negative", "poller_circuit_tripped"),
    "poller_circuit_open":    ("negative", "poller_circuit_open"),
    # chainlink #95: ``poller.env`` contained a key whose name matches a
    # deny-list pattern (``*_API_KEY``, ``*_TOKEN``, etc.).  ``pass_env``
    # is the documented path for forwarding live secrets; ``poller.env``
    # is for static literal config values.  The operator likely put the
    # wrong thing in the ``env`` block — surface as a negative so the
    # algedonic block catches it without requiring a crash.
    "poller_env_secret_reintroduced": ("negative", "poller_env_secret_reintroduced"),
    # chainlink #229: hard-deny on process-control / loader env vars in
    # ``pass_env`` (LD_PRELOAD, PYTHONPATH, DYLD_*, etc.). Unlike the
    # named-secret event above which fires AND propagates the value, this
    # one fires AND BLOCKS — the var is not passed to the subprocess.
    # Operators see the block in the algedonic block so a manifest typo
    # or supply-chain regression doesn't go silent.
    "poller_env_process_control_blocked": ("negative", "poller_env_process_control_blocked"),
    # chainlink #419: a pollers.json entry's cron expression failed to
    # validate on install/reload. A previously-installed poller keeps
    # firing on its last-known-good cron (preserved, like the
    # invalid-manifest path); a fresh install is skipped entirely.
    # Negative so the operator sees the broken schedule algedonically
    # instead of discovering it via a poller that never updated (or
    # never started).
    "poller_reload_invalid_cron": ("negative", "poller_invalid_cron"),
    # chainlink #108: env_required validation — emitted when a poller's
    # declared required env vars are absent from the assembled subprocess
    # env.  The poller run is skipped entirely for that tick.  Negative so
    # the operator sees the configuration gap algedonically; the poller name
    # + list of missing vars lets them provision exactly what's needed.
    "poller_missing_required_env": ("negative", "poller_missing_required_env"),
    # github-poller review-missed-submission (Mimir's PR #234 / #235
    # post-mortem). Fires when a PR-review-needed event reached the
    # agent (``pr_opened`` / ``pr_synchronize``) but the turn ended
    # without ``gh pr review`` (or the MCP equivalent) — usually because
    # the /review skill loaded after reasoning had already committed
    # to "write prose, don't submit." Negative so the operator sees
    # the failure mode in introspection-report; no auto-re-fire (would
    # burn quota for ambiguous benefit).
    "poller_review_missed_submission": ("negative", "review_missed_submission"),
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
    # chainlink #231: after N consecutive anomalous readings, the detector
    # accepts the value. Surface as positive so the operator sees the
    # recovery (prior-stuck-low → accepted new reading).
    "quota_reading_anomaly_confirmed": ("positive", "quota_anomaly_confirmed"),
    "defaults_upgrade_checked": ("positive", "defaults upgrade check"),
    "defaults_upgrade_failed": ("negative", "defaults upgrade failed"),

    # PR 4a (git_tracking): post-turn commit/push failures. All three
    # are first-occurrence-only (see ``_FIRST_OCCURRENCE_ONLY_KINDS``)
    # because once git breaks every subsequent turn re-emits — without
    # dedup the failure crowds out everything else in the 24h window.
    "git_commit_failed": ("negative", "git_commit_failed"),
    "git_push_failed": ("negative", "git_push_failed"),
    "git_pull_blocked": ("negative", "git_pull_blocked"),
    "git_home_invariant_violation": ("negative", "git_home_invariant_violation"),
    # PR 56 (shell-jobs): the cross-thread bridge from waiter thread
    # back to the dispatcher raised. Means a finished async shell job
    # never woke the spawning channel — the operator sees a missing
    # wake-up before they'd notice without surfacing here.
    "shell_job_complete_enqueue_failed": ("negative", "shell_job_complete_enqueue_failed"),
    # chainlink #193: wait-on-pending guard refused a bash_async respawn
    # because a same-intent job was already running on this channel.
    # Negative so dashboards + introspection-report can surface "agent
    # attempted N respawns and was refused" as an observable pattern
    # without requiring reading turn transcripts.
    "bash_async_refused_same_intent": ("negative", "bash_async_refused"),
    # chainlink #201: SKILL.md frontmatter failed to parse — the skill is
    # silently omitted from the catalog.  Surfaces as a negative so the
    # operator sees recurring failures in the algedonic block rather than
    # noticing a missing skill by accident.
    "skill_frontmatter_error": ("negative", "skill_frontmatter_error"),
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
    "git_push_stale": ("negative", "git_push_stale"),
    # SPEC §8.3 / §16 item 16 — automated index-integrity check fires
    # daily against the file-corpus and SAGA SQLite databases. Detects
    # SQLite-level corruption, FTS5 self-check failures, FTS5 row-count
    # drift, and embedding-dim mixing (model-swap-without-rebuild).
    # Recovery procedure: §8.3 (rebuild_index / saga_calibration.re_embed).
    "index_integrity_ok": ("positive", "index_integrity_ok"),
    "index_integrity_failed": ("negative", "index_integrity_failed"),
    # SPEC §4.9 / §16 item 18 — mid-turn quota exhaustion: the model
    # call returned a 429 and the agent recorded a pause via
    # QuotaPauseTracker. The arbiter suppresses scheduled ticks while
    # paused. ``quota_recovered`` fires once on the lazy-expiry
    # transition (when ``now`` crosses the recorded reset time) so
    # the agent's feedback block can show "transient, recovered."
    "quota_exhausted": ("negative", "quota_exhausted"),
    "quota_recovered": ("positive", "quota_recovered"),
    # S1-3 (VSM eval): core memory block degradation — fewer-than-minimum
    # blocks loaded OR an individual block is below the stub-size threshold.
    # Silent identity loss is worse than loud identity loss: the agent
    # continues running on a stripped-down prompt that may be missing persona,
    # values, or filing rules. Negative so the algedonic block makes the
    # degradation visible every turn until the operator can restore the files.
    "core_prompt_degraded": ("negative", "core_prompt_degraded"),
    # A home file read into the system prompt (core block / memory index) has a
    # non-UTF-8 byte. read_text_lossy keeps the turn alive by replacement-decoding,
    # but the stray byte (mojibake / cp1252 paste / mid-write artifact) silently
    # degrades the prompt — surface it so the agent cleans the file. chainlink #470.
    "non_utf8_home_file": ("negative", "non_utf8_home_file"),
    # A real (non-synthetic) channel's injected memory exceeded the channel
    # prompt cap. The prompt still truncates with an inline note, but that note
    # is buried inside the context block and the truncation keeps the oldest
    # lexicographic content. Negative so the agent/operator trims or refiles
    # instead of silently running on stale channel context. chainlink #643.
    "channel_memory_over_cap": ("negative", "channel_memory_over_cap"),
    # SPEC §16 items follow-up from the 2026-05-23 VSM eval. The weekly
    # viability report (mimir/viability_metrics.py) emits one event per
    # threshold-crossing it detects. Each is a distinct collapse /
    # curation failure mode so they're individually surfaced rather
    # than rolled into a single ``viability_warning`` catch-all.
    "collapse_risk_output_self_similarity": ("negative", "collapse_output_sim"),
    "collapse_risk_atom_concentration": ("negative", "collapse_atom_gini"),
    "collapse_risk_topic_lock": ("negative", "collapse_topic_lock"),
    "curation_below_threshold_reflection": ("negative", "curation_reflection_low"),
    "curation_below_threshold_feedback": ("negative", "curation_feedback_low"),
    "curation_below_threshold_forget": ("negative", "curation_forget_low"),
    "viability_report_ok": ("positive", "viability_ok"),
    "viability_report_error": ("negative", "viability_error"),
    "applied_audit_ok": ("positive", "applied_audit_ok"),
    "applied_audit_error": ("negative", "applied_audit_error"),
    # Daily legacy proposed-changes backlog check
    # (mimir/reflection/proposed_changes_health.py). Surfaces legacy
    # operator-review backlog so the agent sees a between-reflection signal
    # that old HITL entries need migration/cleanup. ``_error`` is the
    # cron-callable's own failure path; the steady-state-healthy case emits
    # nothing (no positive event needed — silence IS the success signal).
    "proposed_changes_backlog": ("negative", "proposed_changes_backlog"),
    "proposed_changes_backlog_error": ("negative", "proposed_changes_backlog_error"),
    # PyPI version-check daily cron (mimir/version_check.py). Surfaces
    # newer mimir releases so the operator sees a "version X available"
    # signal in the agent's per-turn algedonic block and on /ops.
    # Positive polarity — new code is generally a good thing. The error
    # case (cron callable raised) is negative, but the routine "PyPI is
    # currently unreachable" / "package not yet published" cases are
    # silent (the check itself returns no signal — no event emitted).
    "mimir_update_available": ("positive", "mimir_update_available"),
    "mimir_update_check_error": ("negative", "mimir_update_check_error"),
    # Pending-update flag lifecycle (see mimir/update_on_start.py).
    # ``mimir_update_starting`` is a neutral diagnostic — it only
    # appears in the event log, not the feedback block, because the
    # operator already knows they approved the install. ``applied``
    # is positive (the upgrade succeeded). ``failed`` is negative
    # (rolled back to the prior version; operator should investigate).
    "mimir_update_applied": ("positive", "mimir_update_applied"),
    "mimir_update_failed": ("negative", "mimir_update_failed"),
    # Post-update deployment digest — surfaces scheduler delta, skills drift,
    # and env gaps discovered during apply_pending_update, on the first turn
    # after the restart. Positive polarity: the update succeeded and the
    # operator needs to see the actionable diff (new ticks to add, skills to
    # refresh, env vars to provision). If all diffs are empty: "nothing requires
    # action" — still positive (update was clean).
    "mimir_update_digest": ("positive", "mimir_update_digest"),
    "skills_auto_update": ("positive", "skills_auto_update"),
    "skills_auto_update_failed": ("negative", "skills_auto_update_failed"),
    # chainlink #214: pre-merge CHANGES_REQUESTED gate blocked an auto-merge.
    # Surfaces when the agent's pre-merge review-state check finds any
    # reviewer's current state is CHANGES_REQUESTED — the merge was refused
    # and the PR stays open until the reviewer re-approves or withdraws the
    # request.  Negative so the operator sees "I wanted to merge PR N but
    # couldn't" without having to read turn transcripts.
    "pr_merge_blocked_by_changes_requested": ("negative", "pr_merge_blocked"),
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
    # Alg-3 (VSM eval): auto-escalation of sustained negative patterns.
    # Emitted by ``_emit_new_escalations`` inside FeedbackLog.recent() when a
    # negative kind crosses its ``_ESCALATION_THRESHOLDS`` entry and has not
    # already been escalated within the current 24h window.  Rendering via
    # ``_render_event_line`` surfaces "which kind / how many / threshold."
    "algedonic_escalation": ("negative", "algedonic_escalation"),
}


def classify(evtype: object) -> tuple[Polarity, str] | None:
    """Map an event type to ``(polarity, short-tag)``, or ``None`` if unrecognized.

    Two-tier lookup:

    1. Exact match in :data:`_EVENT_RULES` (the canonical table).
    2. Suffix convention — any event type ending in ``_gave_up`` is a
       NEGATIVE signal whose rule-kind is the concrete event type.

    Tier 2 lets a poller that exhausts its retry budget surface in the
    agent's algedonic block without a per-poller rule. github-poller's
    ``pr_review_request_gave_up`` (emitted when a re-review nudge is
    abandoned after N attempts) and any future poller's ``<thing>_gave_up``
    both classify as negative automatically. (chainlink #299)

    All three feedback read-paths — algedonic ``recent()``, Alg-2 run
    detection, Alg-3 escalation counts — route their event→rule lookup
    through here, so the convention is honoured uniformly. Returning the
    concrete event type (rather than a shared ``gave_up`` kind) keeps the
    algedonic ``×N`` count and escalation bookkeeping from conflating
    unrelated give-up causes. These concrete ``*_gave_up`` kinds are not in
    any valence group, so (like the other ``poller_*`` negatives) they
    surface as standalone negative algedonic signals rather than as part of
    a paired pos/neg run.
    """
    if not isinstance(evtype, str):
        return None
    rule = _EVENT_RULES.get(evtype)
    if rule is not None:
        return rule
    if evtype.endswith("_gave_up"):
        return ("negative", evtype)
    return None


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
    # chainlink #587: the scheduler loop-lag monitor fires once per stall (up to
    # ~once/sec during a bad burst). Each line embeds a different lag value, so
    # content-dedup can't collapse them — without first-occurrence-only they take
    # several recent slots and bury everything else (the symptom: the same signal
    # shown 3× with the same ×N). Latest-only surfaces the most recent lag + the
    # ×N count, which is the actionable signal.
    "scheduler_loop_lag",
    # PR 4a (git_tracking): a stuck network outage / auth issue / dirty
    # tree will re-emit on every turn until resolved. Dedup so the
    # operator-visible signal is the *most recent* failure, not 50
    # copies of the same error in the 24h window.
    "git_commit_failed",
    "git_push_failed",
    "git_pull_blocked",
    "git_home_invariant_violation",
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
    # chainlink #287: fires once per update cycle (written before execv,
    # drained on next boot). Re-emitting on subsequent turns confuses the
    # operator — the digest is the one-shot "what changed on upgrade" signal.
    "mimir_update_digest",
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

# Arousal thresholds — Beer (Brain of the Firm, Chapter 7) arousal filter.
# Maps rule kind → minimum occurrence count in the algedonic window before
# the kind claims a slot in the block. Default (absent from dict) = 1, meaning
# "surface on first occurrence" — these are Beer's "discrete internal events"
# that should bypass the regulatory hierarchy immediately.
#
# Raise threshold for kinds where a single occurrence is below the noise floor
# and only a sustained pattern is worth escalating to S5 attention. The
# threshold is a statistical criterion: "has this fired enough times to be
# significant rather than coincidental?"
#
# Infrastructure note: all currently-registered kinds remain at the default
# threshold (1). The structure is here for operational tuning as experience
# reveals routinely-noisy kinds that don't warrant first-occurrence surfacing.
# The count IS surfaced in the rendered line (see _format_lines) regardless of
# threshold — "×47 in 24h" on a git_push_ok signals "constant pushing, healthy"
# while "×1" signals "just happened once." Pattern visibility is the primary
# Alg-2 deliverable; threshold gating is secondary infrastructure.
_AROUSAL_THRESHOLDS: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Alg-3 (VSM eval S4 → S5 feedback loop): auto-escalation thresholds.
#
# Maps rule-kind (negative) → minimum count in the 24h window before an
# ``algedonic_escalation`` event is emitted.  The escalation event fires at
# most once per 24h window per kind (``_escalated_kinds_in_window`` provides
# the dedup); it surfaces in the algedonic block on subsequent turns so the
# "sustained pattern crossed threshold" signal isn't lost in the noise.
#
# Philosophy: escalation ≠ arousal.  ``_AROUSAL_THRESHOLDS`` gates *display*;
# escalation gates *emission of a new event* — a persistent paper trail that
# the viability report, introspection report, and operator can all query.  A
# kind absent from this dict is never auto-escalated (single-occurrence kinds
# like ``discord_bridge_login_failure`` are operator-actionable on first hit
# and don't need a separate escalation event).
# ---------------------------------------------------------------------------
_ESCALATION_THRESHOLDS: dict[str, int] = {
    # Git: 3 push failures in a day signals a structural problem (auth,
    # network, dirty worktree) that self-correction hasn't caught.
    "git_push_failed": 3,
    # Bridges: sustained retries (5+) indicate an outage that outlasted
    # normal backoff recovery — operator should rotate token or check
    # platform status.
    "discord_bridge_retry": 5,
    "slack_bridge_retry": 5,
    # Quota anomaly: the 5h endpoint is glitching repeatedly (>10 in 24h
    # means it hasn't cleared on its own after the first cycle or two).
    "quota_anomaly": 10,
    # Viability / collapse: any 3+ signals in a day means the collapse
    # detection is firing repeatedly — worth operator attention.
    "collapse_output_sim": 3,
    "collapse_atom_gini": 3,
    "collapse_topic_lock": 3,
    # Core prompt degraded: 2+ means the first signal didn't get resolved;
    # the agent may have been running on a stripped-down prompt for hours.
    "core_prompt_degraded": 2,
    "scheduler_loop_lag": 3,
    "scheduler_loop_lag_monitor_failed": 1,
    # Bind-mount stale: 3+ transient stales (or 2 persistent) means the
    # VirtioFS issue is recurring, not a one-off.
    "bind_mount_stale": 3,
    "bind_mount_persistent": 2,
    # Proposed-changes backlog: 5+ daily fires = 5 days of unaddressed
    # backlog. Escalation event creates a persistent paper trail beyond
    # the 24h algedonic display so introspection / viability reports
    # find it during weekly reviews.
    "proposed_changes_backlog": 5,
}


# ---------------------------------------------------------------------------
# Valence groups — define success/failure spaces for run detection.
# Each group key maps to a ValenceGroup; the kind tags used here are the
# SHORT rule-kind tags (values from _EVENT_RULES), not the raw event types.
#
# Invariant: kind sets across all groups must be disjoint.
# Validated by assertion below.
# ---------------------------------------------------------------------------

def _build_valence_groups() -> dict[str, ValenceGroup]:
    return {
        "git_push": ValenceGroup(
            label="git push",
            positive_kinds=frozenset({"git_push_ok"}),
            negative_kinds=frozenset({"git_push_failed", "git_push_stale"}),
            verb_map={
                "git_push_ok": "succeeded",
                "git_push_failed": "failed",
                "git_push_stale": "stale",
            },
        ),
        "git_pull": ValenceGroup(
            label="git pull",
            positive_kinds=frozenset({"git_pull_ok", "git_fetch_ok"}),
            negative_kinds=frozenset({"git_pull_blocked"}),
            verb_map={
                "git_pull_ok": "pulled",
                "git_fetch_ok": "fetched",
                "git_pull_blocked": "blocked",
            },
        ),
        "git_commit": ValenceGroup(
            label="git commit",
            positive_kinds=frozenset(),
            negative_kinds=frozenset({"git_commit_failed"}),
            verb_map={
                "git_commit_failed": "failed",
            },
        ),
        "bind_mount": ValenceGroup(
            label="bind mount",
            positive_kinds=frozenset({"bind_mount_recovered"}),
            negative_kinds=frozenset({"bind_mount_stale", "bind_mount_persistent"}),
            verb_map={
                "bind_mount_recovered": "recovered",
                "bind_mount_stale": "stale",
                "bind_mount_persistent": "persistent",
            },
        ),
        "discord_bridge": ValenceGroup(
            label="Discord bridge",
            positive_kinds=frozenset(),
            negative_kinds=frozenset({
                "discord_bridge_retry",
                "discord_bridge_login_failure",
                "discord_bridge_intents_failure",
            }),
            verb_map={
                "discord_bridge_retry": "retrying",
                "discord_bridge_login_failure": "login-failed",
                "discord_bridge_intents_failure": "intents-failed",
            },
        ),
        "slack_bridge": ValenceGroup(
            label="Slack bridge",
            positive_kinds=frozenset(),
            negative_kinds=frozenset({
                "slack_bridge_retry",
                "slack_bridge_auth_failure",
                "slack_bridge_scope_failure",
            }),
            verb_map={
                "slack_bridge_retry": "retrying",
                "slack_bridge_auth_failure": "auth-failed",
                "slack_bridge_scope_failure": "scope-failed",
            },
        ),
        "oauth": ValenceGroup(
            label="oauth",
            positive_kinds=frozenset({"oauth_usage_ok", "oauth_refresh_ok"}),
            negative_kinds=frozenset({
                "oauth_usage_failed",
                "oauth_logged_out",
                "oauth_refresh_age_warn",
            }),
            verb_map={
                "oauth_usage_ok": "ok",
                "oauth_refresh_ok": "refreshed",
                "oauth_usage_failed": "failed",
                "oauth_logged_out": "logged-out",
                "oauth_refresh_age_warn": "age-warn",
            },
        ),
        "spawn": ValenceGroup(
            label="spawn",
            positive_kinds=frozenset({"spawn_ok"}),
            negative_kinds=frozenset({"spawn_auth_fail", "spawn_work_fail"}),
            verb_map={
                "spawn_ok": "completed",
                "spawn_auth_fail": "auth-failed",
                "spawn_work_fail": "work-failed",
            },
        ),
        "quota": ValenceGroup(
            label="quota",
            positive_kinds=frozenset({"quota_recovered"}),
            negative_kinds=frozenset({"quota_exhausted", "quota_anomaly"}),
            verb_map={
                "quota_recovered": "recovered",
                "quota_exhausted": "exhausted",
                "quota_anomaly": "anomalous",
            },
        ),
        "viability": ValenceGroup(
            label="viability",
            positive_kinds=frozenset({"viability_ok"}),
            negative_kinds=frozenset({
                "collapse_output_sim",
                "collapse_atom_gini",
                "collapse_topic_lock",
                "curation_reflection_low",
                "curation_feedback_low",
                "curation_forget_low",
                "viability_error",
            }),
            verb_map={
                "viability_ok": "healthy",
                "collapse_output_sim": "output-similarity",
                "collapse_atom_gini": "atom-concentration",
                "collapse_topic_lock": "topic-lock",
                "curation_reflection_low": "reflection-low",
                "curation_feedback_low": "feedback-low",
                "curation_forget_low": "forget-low",
                "viability_error": "error",
            },
        ),
        # Logical pairings not in the original spec but present in _EVENT_RULES.
        "index_integrity": ValenceGroup(
            label="index integrity",
            positive_kinds=frozenset({"index_integrity_ok"}),
            negative_kinds=frozenset({"index_integrity_failed"}),
            verb_map={
                "index_integrity_ok": "ok",
                "index_integrity_failed": "failed",
            },
        ),
        "shell_job": ValenceGroup(
            label="shell job",
            positive_kinds=frozenset({"shell_job_complete_enqueue_ok"}),
            negative_kinds=frozenset({"shell_job_complete_enqueue_failed"}),
            verb_map={
                "shell_job_complete_enqueue_ok": "enqueued",
                "shell_job_complete_enqueue_failed": "enqueue-failed",
            },
        ),
    }


_VALENCE_GROUPS: dict[str, ValenceGroup] = _build_valence_groups()

# Validate: kind sets across all groups must be disjoint.
_all_grouped_kinds: list[str] = []
for _vg in _VALENCE_GROUPS.values():
    _all_grouped_kinds.extend(_vg.positive_kinds)
    _all_grouped_kinds.extend(_vg.negative_kinds)
assert len(_all_grouped_kinds) == len(set(_all_grouped_kinds)), (
    "ValenceGroup kind sets must be disjoint — a kind appears in more than "
    "one group. Check _VALENCE_GROUPS for duplicates."
)
del _all_grouped_kinds, _vg


# Pre-pass helper: count occurrences of each rule-matched event kind
# in the algedonic window. Iterates tail-first (most-recent first),
# stops at cutoff_iso. Called before the main selection loop so the
# full-window count is available for (a) threshold gating and
# (b) attaching to displayed FeedbackSignals as pattern-visibility context.
def _count_kinds_in_window(
    snapshot: "JsonlSnapshot | None",
    events_path: Path,
    cutoff_iso: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in iter_window_records(snapshot, events_path):  # #498: complete window
        ts = ev.get("timestamp")
        if not isinstance(ts, str) or ts < cutoff_iso:
            if isinstance(ts, str):
                break
            continue
        evtype = ev.get("type")
        rule = classify(evtype)
        if rule is None:
            continue
        _, kind = rule
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _escalated_kinds_in_window(
    snapshot: "JsonlSnapshot | None",
    events_path: Path,
    cutoff_iso: str,
) -> set[str]:
    """Return the set of negative kinds already escalated in the window.

    Scans for ``algedonic_escalation`` events whose ``kind`` payload matches
    a negative kind tag.  Used by ``_emit_new_escalations`` to provide a 24h
    dedup so each kind emits at most one escalation event per window.
    """
    escalated: set[str] = set()
    for ev in iter_window_records(snapshot, events_path):  # #498: complete window
        ts = ev.get("timestamp")
        if not isinstance(ts, str) or ts < cutoff_iso:
            if isinstance(ts, str):
                break
            continue
        if ev.get("type") == "algedonic_escalation":
            kind = ev.get("kind")
            if isinstance(kind, str):
                escalated.add(kind)
    return escalated


def _emit_new_escalations(
    kind_counts: dict[str, int],
    already_escalated: set[str],
    thresholds: dict[str, int],
) -> None:
    """Emit ``algedonic_escalation`` events for threshold-crossing kinds not yet escalated.

    Runs synchronously (via ``log_event_sync``); called from
    ``FeedbackLog.recent()`` after the arousal-filter kind-count pre-pass.
    The ``already_escalated`` set (from ``_escalated_kinds_in_window``)
    provides the 24h dedup: each kind emits at most one escalation per window.

    Beer framing: Alg-3 closes the S4 → S5 algedonic loop.  Arousal (Alg-2)
    decides *what to display*; escalation decides *what to record as a new
    event* so the threshold-crossing outlives the current turn's display
    window and surfaces in introspection reports + viability scans.
    """
    from ..event_logger import log_event_sync  # lazy import — avoids top-level cycle

    for kind, threshold in thresholds.items():
        if kind in already_escalated:
            continue
        count = kind_counts.get(kind, 0)
        if count >= threshold:
            try:
                log_event_sync(
                    "algedonic_escalation",
                    kind=kind,
                    count=count,
                    threshold=threshold,
                )
            except RuntimeError:
                # event_logger not initialised (e.g. in unit tests that
                # construct FeedbackLog directly without a running server).
                # Escalation is best-effort — silently skip rather than
                # crashing the caller.
                log.debug(
                    "algedonic escalation for %r skipped: event_logger not initialised",
                    kind,
                )


# ---------------------------------------------------------------------------
