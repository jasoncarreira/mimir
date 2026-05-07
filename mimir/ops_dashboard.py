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

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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
    """Stream the event log; keep records inside the cutoff window.

    Malformed JSON lines are silently skipped (matches the rest of
    mimir's `_read_jsonl` pattern). Missing log file returns []."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    if not events_log.exists():
        return out
    with events_log.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(record.get("timestamp", ""))
            if ts is None or ts < cutoff:
                continue
            record["_ts"] = ts
            out.append(record)
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
        "auto_dispatch_ok": by_event.get("auto_dispatch_ok", 0),
        "subagents_started": by_event.get("subagent_started", 0),
        "subagents_completed": by_event.get("subagent_notification", 0),
        "shell_jobs_spawned": shell_spawned,
        "shell_jobs_routed": shell_routed,
        "failures": sum(failures_by_kind.values()),
        "high_water_events": by_event.get("event_queue_high_water", 0),
        "client_pool_drains": by_event.get("client_pool_drained", 0),
    }

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
            "id": "tool-call-counters",
            "title": "Per-tool call counts (Read / Write / Bash / Glob / etc.)",
            "status": "Not instrumented",
            "blocker": (
                "SDK preset tools fire through hooks but mimir doesn't "
                "emit a per-call event with the tool name. Add a "
                "tool_call event in mimir.hooks PostToolUse so the "
                "dashboard can surface tool-mix shifts (e.g. Bash spike "
                "= subagent runaway, Read spike = navigation loop)."
            ),
        },
        {
            "id": "tool-failure-rate",
            "title": "Tool failure rate (vs raw call counts)",
            "status": "Blocked on tool-call-counters",
            "blocker": (
                "Once tool_call events exist, pair with a tool_error "
                "branch (PreToolUse rejection, PostToolUse non-zero "
                "result) so failure rate is computable per-tool."
            ),
        },
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
            "id": "llm-token-usage",
            "title": "Token usage per turn (input / output / cache reads)",
            "status": "Not instrumented",
            "blocker": (
                "ResultMessage carries token counts; mimir captures it "
                "in turns.jsonl but not as a discrete events.jsonl entry. "
                "Adding a per-turn llm_usage event would let the "
                "dashboard render token-burn timeseries alongside the "
                "cost-rate alert events."
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


def build_dashboard_payload(events_log: Path, days: int) -> dict[str, Any]:
    """Top-level entry: load events, compute stats."""
    events = _load_events(events_log, days)
    return compute_stats(events, days)


def render_dashboard_html(stats: dict[str, Any]) -> str:
    """Inject the stats payload into the HTML template via a JSON
    script tag. The frontend reads it once on load — no XHR back to
    /api/ops needed."""
    return _DASHBOARD_HTML.replace("__DATA__", json.dumps(stats))


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex,nofollow" />
    <title>mimir Ops</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
      :root {
        --paper: #f5efe3;
        --paper-strong: #fffaf1;
        --ink: #1e2430;
        --muted: #5f6b76;
        --line: rgba(30, 36, 48, 0.12);
        --accent: #0d766e;
        --accent-soft: rgba(13, 118, 110, 0.12);
        --warn: #b76d0d;
        --warn-soft: rgba(183, 109, 13, 0.12);
        --bad: #b13324;
      }
      * { box-sizing: border-box; }
      html, body {
        margin: 0;
        background:
          radial-gradient(circle at top left, rgba(13, 118, 110, 0.08), transparent 32rem),
          linear-gradient(180deg, #efe4cf 0%, #f7f2e7 36%, #f5efe3 100%);
        color: var(--ink);
        font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      }
      body { padding: 1rem 1.4rem 3rem; }
      .shell { max-width: 1100px; margin: 0 auto; }
      header.page-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 1rem;
        flex-wrap: wrap;
        padding-bottom: 0.6rem;
        border-bottom: 1px solid var(--line);
        margin-bottom: 1.2rem;
      }
      header.page-header h1 { margin: 0; font-size: 1.4rem; font-weight: 600; }
      header.page-header a { color: var(--accent); text-decoration: none; font-size: 0.9rem; margin-left: 1rem; }
      header.page-header a:hover { text-decoration: underline; }
      .meta { color: var(--muted); font-size: 0.82rem; }
      .summary-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 0.6rem;
        margin-bottom: 1.4rem;
      }
      .stat {
        background: var(--paper-strong);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 0.75rem 0.9rem;
      }
      .stat .num { font-size: 1.4rem; font-weight: 600; color: var(--accent); }
      .stat .num.bad { color: var(--bad); }
      .stat .label { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
      .tabs {
        display: flex;
        gap: 0.2rem;
        border-bottom: 1px solid var(--line);
        margin-bottom: 1rem;
        flex-wrap: wrap;
      }
      .tab {
        padding: 0.5rem 0.9rem;
        cursor: pointer;
        background: transparent;
        border: none;
        font-size: 0.92rem;
        color: var(--muted);
        font-family: inherit;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
      }
      .tab:hover { color: var(--ink); }
      .tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
      .panel { display: none; }
      .panel.active { display: block; }
      .chart-wrap {
        background: var(--paper-strong);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 0.7rem;
        margin: 0.7rem 0;
      }
      canvas { max-height: 320px; }
      table { border-collapse: collapse; width: 100%; font-size: 0.9rem; margin: 0.6rem 0 1rem; }
      th, td { padding: 0.45rem 0.7rem; text-align: left; border-bottom: 1px solid var(--line); }
      th { background: rgba(13, 118, 110, 0.04); font-weight: 600; }
      td.num { text-align: right; font-variant-numeric: tabular-nums; }
      .backlog-item {
        background: var(--warn-soft);
        border-left: 3px solid var(--warn);
        padding: 0.7rem 0.9rem;
        margin: 0.5rem 0;
        border-radius: 4px;
      }
      .backlog-item h3 { margin: 0 0 0.25rem 0; font-size: 0.95rem; }
      .backlog-item .status { font-size: 0.8rem; color: var(--warn); font-weight: 500; }
      .backlog-item .blocker { font-size: 0.86rem; color: var(--muted); margin-top: 0.3rem; }
      details { margin: 0.6rem 0; }
      summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; }
      pre { background: var(--paper-strong); border: 1px solid var(--line); padding: 0.6rem; border-radius: 4px; font-size: 0.78rem; overflow-x: auto; }
      .hint { color: var(--muted); font-size: 0.85rem; margin: 0.2rem 0 0.7rem; }
      .resolution-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 0.6rem;
      }
      .resolution-card {
        background: var(--paper-strong);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 0.6rem 0.8rem;
      }
      .resolution-card h4 { margin: 0 0 0.4rem 0; font-size: 0.9rem; font-family: ui-monospace, SFMono-Regular, monospace; color: var(--ink); }
      .resolution-bar { display: flex; height: 18px; border-radius: 3px; overflow: hidden; margin: 0.3rem 0; background: rgba(0,0,0,0.04); }
      .resolution-seg { font-size: 0.7rem; color: white; text-align: center; line-height: 18px; padding: 0 4px; white-space: nowrap; }
      .resolution-seg.saga_session_id { background: var(--accent); }
      .resolution-seg.single_active { background: #4a8c84; }
      .resolution-seg.contextvar { background: var(--warn); }
      .resolution-seg.missing { background: var(--bad); }
      .resolution-seg.unknown { background: var(--muted); }
      .resolution-legend { font-size: 0.75rem; color: var(--muted); margin-top: 0.3rem; }
    </style>
  </head>
  <body>
    <main class="shell">
      <header class="page-header">
        <div>
          <h1>mimir Ops</h1>
          <div class="meta" id="meta"></div>
        </div>
        <nav>
          <a href="/turns" title="Turn viewer">Turns</a>
          <a href="/api/ops" title="JSON twin">JSON</a>
        </nav>
      </header>

      <section class="summary-grid" id="summary"></section>

      <nav class="tabs">
        <button class="tab active" data-panel="overview">Overview</button>
        <button class="tab" data-panel="invocations">Invocations</button>
        <button class="tab" data-panel="resolution">Resolution paths</button>
        <button class="tab" data-panel="shell">Shell jobs</button>
        <button class="tab" data-panel="failures">Failures</button>
        <button class="tab" data-panel="backlog">Backlog</button>
        <button class="tab" data-panel="raw">Raw</button>
      </nav>

      <section id="overview" class="panel active">
        <div class="chart-wrap"><canvas id="event-mix"></canvas></div>
        <div class="chart-wrap"><canvas id="events-timeseries"></canvas></div>
      </section>

      <section id="invocations" class="panel">
        <p class="hint">Trigger = the kind of event that opened a turn (user_message, scheduled_tick, saga_session_end, shell_job_complete). Channel = which conversation it landed on.</p>
        <div class="chart-wrap"><canvas id="trigger-mix"></canvas></div>
        <h3>Events queued by channel (top 20)</h3>
        <table id="channel-table"><thead><tr><th>Channel</th><th class="num">Queued</th></tr></thead><tbody></tbody></table>
      </section>

      <section id="resolution" class="panel">
        <p class="hint">Per-tool ctx-resolution path histograms (chainlink #23). <code>saga_session_id</code> = model passed it correctly. <code>single_active</code> = heuristic fallback (works in single-channel; fragile under concurrency). <code>contextvar</code> = direct-call fallback (test path). <code>missing</code> = no ctx found, the call's per-turn bookkeeping silently no-op'd.</p>
        <div class="resolution-grid" id="resolution-grid"></div>
      </section>

      <section id="shell" class="panel">
        <p class="hint">Async shell-job wake-up loop. Routed = wake-up event landed on the spawning channel. No-channel = job exited without a routing target. Enqueue-failed = bridge crashed while routing.</p>
        <div class="summary-grid" id="shell-summary"></div>
        <h3>Spawned by channel (top 20)</h3>
        <table id="shell-channel-table"><thead><tr><th>Channel</th><th class="num">Spawned</th></tr></thead><tbody></tbody></table>
      </section>

      <section id="failures" class="panel">
        <p class="hint">Failure-shaped events (anything ending in <code>_failed</code>, <code>_error</code>, <code>_blocked</code>, <code>_anomalous</code>, <code>_rejected</code>).</p>
        <h3>Failures by kind</h3>
        <table id="failure-table"><thead><tr><th>Kind</th><th class="num">Count</th></tr></thead><tbody></tbody></table>
        <details open><summary>Recent failures (up to 30)</summary><pre id="recent-failures"></pre></details>
      </section>

      <section id="backlog" class="panel">
        <p class="hint">Data not yet captured. Each item describes the instrumentation needed.</p>
        <div id="backlog-list"></div>
      </section>

      <section id="raw" class="panel">
        <h3>All event types</h3>
        <table id="event-table"><thead><tr><th>Event type</th><th class="num">Count</th></tr></thead><tbody></tbody></table>
      </section>
    </main>

    <script id="data" type="application/json">__DATA__</script>
    <script>
      const D = JSON.parse(document.getElementById('data').textContent);

      document.getElementById('meta').textContent =
        'Window: ' + D.window_days + ' days · Generated ' + D.generated_at + ' · Live read of logs/events.jsonl';

      document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
        document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        document.getElementById(t.dataset.panel).classList.add('active');
      }));

      const summaryEl = document.getElementById('summary');
      const labels = {
        total_events: 'Total events',
        events_queued: 'Events queued',
        auto_dispatch_ok: 'Bridge dispatches',
        subagents_started: 'Subagents started',
        subagents_completed: 'Subagents completed',
        shell_jobs_spawned: 'Shell jobs spawned',
        shell_jobs_routed: 'Shell jobs routed',
        failures: 'Failures',
        high_water_events: 'Queue high-water hits',
        client_pool_drains: 'Pool drains',
      };
      const badKeys = new Set(['failures', 'high_water_events']);
      for (const [k, v] of Object.entries(D.summary)) {
        if (v === null || v === undefined) continue;
        const div = document.createElement('div');
        div.className = 'stat';
        const numClass = (badKeys.has(k) && v > 0) ? 'num bad' : 'num';
        div.innerHTML = '<div class="' + numClass + '">' + v + '</div><div class="label">' + (labels[k] || k) + '</div>';
        summaryEl.appendChild(div);
      }

      // Shell-jobs sub-summary on the Shell panel.
      const shellSummary = document.getElementById('shell-summary');
      const sj = D.shell_jobs;
      const shellLabels = {
        spawned: 'Spawned',
        routed: 'Routed',
        no_channel: 'No-channel drops',
        enqueue_failed: 'Enqueue failed',
      };
      const shellBadKeys = new Set(['no_channel', 'enqueue_failed']);
      for (const k of ['spawned', 'routed', 'no_channel', 'enqueue_failed']) {
        const v = sj[k] || 0;
        const div = document.createElement('div');
        div.className = 'stat';
        const numClass = (shellBadKeys.has(k) && v > 0) ? 'num bad' : 'num';
        div.innerHTML = '<div class="' + numClass + '">' + v + '</div><div class="label">' + shellLabels[k] + '</div>';
        shellSummary.appendChild(div);
      }

      function fillTable(id, obj, emptyMsg) {
        const tbody = document.querySelector('#' + id + ' tbody');
        const entries = Object.entries(obj);
        if (entries.length === 0) {
          const tr = document.createElement('tr');
          tr.innerHTML = '<td colspan="2" style="color:var(--muted)">' + (emptyMsg || 'no data') + '</td>';
          tbody.appendChild(tr);
          return;
        }
        for (const [k, v] of entries) {
          const tr = document.createElement('tr');
          tr.innerHTML = '<td>' + k + '</td><td class="num">' + v + '</td>';
          tbody.appendChild(tr);
        }
      }
      fillTable('event-table', D.by_event);
      fillTable('channel-table', D.queued_by_channel, 'no events queued in window');
      fillTable('shell-channel-table', D.shell_jobs.spawn_by_channel, 'no shell jobs spawned in window');
      fillTable('failure-table', D.failures_by_kind, 'no failures in window');

      document.getElementById('recent-failures').textContent =
        D.recent_failures.length ? JSON.stringify(D.recent_failures, null, 2) : '(none)';

      // Resolution-path cards: one per tool kind, each rendering a
      // proportion bar + label so missing/contextvar dominance jumps
      // out without needing a Chart.js call per card.
      const resolutionGrid = document.getElementById('resolution-grid');
      const pathOrder = ['saga_session_id', 'single_active', 'contextvar', 'missing'];
      const resolutionEntries = Object.entries(D.resolution_paths);
      if (resolutionEntries.length === 0) {
        resolutionGrid.innerHTML = '<div class="hint">No resolution-path events in window. Either no MCP tool calls landed, or the tools are short-circuiting before logging — check the Raw tab.</div>';
      } else {
        for (const [kind, paths] of resolutionEntries) {
          const total = Object.values(paths).reduce((a, b) => a + b, 0);
          const card = document.createElement('div');
          card.className = 'resolution-card';
          const bar = document.createElement('div');
          bar.className = 'resolution-bar';
          const allKeys = pathOrder.concat(Object.keys(paths).filter(k => !pathOrder.includes(k)));
          for (const key of allKeys) {
            const count = paths[key] || 0;
            if (count === 0) continue;
            const pct = (count / total) * 100;
            const seg = document.createElement('div');
            seg.className = 'resolution-seg ' + (pathOrder.includes(key) ? key : 'unknown');
            seg.style.width = pct + '%';
            seg.title = key + ': ' + count + ' (' + pct.toFixed(1) + '%)';
            seg.textContent = pct >= 8 ? key : '';
            bar.appendChild(seg);
          }
          const legend = Object.entries(paths).map(([k, v]) => k + ': ' + v).join(' · ');
          card.innerHTML = '<h4>' + kind + '</h4>';
          card.appendChild(bar);
          const legendEl = document.createElement('div');
          legendEl.className = 'resolution-legend';
          legendEl.textContent = legend + '  (' + total + ' total)';
          card.appendChild(legendEl);
          resolutionGrid.appendChild(card);
        }
      }

      const backlogEl = document.getElementById('backlog-list');
      for (const item of D.backlog) {
        const div = document.createElement('div');
        div.className = 'backlog-item';
        div.innerHTML =
          '<h3>' + item.title + '</h3>' +
          '<div class="status">' + item.status + '</div>' +
          '<div class="blocker">' + item.blocker + '</div>';
        backlogEl.appendChild(div);
      }

      const accent = '#0d766e';
      const warn = '#b76d0d';

      const eventLabels = Object.keys(D.by_event).slice(0, 12);
      const eventValues = eventLabels.map(k => D.by_event[k]);
      new Chart(document.getElementById('event-mix'), {
        type: 'bar',
        data: { labels: eventLabels, datasets: [{ label: 'Events', data: eventValues, backgroundColor: accent }] },
        options: { plugins: { title: { display: true, text: 'Event mix (top 12)' }, legend: { display: false } } }
      });

      const days = D.timeseries.map(x => x.day);
      const events = D.timeseries.map(x => x.events);
      const queued = D.timeseries.map(x => x.queued);
      new Chart(document.getElementById('events-timeseries'), {
        type: 'line',
        data: { labels: days, datasets: [
          { label: 'Total events', data: events, borderColor: accent, tension: 0.2 },
          { label: 'Events queued', data: queued, borderColor: warn, tension: 0.2 },
        ]},
        options: { plugins: { title: { display: true, text: 'Events vs queued events per day' } } }
      });

      const triggerLabels = Object.keys(D.queued_by_trigger);
      const triggerValues = triggerLabels.map(k => D.queued_by_trigger[k]);
      new Chart(document.getElementById('trigger-mix'), {
        type: 'bar',
        data: { labels: triggerLabels, datasets: [{ label: 'Queued', data: triggerValues, backgroundColor: accent }] },
        options: { indexAxis: 'y', plugins: { title: { display: true, text: 'Events queued by trigger' }, legend: { display: false } } }
      });
    </script>
  </body>
</html>
"""


__all__: tuple[str, ...] = (
    "build_dashboard_payload",
    "compute_stats",
    "parse_days_param",
    "render_dashboard_html",
)
