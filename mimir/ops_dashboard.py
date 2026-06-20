"""Live ops dashboard — reads ``logs/events.jsonl`` on demand and renders
an operational overview at ``/ops``. No caching; every request recomputes
from the log so what you see is always current.

Adapted from open-strix's ``ops_dashboard.py`` (commit ``fee7d1d``,
2026-04-25). Mimir's event vocabulary differs from open-strix's, so the
analytics layer is rewritten — same overall shape (summary cards, tabs,
Chart.js bar/line charts, recent-failures pre-block) but the buckets
reflect mimir's actual surfaces:

- Volume + queued-by-trigger histograms
- Resolution-path histograms for the chainlink #23 events
  (``saga_*_ctx_resolution`` + ``bash_async_ctx_resolution``)
- Async shell-job counters (spawned / routed / no-channel / enqueue-failed)
- Failure-shaped events (anything ending in ``_failed`` / ``_error`` /
  ``_blocked`` / ``_anomalous`` / ``_rejected``)
- Backlog tab listing instrumentation gaps mimir hasn't filled yet

The companion ``/api/ops`` returns the same payload as JSON for ad-hoc
scripting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import tail_jsonl_records
from .feedback import FeedbackLog

log = logging.getLogger(__name__)


_MAX_DAYS = 365
_DEFAULT_DAYS = 7
_RECENT_FAILURES_CAP = 30


# Failure-shape suffixes. Anything matching one of these gets folded
# into the failures bucket. Tuned conservatively — `*_blocked` and
# `*_anomalous` are also failure-shaped for our purposes (the
# system noticed something off).
_FAILURE_SUFFIXES = (
    "_failed",
    "_error",
    "_blocked",
    "_anomalous",
    "_rejected",
)


# Resolution-path event kinds from chainlink #23 #25/#26/#27 (saga
# tools) + PR 56 (bash_async). Captured separately so the dashboard
# can render per-tool resolution_path histograms — the most useful
# observability surface mimir has today for "is the model passing
# session_id correctly?"
_RESOLUTION_EVENT_KINDS = (
    "saga_synthesis_ctx_resolution",
    "saga_query_ctx_resolution",
    "saga_store_ctx_resolution",
    "saga_feedback_ctx_resolution",
    "saga_mark_contributions_ctx_resolution",
    "bash_async_ctx_resolution",
)


def _parse_ts(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_events(events_log: Path, days: int) -> list[dict[str, Any]]:
    """Stream the event log from the tail; keep records inside the
    cutoff window.

    Pre-2026-05-10 this forward-scanned the entire file, filtering
    by cutoff. With events.jsonl bounded at ~300 MB and the dashboard
    polled by /api/ops, the forward scan re-parsed up to 75% of the
    file just to discard records older than the window. Now we
    tail-read newest-first and break as soon as we cross the cutoff.

    Trace-further #3 (2026-05-10 spike) verified that timestamp order
    matches append order in the live events.jsonl — 0 inversions across
    10K events with 10+ concurrent writers. So break-on-cutoff is
    correct in practice. If a future producer pattern reintroduces
    out-of-order writes, swap the ``break`` for ``continue`` and let
    the loop run to BOF.

    Malformed JSON lines are silently skipped. Missing log file
    returns []. Output is in chronological order (oldest-first) —
    callers like ``compute_stats`` build day-keyed aggregates that
    don't depend on ordering, but ordering is preserved for
    backwards-compat with any future caller that does.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if not events_log.exists():
        return []
    out: list[dict[str, Any]] = []
    for record in tail_jsonl_records(events_log):
        ts = _parse_ts(record.get("timestamp", ""))
        if ts is None:
            # Malformed / missing ts — keep scanning. Worst case, a
            # long stretch of malformed records walks us to BOF
            # without ever crossing the cutoff. In practice every
            # mimir-emitted event carries a tz-aware UTC ISO ts; bad
            # values would only arrive from third-party writers, and
            # the firehose is mimir-only.
            continue
        if ts < cutoff:
            break
        record["_ts"] = ts
        out.append(record)
    out.reverse()  # tail yields newest-first; restore chronological
    return out


def _load_turns(turns_log: Path, days: int) -> list[dict[str, Any]]:
    """Stream turns.jsonl from the tail; keep records inside the cutoff
    window. Same tail-and-break pattern as ``_load_events``, but the
    timestamp field is ``ts`` (TurnRecord shape) rather than
    ``timestamp`` (Event shape).

    Used by the token-usage chart in the Usage tab — per-turn token
    counts live in ``TurnRecord.usage``, not in events.jsonl.

    Missing log file returns []. Malformed JSON skipped. Output is
    chronological order (oldest-first) for caller convenience.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if not turns_log.exists():
        return []
    out: list[dict[str, Any]] = []
    for record in tail_jsonl_records(turns_log):
        ts = _parse_ts(record.get("ts", ""))
        if ts is None:
            continue
        if ts < cutoff:
            break
        out.append(record)
    out.reverse()
    return out


def _day_key(ts: datetime) -> str:
    return ts.date().isoformat()


def _is_failure_kind(kind: str) -> bool:
    return any(kind.endswith(suffix) for suffix in _FAILURE_SUFFIXES)


def compute_stats(events: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Roll up the loaded events into the JSON payload the HTML/JSON
    endpoints both consume."""
    by_event: Counter[str] = Counter()
    queued_by_trigger: Counter[str] = Counter()
    queued_by_channel: Counter[str] = Counter()
    queued_by_day: Counter[str] = Counter()
    events_by_day: Counter[str] = Counter()

    failures_by_kind: Counter[str] = Counter()
    recent_failures: list[dict[str, Any]] = []

    # Per-tool resolution-path histograms keyed by tool kind.
    # Inner Counter: "saga_session_id" → N, "single_active" → N, etc.
    resolution_paths: defaultdict[str, Counter[str]] = defaultdict(Counter)

    # Async shell-job counters.
    shell_spawned = 0
    shell_routed = 0
    shell_no_channel = 0
    shell_enqueue_failed = 0
    shell_spawn_by_channel: Counter[str] = Counter()

    # Per-tool counters (chainlink #364). ``tool_call`` records carry an
    # ``ok`` boolean; ``tool_error`` is also accepted so older/future
    # producers that only emit the error branch still contribute to the
    # failure-rate numerator.
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    tool_duration_ms: defaultdict[str, float] = defaultdict(float)

    for record in events:
        kind = record.get("type", "unknown")
        ts: datetime = record["_ts"]

        by_event[kind] += 1
        events_by_day[_day_key(ts)] += 1

        if kind == "event_queued":
            trigger = record.get("trigger") or "unknown"
            queued_by_trigger[trigger] += 1
            channel_id = record.get("channel_id") or "unknown"
            queued_by_channel[channel_id] += 1
            queued_by_day[_day_key(ts)] += 1
        elif kind in _RESOLUTION_EVENT_KINDS:
            path = record.get("resolution_path") or "unknown"
            resolution_paths[kind][path] += 1
        elif kind == "bash_async_spawned":
            shell_spawned += 1
            channel_id = record.get("channel_id") or "(none)"
            shell_spawn_by_channel[channel_id] += 1
        elif kind == "shell_job_complete_routed":
            shell_routed += 1
        elif kind == "shell_job_complete_no_channel":
            shell_no_channel += 1
        elif kind == "shell_job_complete_enqueue_failed":
            shell_enqueue_failed += 1
        elif kind == "tool_call":
            tool = record.get("tool") or "unknown"
            tool_calls[tool] += 1
            if record.get("ok") is False:
                tool_errors[tool] += 1
            try:
                tool_duration_ms[tool] += float(record.get("duration_ms") or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind == "tool_error":
            tool = record.get("tool") or "unknown"
            if not record.get("paired_tool_call"):
                # The normal middleware emits both tool_call(ok=false) and
                # tool_error. Avoid double-counting that common case by
                # letting the tool_call branch own the numerator; standalone
                # tool_error producers still contribute to the numerator.
                tool_errors[tool] += 1

        if _is_failure_kind(kind):
            failures_by_kind[kind] += 1
            # Collect every failure in the window. ``_load_events``
            # iterates the file in append order (oldest first), so an
            # earlier "stop after N" cap during this loop would keep
            # the OLDEST N — exactly the wrong end for a "recent
            # failures" surface. Memory is bounded by failures-in-
            # window × ~200 chars per entry; even pathological cases
            # (10k failures over 7d) stay under a few MB.
            recent_failures.append({
                "t": ts.isoformat(),
                "kind": kind,
                "channel_id": record.get("channel_id"),
                "trigger": record.get("trigger"),
                "detail": _failure_detail(record),
            })

    # Sort most-recent-first across the full set, then trim to the
    # cap so the rendered table stays scannable.
    recent_failures.sort(key=lambda x: x["t"], reverse=True)
    recent_failures = recent_failures[:_RECENT_FAILURES_CAP]

    days_axis = sorted(events_by_day.keys())
    timeseries = [
        {
            "day": d,
            "events": events_by_day.get(d, 0),
            "queued": queued_by_day.get(d, 0),
        }
        for d in days_axis
    ]

    summary = {
        "total_events": sum(by_event.values()),
        "events_queued": by_event.get("event_queued", 0),
        "messages_sent": by_event.get("send_message_sent", 0),
        "subagents_started": by_event.get("subagent_started", 0),
        "subagents_completed": by_event.get("subagent_notification", 0),
        "shell_jobs_spawned": shell_spawned,
        "shell_jobs_routed": shell_routed,
        "failures": sum(failures_by_kind.values()),
        "high_water_events": by_event.get("event_queue_high_water", 0),
        "client_pool_drains": by_event.get("client_pool_drained", 0),
        "tool_calls": sum(tool_calls.values()),
        "tool_errors": sum(tool_errors.values()),
    }

    tool_stats = []
    for tool in sorted(set(tool_calls) | set(tool_errors)):
        calls = tool_calls.get(tool, 0)
        errors = tool_errors.get(tool, 0)
        tool_stats.append({
            "tool": tool,
            "calls": calls,
            "errors": errors,
            "failure_rate": (errors / calls) if calls else 0.0,
            "avg_duration_ms": (tool_duration_ms.get(tool, 0.0) / calls)
            if calls else 0.0,
        })
    tool_stats.sort(
        key=lambda row: (row["errors"], row["calls"], row["tool"]),
        reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "summary": summary,
        "by_event": dict(by_event.most_common()),
        "queued_by_trigger": dict(queued_by_trigger.most_common()),
        "queued_by_channel": dict(queued_by_channel.most_common(20)),
        "resolution_paths": {
            kind: dict(counter.most_common())
            for kind, counter in resolution_paths.items()
        },
        "shell_jobs": {
            "spawned": shell_spawned,
            "routed": shell_routed,
            "no_channel": shell_no_channel,
            "enqueue_failed": shell_enqueue_failed,
            "spawn_by_channel": dict(shell_spawn_by_channel.most_common(20)),
        },
        "tools": tool_stats,
        "failures_by_kind": dict(failures_by_kind.most_common()),
        "timeseries": timeseries,
        "recent_failures": recent_failures,
        "backlog": _backlog_items(),
    }


def _failure_detail(record: dict[str, Any]) -> str:
    """Pull a short string from whichever field the failing event used.

    Different failure events carry different payloads — this just
    surfaces *something* in the recent-failures pre-block so the
    operator can pivot to grep events.jsonl with the right key."""
    for key in ("error", "reason", "stderr", "message", "detail", "stage"):
        val = record.get(key)
        if val:
            text = str(val)
            return text if len(text) <= 200 else text[:200] + "…"
    return ""


def _backlog_items() -> list[dict[str, str]]:
    """Instrumentation gaps the dashboard would surface if they existed.
    Visible on the Backlog tab so operators see what's not yet captured
    without leaving the page."""
    return [
        {
            "id": "turn-timing-histogram",
            "title": "Turn duration histogram",
            "status": "Partial — turns.jsonl has timing but events.jsonl doesn't",
            "blocker": (
                "Turn duration lives in turns.jsonl as a per-turn record; "
                "the dashboard reads events.jsonl. Either widen the "
                "dashboard's data source to read turns.jsonl too, or "
                "emit a turn_complete event with duration_ms at the end "
                "of run_turn."
            ),
        },
        {
            "id": "session-boundary-rate",
            "title": "Session-boundary fire rate (synthesis pressure)",
            "status": "Partial — saga_synthesis_skipped_boundary fires but no positive counterpart",
            "blocker": (
                "saga_session_end happens via the synthesis turn; no "
                "explicit event fires when a session boundary is "
                "*actually* crossed. Adding a session_boundary_crossed "
                "event lets the dashboard show synthesis throughput vs "
                "skip rate."
            ),
        },
    ]


def parse_days_param(raw: str | None, default: int = _DEFAULT_DAYS) -> int:
    """Validate the ``?days=N`` query param. Errors as ValueError so
    the route handler can return 400."""
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be an integer") from exc
    if value < 1:
        raise ValueError("days must be >= 1")
    if value > _MAX_DAYS:
        raise ValueError(f"days must be <= {_MAX_DAYS}")
    return value


_CHAINLINK_TIMEOUT_SECONDS = 5.0
_CHAINLINK_MAX_ISSUES = 200


async def _load_chainlink_issues(home: Path) -> dict[str, Any]:
    """Run ``chainlink issue list --json`` against ``home`` and return
    a structured envelope.

    Returns ``{"available": bool, "issues": list, "error": str | None}``
    so the frontend can distinguish "chainlink not initialized here"
    from "real failure" without crashing the whole dashboard. Bounded
    to ``_CHAINLINK_MAX_ISSUES`` so a deployment with thousands of
    closed issues doesn't blow the prompt budget — the dashboard is
    a triage surface, not an exhaustive issue browser.

    Soft-fails on every error path: chainlink not on PATH, repo not
    initialized, JSON garbled, subprocess hangs. The dashboard's own
    Backlog tab handles the "chainlink unavailable" message.
    """
    try:
        # ``--status open`` is defensive — chainlink's current default
        # is ``open`` but pinning it at the call site means a future
        # CLI default change can't silently start filling the dashboard
        # tab with closed/archived issues.
        proc = await asyncio.create_subprocess_exec(
            "chainlink", "issue", "list", "--status", "open", "--json",
            cwd=str(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"available": False, "issues": [], "error": "chainlink CLI not on PATH"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "issues": [], "error": str(exc)[:500]}

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_CHAINLINK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Drain the stdout/stderr pipes after kill so file descriptors
        # release immediately rather than lingering until GC. Under
        # heavy /ops traffic with a hung chainlink CLI the FD count
        # could otherwise climb. Swallow exceptions — the kill already
        # happened; we're just cleaning up.
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        return {
            "available": False,
            "issues": [],
            "error": f"chainlink timed out after {_CHAINLINK_TIMEOUT_SECONDS}s",
        }

    if proc.returncode != 0:
        err_text = (stderr or b"").decode("utf-8", errors="replace").strip()
        return {
            "available": False,
            "issues": [],
            "error": err_text[:500] or f"chainlink exit code {proc.returncode}",
        }

    try:
        issues = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {"available": False, "issues": [], "error": f"chainlink output: {exc}"}

    if not isinstance(issues, list):
        return {"available": False, "issues": [], "error": "chainlink returned non-list payload"}

    return {
        "available": True,
        "issues": issues[:_CHAINLINK_MAX_ISSUES],
        "error": None,
        "truncated": len(issues) > _CHAINLINK_MAX_ISSUES,
        "total_count": len(issues),
    }


def build_dashboard_payload(
    events_log: Path, days: int, *, active_provider: str | None = None,
) -> dict[str, Any]:
    """Sync top-level entry: load events, compute stats. Adds an
    empty ``chainlink_issues`` envelope so the frontend's tab renders
    consistently. For the chainlink-populated variant used by the
    live route handler, see ``build_dashboard_payload_async``."""
    from .token_usage_history import compute_token_usage_history
    from .usage_history import compute_usage_history, filter_history_to_provider

    events = _load_events(events_log, days)
    stats = compute_stats(events, days)
    stats["chainlink_issues"] = {
        "available": False, "issues": [], "error": None,
    }
    # Per-provider subscription-quota history for the ops chart. Collapse to
    # the ACTIVE provider (chainlink #301, dashboard side) so stale windows
    # from a prior subscription — e.g. Anthropic OAuth keys still in the store
    # after a Codex cutover — don't render a phantom chart next to the live
    # one. ``active_provider=None`` (the default, and what tests / the sync
    # entrypoint pass) keeps every provider, so genuine multi-subscription
    # deployments are unaffected unless the caller opts in.
    # ``events`` is already date-filtered by ``_load_events``.
    stats["usage_history"] = filter_history_to_provider(
        compute_usage_history(events), active_provider,
    )
    # Per-day token volume history (Usage tab). Useful for both modes:
    # subscription deployments see absolute volume alongside
    # utilization-%; API-mode deployments use this as the PRIMARY
    # Usage chart since their ``usage_history`` is empty (no
    # quota-poll events fire on pay-per-token routes).
    turns_log = events_log.parent / "turns.jsonl"
    turns = _load_turns(turns_log, days)
    stats["token_usage_history"] = compute_token_usage_history(turns)
    stats["algedonic_signals"] = _build_algedonic_signals(events_log, turns_log)
    return stats


async def build_dashboard_payload_async(
    events_log: Path,
    days: int,
    *,
    home: Path | None = None,
    active_provider: str | None = None,
) -> dict[str, Any]:
    """Async variant: same as ``build_dashboard_payload`` plus the
    chainlink subprocess call when ``home`` is given. The route
    handler uses this; tests that don't exercise chainlink can stick
    with the sync version. ``active_provider`` collapses the Usage chart
    to that provider (see ``build_dashboard_payload``)."""
    stats = build_dashboard_payload(
        events_log, days, active_provider=active_provider,
    )
    if home is not None:
        stats["chainlink_issues"] = await _load_chainlink_issues(home)
    return stats


def _build_algedonic_signals(events_log: Path, turns_log: Path) -> dict[str, Any]:
    """Render the same recent feedback-signal body used in agent prompts.

    The prompt block is intentionally a 24h algedonic window, independent of
    the dashboard's broader analytics ``days`` filter. Keeping this as the
    rendered Markdown body makes the Ops > Signals page match what the agent
    sees instead of introducing a second formatter with drift risk.
    """
    window_hours = 24
    block = FeedbackLog(
        events_path=events_log,
        turns_path=turns_log,
    ).recent_block(window_hours=window_hours)
    return {
        "title": "Recent feedback signals",
        "window_hours": window_hours,
        "block": block or "",
    }


def render_dashboard_html(stats: dict[str, Any] | None = None) -> str:
    """Return the dashboard HTML shell.

    Pre-2026-05-10 this function injected a server-rendered ``stats``
    payload into the HTML via a ``__DATA__`` placeholder, so the
    frontend had no XHR. Pattern B (auth-on-all-routes) made that
    shape untenable: the /ops route is exempt from the auth middleware
    so the JS can prompt for a key on first visit, but baking the
    dashboard data into the exempt-route HTML would leak it to anyone
    who could load the page. The frontend now AJAX-fetches /api/ops
    (which IS auth-required), so this function no longer takes the
    stats argument. Kept the parameter for one release of API
    compatibility — it's silently ignored — and the caller in
    ``web_ui.ops_page`` no longer computes a payload before calling.
    """
    return _load_dashboard_html()


# chainlink #243: dashboard HTML lives in a sibling .html file.
# Lazy-loaded + cached so the first /ops request pays the read but the
# rest is in-memory. Moving the JS out of the Python triple-string
# eliminates the "Python eats backslash" footgun and lets IDEs syntax-
# highlight the JS / lint it normally.
_DASHBOARD_HTML: str | None = None


def _load_dashboard_html() -> str:
    global _DASHBOARD_HTML
    if _DASHBOARD_HTML is None:
        _DASHBOARD_HTML = (
            Path(__file__).parent / "ops_dashboard.html"
        ).read_text(encoding="utf-8")
    return _DASHBOARD_HTML



__all__: tuple[str, ...] = (
    "build_dashboard_payload",
    "compute_stats",
    "parse_days_param",
    "render_dashboard_html",
)
