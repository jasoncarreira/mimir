"""Temporal run detection and chain-signal synthesis.

Alg-2: reads events, groups them into runs per valence group, annotates
polarity transitions, and synthesises per-group chain FeedbackSignals.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Literal

from ._models import Polarity, ValenceGroup, Run, AnnotatedRun, FeedbackSignal
from .rules import _VALENCE_GROUPS, classify
from .._jsonl_tail import tail_jsonl_records
from ..jsonl_snapshot import JsonlSnapshot, iter_window_records

log = logging.getLogger(__name__)

# Alg-2 temporal run detection — implementation
# ---------------------------------------------------------------------------

def _compute_group_runs(
    snapshot: "JsonlSnapshot | None",
    events_path: Path,
    cutoff_iso: str,
    valence_groups: dict[str, ValenceGroup],
) -> dict[str, list[Run]]:
    """Return {group_key: [Run, ...]} in CHRONOLOGICAL order (oldest first).

    Collects all rule-matched events in the window, sorts by ts ascending,
    then identifies maximal contiguous runs per group.  "Contiguous" means
    no intervening event from the *same* group — events from other groups
    are transparent.
    """
    # Build reverse lookup: kind → (group_key, polarity)
    kind_to_group: dict[str, tuple[str, Polarity]] = {}
    for gk, group in valence_groups.items():
        for k in group.positive_kinds:
            kind_to_group[k] = (gk, "positive")
        for k in group.negative_kinds:
            kind_to_group[k] = (gk, "negative")

    # Collect all rule-matched events in the window (iter is tail-first).
    window_events: list[tuple[str, str]] = []  # (ts, kind)
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
        if kind in kind_to_group:
            window_events.append((ts, kind))

    # Sort chronologically (oldest first) for run detection.
    window_events.sort(key=lambda x: x[0])

    result: dict[str, list[Run]] = {}

    for group_key, group in valence_groups.items():
        # Filter to events belonging to this group (preserves chrono order).
        group_events = [
            (ts, kind)
            for ts, kind in window_events
            if kind_to_group.get(kind, (None,))[0] == group_key
        ]
        if not group_events:
            continue

        runs: list[Run] = []
        cur_kind = group_events[0][1]
        cur_polarity = kind_to_group[cur_kind][1]
        cur_start = group_events[0][0]
        cur_end = group_events[0][0]
        cur_count = 1

        for ts, kind in group_events[1:]:
            if kind == cur_kind:
                cur_count += 1
                cur_end = ts
            else:
                runs.append(Run(
                    group_key=group_key,
                    kind=cur_kind,
                    polarity=cur_polarity,
                    count=cur_count,
                    start_ts=cur_start,
                    end_ts=cur_end,
                ))
                cur_kind = kind
                cur_polarity = kind_to_group[kind][1]
                cur_start = ts
                cur_end = ts
                cur_count = 1

        # Close the final open run.
        runs.append(Run(
            group_key=group_key,
            kind=cur_kind,
            polarity=cur_polarity,
            count=cur_count,
            start_ts=cur_start,
            end_ts=cur_end,
        ))
        result[group_key] = runs

    return result


def _annotate_transitions(runs: list[Run]) -> list[AnnotatedRun]:
    """Tag each run with the transition that opened it (if any).

    A transition fires when the polarity of the current run differs from
    the immediately preceding run.  The first run never has a transition
    (no prior context within the window).
    """
    annotated: list[AnnotatedRun] = []
    for i, run in enumerate(runs):
        if i == 0:
            transition_type = None
        else:
            prior = runs[i - 1]
            if prior.polarity == "positive" and run.polarity == "negative":
                transition_type = "degradation"
            elif prior.polarity == "negative" and run.polarity == "positive":
                transition_type = "recovery"
            else:
                transition_type = None
        annotated.append(AnnotatedRun(run=run, transition_type=transition_type))
    return annotated


def _format_run_segment(run: Run, group: ValenceGroup) -> str:
    verb = group.verb_map.get(run.kind, run.kind)
    return f"{verb} ×{run.count}"


def _format_chain(annotated_runs: list[AnnotatedRun], group: ValenceGroup) -> str:
    """Format a run chain as a human-readable string.

    For >5 runs: show the first 2 and last 2, compressing the middle.
    The transition label ([recovery] / [degradation]) is taken from the
    rightmost transition in the chain.
    """
    # Find the most recent (rightmost) transition type.
    last_transition: Literal["recovery", "degradation"] | None = None
    for ar in annotated_runs:
        if ar.transition_type is not None:
            last_transition = ar.transition_type

    n = len(annotated_runs)
    if n <= 5:
        segments = [_format_run_segment(ar.run, group) for ar in annotated_runs]
    else:
        # Show first 2 + "... (N more)" + last 2.
        head = [_format_run_segment(ar.run, group) for ar in annotated_runs[:2]]
        tail = [_format_run_segment(ar.run, group) for ar in annotated_runs[-2:]]
        compressed = n - 4
        segments = head + [f"... ({compressed} more)"] + tail

    chain_str = " → ".join(segments)
    if last_transition:
        chain_str += f" [{last_transition}]"
    return f"{group.label}: {chain_str}"


def _synthesize_chain_signals(
    group_runs: dict[str, list[Run]],
    valence_groups: dict[str, ValenceGroup],
) -> tuple[list[FeedbackSignal], set[str]]:
    """For each group with a polarity transition, synthesize one FeedbackSignal.

    Returns:
        chain_signals: list of synthesized FeedbackSignal (one per group
            with at least one transition).
        chain_consumed_kinds: set of rule-kind tags whose events are fully
            rendered by a chain signal and should be skipped in the main
            display loop.
    """
    chain_signals: list[FeedbackSignal] = []
    chain_consumed_kinds: set[str] = set()

    for group_key, runs in group_runs.items():
        if len(runs) < 2:
            continue  # need ≥ 2 runs to have any transition

        annotated = _annotate_transitions(runs)
        has_transition = any(ar.transition_type is not None for ar in annotated)
        if not has_transition:
            continue

        group = valence_groups[group_key]
        chain_str = _format_chain(annotated, group)

        # The chain's polarity and timestamp come from the most recent run.
        most_recent = runs[-1]
        total_count = sum(r.count for r in runs)

        chain_signals.append(FeedbackSignal(
            ts=most_recent.end_ts,
            polarity=most_recent.polarity,
            kind=f"{group_key}_chain",
            channel_id=None,
            content=chain_str,
            count=total_count,
        ))

        # Mark ALL kinds in this group as chain-consumed so the main loop
        # skips their individual events (the chain renders the full sequence).
        chain_consumed_kinds.update(group.positive_kinds)
        chain_consumed_kinds.update(group.negative_kinds)

    return chain_signals, chain_consumed_kinds


# ---------------------------------------------------------------------------
# Prompt-injection hardening helper
# ---------------------------------------------------------------------------

_FIELD_MAX_LEN = 240  # per-field cap for sanitized event-payload strings
