"""v0.4 §2: algedonic surfacing — subpackage root.

Surfaces recent self-feedback signals (errors, denials, loop hits;
positive engagement) into the agent's turn prompt. Beer's framing:
algedonic channel — pain/pleasure signals that bypass the regulatory
hierarchy and feed back to S5. In mimir, that means the agent's own
errors and successes get a guaranteed prompt slot independent of the
inbound message context.

Source of truth is ``logs/events.jsonl`` + ``logs/turns.jsonl`` — no
parallel state to keep coherent. The reader scans tail-first, stops at
the time window or the per-polarity cap, whichever hits first.

This file is the package root; sub-modules hold the implementation:

- ``_models`` — data classes (ValenceGroup, Run, AnnotatedRun, FeedbackSignal)
- ``rules`` — _EVENT_RULES, _VALENCE_GROUPS, escalation helpers
- ``runs`` — temporal run detection, chain-signal synthesis
- ``renderers`` — _render_event_line and all kind-specific branches
- ``cross_turn`` — cross-turn send-loop detection
- ``resolved`` — resolved-incidents I/O

All public names imported here for backward compatibility: callers that
do ``from mimir.feedback import X`` continue to resolve unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .._jsonl_tail import tail_jsonl_records
from ..jsonl_snapshot import (
    JsonlSnapshot,
    iter_snapshot_or_tail,
    iter_window_records,
)

# --- Sub-module imports + backward-compat re-exports ---
from ._models import (  # noqa: F401
    Polarity,
    ValenceGroup,
    Run,
    AnnotatedRun,
    FeedbackSignal,
)
from .rules import (  # noqa: F401
    _EVENT_RULES,
    classify,
    _ESCALATION_THRESHOLDS,
    _ESCALATION_ONLY_EVENT_THRESHOLDS,
    _AROUSAL_THRESHOLDS,
    _VALENCE_GROUPS,
    _FIRST_OCCURRENCE_ONLY_KINDS,
    _POLARITY_DYNAMIC_KINDS,
    _build_valence_groups,
    _count_kinds_in_window,
    _count_escalation_only_events_in_window,
    _escalated_kinds_in_window,
    _emit_new_escalations,
)
from .runs import (  # noqa: F401
    _compute_group_runs,
    _annotate_transitions,
    _format_run_segment,
    _format_chain,
    _synthesize_chain_signals,
)
from .renderers import (  # noqa: F401
    _FIELD_MAX_LEN,
    _CONTROL_CHARS_RE,
    _sanitize_field,
    _render_event_line,
    _render_turn_error,
)
from .cross_turn import _detect_cross_turn_send_loops  # noqa: F401
from .resolved import (  # noqa: F401
    _load_resolved_incidents,
    _parse_resolved_ts,
    _is_event_resolved,
)

log = logging.getLogger(__name__)



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
    arousal_thresholds: dict[str, int] | None = None
    # None → use the module-level _AROUSAL_THRESHOLDS. Override in tests
    # (or operator config) to tune per-kind minimum-occurrence thresholds
    # without monkeypatching. Passed as None by default so the live
    # deployment uses the shared dict and changes to it propagate at
    # import time.
    escalation_thresholds: dict[str, int] | None = None
    # None → use the module-level _ESCALATION_THRESHOLDS (Alg-3). Pass {} to
    # disable escalation entirely (e.g. in tests that don't want side-effects
    # from _emit_new_escalations writing to events.jsonl). Same propagation
    # semantics as ``arousal_thresholds``.
    resolved_incidents_path: Path | None = None
    # Path to ``resolved-incidents.jsonl`` (chainlink #197). Events matching
    # an entry there are filtered from the feedback block until the rolling
    # window naturally clears them. None → no filtering (default for tests /
    # back-compat). The live agent wires this to
    # ``config.home / "resolved-incidents.jsonl"``.

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

        # Resolved-incident filter (chainlink #197): load rules once per
        # recent() call. Events matching a rule are silently dropped from
        # both polarities so stale-but-fixed errors don't re-surface.
        resolved_rules = (
            _load_resolved_incidents(self.resolved_incidents_path)
            if self.resolved_incidents_path is not None
            else []
        )

        # Arousal filter pre-pass (Beer Ch.7): count all rule-matched
        # event-kind occurrences in the window before the display loop.
        # Serves two purposes:
        #   1. Threshold gate: kinds below their min_occurrences are
        #      suppressed — only sustained patterns clear their filter.
        #   2. Count display: FeedbackSignal.count > 1 renders as
        #      "(×N in Xh)" so the agent can distinguish a one-off from
        #      a recurring pattern without needing separate tool calls.
        thresholds = (
            self.arousal_thresholds
            if self.arousal_thresholds is not None
            else _AROUSAL_THRESHOLDS
        )
        kind_counts = _count_kinds_in_window(
            self.events_snapshot, self.events_path, cutoff_iso
        )

        # Alg-3: emit algedonic_escalation events for negative kinds that
        # crossed their threshold and haven't already been escalated in this
        # 24h window.  Escalation is a WRITE side-effect (log_event_sync
        # appends to events.jsonl) so callers that want side-effect-free
        # behavior should pass escalation_thresholds={} on construction.
        _esc_thresholds = (
            self.escalation_thresholds
            if self.escalation_thresholds is not None
            else _ESCALATION_THRESHOLDS
        )
        if _esc_thresholds:
            _already_escalated = _escalated_kinds_in_window(
                self.events_snapshot, self.events_path, cutoff_iso
            )
            escalation_counts = dict(kind_counts)
            for kind, count in _count_escalation_only_events_in_window(
                self.events_snapshot, self.events_path, cutoff_iso
            ).items():
                escalation_counts[kind] = escalation_counts.get(kind, 0) + count
            escalation_thresholds = dict(_esc_thresholds)
            escalation_thresholds.update(
                {
                    kind: threshold
                    for kind, threshold in _ESCALATION_ONLY_EVENT_THRESHOLDS.values()
                }
            )
            _emit_new_escalations(
                escalation_counts, _already_escalated, escalation_thresholds
            )

        # Temporal run detection (Alg-2 enhancement): scan for valence-group
        # polarity transitions (recovery / degradation) and synthesize one
        # chain FeedbackSignal per transitioning group.  chain_consumed_kinds
        # is the set of rule-kind tags fully rendered by a chain — the main
        # loop must skip those individual events so they don't duplicate.
        # Chain signals are always-surface (bypass the per-polarity limit).
        group_runs = _compute_group_runs(
            self.events_snapshot, self.events_path, cutoff_iso, _VALENCE_GROUPS
        )
        chain_signals, chain_consumed_kinds = _synthesize_chain_signals(
            group_runs, _VALENCE_GROUPS
        )

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
        for ev in iter_window_records(self.events_snapshot, self.events_path):  # #498
            ts = ev.get("timestamp")
            if not isinstance(ts, str) or ts < cutoff_iso:
                # tail-first scan: as soon as a record predates the window
                # we can stop; jsonl is appended in chronological order.
                if isinstance(ts, str):
                    break
                continue
            evtype = ev.get("type")
            rule = classify(evtype)
            if rule is None:
                continue
            # Resolved-incident filter: skip events covered by an operator-
            # marked resolution rule.  Applied after the rule lookup (unknown
            # event types are already dropped above) so we don't penalise the
            # common case (no rules file → list is empty, no work done).
            if resolved_rules and _is_event_resolved(ev, resolved_rules):
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
            # Chain-consumed kinds: their events are fully rendered by a
            # transition chain signal. Skip them in the individual-event
            # path so they don't duplicate the chain line.
            if kind in chain_consumed_kinds:
                continue
            # Arousal filter: suppress kinds below their minimum-occurrence
            # threshold. Default threshold is 1 (always surface); raise in
            # _AROUSAL_THRESHOLDS for kinds where single occurrence is noise.
            min_occ = thresholds.get(kind, 1)
            if kind_counts.get(kind, 0) < min_occ:
                continue
            # First-occurrence-only kinds: skip duplicates. Tail-first
            # iteration means we keep the most recent.
            if kind in _FIRST_OCCURRENCE_ONLY_KINDS:
                if kind in seen_first_only:
                    continue
                seen_first_only.add(kind)
            content = _render_event_line(kind, ev)
            # Content-level dedup: if a prior (more recent) event already
            # produced this exact line text in this polarity bucket, skip —
            # see ``seen_content`` comment above. Render cost is low and we're
            # in tail-first iteration, so the *kept* item is the most recent.
            #
            # #496: record content BEFORE the display-capacity gate below, so
            # the dedup set stays complete even when the bucket is full. Keeps
            # the turns.jsonl pass (and any future emit path) from re-emitting
            # a line identical to an in-window event we saw but had no room to
            # display. Output is unchanged — only the dedup set is more complete.
            polarity_bucket = seen_content[polarity]
            if content in polarity_bucket:
                continue
            polarity_bucket.add(content)
            target = negatives if polarity == "negative" else positives
            if len(target) >= limit:
                continue
            target.append(
                FeedbackSignal(
                    ts=ts,
                    polarity=polarity,
                    kind=kind,
                    channel_id=ev.get("channel_id"),
                    content=content,
                    count=kind_counts.get(kind, 1),
                )
            )
            # Early exit when both sides full.
            if len(negatives) >= limit and len(positives) >= limit:
                break

        # Inject chain signals (always-surface — they bypass the per-polarity
        # limit).  Sort each bucket by ts descending so chain signals
        # interleave correctly with individually-displayed events.
        for sig in chain_signals:
            if sig.polarity == "negative":
                negatives.append(sig)
            else:
                positives.append(sig)
        negatives.sort(key=lambda s: s.ts, reverse=True)
        positives.sort(key=lambda s: s.ts, reverse=True)

        # S2-2: cross-turn send_message loop detection.  Always-surface —
        # a cross-turn flood is an acute S2 concern that bypasses the
        # per-polarity limit.  Detection runs after the main events loop so
        # it doesn't interfere with the within-turn loop_stop / loop_warn
        # signals that the individual-event pass already handles.
        for sig in _detect_cross_turn_send_loops(
            self.events_snapshot, self.events_path, cutoff_iso
        ):
            negatives.append(sig)
        if any(s.kind == "cross_turn_loop" for s in negatives):
            negatives.sort(key=lambda s: s.ts, reverse=True)

        # 2) turns.jsonl — error / result_is_error are turn-level negatives
        # the events stream might not capture.  Capacity here is measured
        # against only the limit-bounded event signals gathered above: chain
        # signals and cross-turn loop signals intentionally bypass the
        # per-polarity limit, so they must not starve crash/error records.
        bounded_negative_count = sum(
            1
            for sig in negatives
            if not sig.kind.endswith("_chain") and sig.kind != "cross_turn_loop"
        )
        if bounded_negative_count < limit:
            for rec in iter_window_records(self.turns_snapshot, self.turns_path):  # #498
                ts = rec.get("ts")
                if not isinstance(ts, str) or ts < cutoff_iso:
                    if isinstance(ts, str):
                        break
                    continue
                has_error = rec.get("error") or rec.get("result_is_error")
                if not has_error:
                    continue
                if bounded_negative_count >= limit:
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
                bounded_negative_count += 1

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
        parts.extend(_format_lines(negatives, window_hours=window_hours))
    if positives:
        if parts:
            parts.append("")  # blank line between subsections
        parts.append(f"Positive (last {window_hours}h):")
        parts.extend(_format_lines(positives, window_hours=window_hours))
    return "\n".join(parts)


def _format_lines(signals: list[FeedbackSignal], *, window_hours: int = 24) -> list[str]:
    out: list[str] = []
    for sig in signals:
        # YYYY-MM-DDTHH:MM:SS+00:00 → "YYYY-MM-DD HH:MM" for compactness.
        ts = _short_ts(sig.ts)
        ch = f" [{sig.channel_id}]" if sig.channel_id else ""
        # Arousal filter count display: "(×N in Xh)" when count > 1
        # surfaces the "pattern vs one-off" distinction that Beer's
        # arousal filter is meant to provide. A single occurrence renders
        # with no suffix — no noise for genuinely discrete events.
        # Exception: chain signals (kind ends with "_chain") already encode
        # per-run counts inline ("succeeded ×20 → failed ×5 → ...") — a
        # redundant total count suffix would be confusing and noisy.
        if sig.kind.endswith("_chain"):
            count_suffix = ""
        else:
            count_suffix = f" (×{sig.count} in {window_hours}h)" if sig.count > 1 else ""
        out.append(f"- {ts} — {sig.content}{count_suffix}{ch}")
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
