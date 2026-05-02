"""``mimir loops`` — runtime introspection of mimir's feedback loops.

Joins the static loop-inventory (VSM-tagged comments scanned from
mimir/ + saga/saga/) against runtime evidence (events.jsonl + turns.jsonl
tail) to produce a per-loop status table grouped by VSM layer.

The diagnostic value is the **never-fired** rows. If inbound-reactions
hasn't fired in a week, either nobody's reacting or the bridge wiring
is broken. If the heartbeat hasn't fired, scheduler.yaml is wrong.
The CLI surfaces these silences so they don't sit invisibly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .loop_inventory import LoopTag, scan as scan_inventory, default_roots


# ─── Loop ID → event-type mapping ────────────────────────────────
# Maps the loop_id used in code tags to the events.jsonl "type" field
# we expect to see when the loop fires. Loops without a single
# event signature (like 1.1 mark_contributions, where the firing
# evidence is in turns.jsonl saga_atom_ids, not a discrete event)
# get a custom probe in _measure_runtime.

_LOOP_EVENT_MAP: dict[str, list[str]] = {
    "1.1": ["saga_feedback_sent"],         # post-message credit pass
    "1.3": ["send_message_loop_warning",
            "send_message_loop_hard_stop"],  # LoopDetector trips
    "1.4": ["tool_call_denied",
            "tool_call_budget_warning"],     # budget hits
    "2.1": [],   # algedonic — fires every turn that renders the block
    "2.2": ["saga_session_started"],         # session-boundary writes
    "2.3": [],   # operator alert — surfaces in system prompt only
    "2.4": ["cost_rate_alert",
            "rate_limit_warning",
            "rate_limit_off_pace"],          # threshold trips
    "2.5": [],   # most-retrieved — invoked from reflection skill
    "2.6": ["react_received"],               # inbound reactions
    "4.1": ["scheduled_tick"],               # heartbeat / cron
    "4.3": ["saga_consolidate_ok",
            "saga_consolidate_error"],       # consolidation
    "4.4": [],   # decay — no event today
    "4.5": [],   # supersedes — internal saga writes
    "4.6": [],   # world model — internal retrieval pathway
    "pre-message": ["saga_query_error"],     # error-only event
}


@dataclass
class LoopStatus:
    """Joined static inventory + runtime evidence for one loop."""

    loop_id: str
    layer: str
    description: str
    sites: list[LoopTag]
    last_fired: datetime | None
    volume_24h: int
    expected_event_types: list[str]

    @property
    def status(self) -> str:
        """Three-bucket health: healthy, idle, never-fired.

        - healthy: at least one fire in last 24h
        - idle: fired before but nothing in 24h
        - never-fired: no record at all (diagnostic — bridge wiring,
          missing scheduler entry, or genuinely-nobody-using-it)
        """
        if self.volume_24h > 0:
            return "healthy"
        if self.last_fired is not None:
            return "idle"
        if not self.expected_event_types:
            # No probe defined — neither healthy nor unhealthy.
            return "n/a"
        return "never-fired"


def _humanize_age(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _measure_runtime(
    events_log: Path, expected_types: list[str], *, now: datetime,
) -> tuple[datetime | None, int]:
    """Tail-scan events.jsonl for the latest matching event timestamp
    and the count in the last 24h. Cheap (jsonl appends in chrono
    order; we stop at the cutoff)."""
    if not expected_types or not events_log.exists():
        return None, 0
    cutoff = (now - timedelta(hours=24)).isoformat()
    types = set(expected_types)
    last_fired: datetime | None = None
    volume = 0

    # Read backwards in 64KB chunks. Most events.jsonl files are
    # bounded by MIMIR_MAX_EVENTS so even a full forward scan is fine
    # in practice; we forward-scan for simplicity.
    try:
        with events_log.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") not in types:
                    continue
                ts = ev.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if last_fired is None or when > last_fired:
                    last_fired = when
                if ts >= cutoff:
                    volume += 1
    except OSError:
        return None, 0
    return last_fired, volume


def collect_loops(events_log: Path, *, now: datetime | None = None) -> list[LoopStatus]:
    """Scan code for VSM tags, fold runtime evidence onto each."""
    now = now or datetime.now(tz=timezone.utc)
    tags = scan_inventory(default_roots())

    # Group multi-site tags by loop_id (e.g. 2.6 = Discord + Slack).
    by_id: dict[str, list[LoopTag]] = {}
    for tag in tags:
        if not tag.loop_id:
            continue
        by_id.setdefault(tag.loop_id, []).append(tag)

    rows: list[LoopStatus] = []
    for loop_id, sites in by_id.items():
        primary = sites[0]
        expected = _LOOP_EVENT_MAP.get(loop_id, [])
        last_fired, volume = _measure_runtime(events_log, expected, now=now)
        rows.append(LoopStatus(
            loop_id=loop_id,
            layer=primary.layer,
            description=primary.description,
            sites=sites,
            last_fired=last_fired,
            volume_24h=volume,
            expected_event_types=expected,
        ))
    return rows


def render_table(rows: list[LoopStatus], *, now: datetime | None = None) -> str:
    """Format the status table. One row per loop_id, grouped by VSM
    layer. ``mimir loops`` prints this and exits."""
    now = now or datetime.now(tz=timezone.utc)

    # VSM layer ordering for display — innermost → outermost.
    layer_order = [
        "S1", "S2", "S3", "S3*", "S4", "S5",
        "algedonic", "algedonic (in)", "algedonic (out)",
    ]

    def layer_key(layer: str) -> tuple[int, str]:
        # Strip parenthetical qualifiers ("S3 (saga-internal)" → "S3")
        # for ordering; keep full text for display.
        bare = layer.split(" (")[0]
        for i, prefix in enumerate(layer_order):
            if bare == prefix:
                return (i, layer)
        return (len(layer_order), layer)

    rows_sorted = sorted(rows, key=lambda r: (layer_key(r.layer), r.loop_id))

    if not rows_sorted:
        return "(no VSM-tagged loops found)"

    header = f"{'Layer':<22}  {'Loop':<8}  {'Last fired':<12}  {'24h vol':>7}  Status      Description"
    sep = "─" * len(header)
    out = [header, sep]
    for r in rows_sorted:
        last = _humanize_age(r.last_fired, now)
        status_marker = {
            "healthy": "✓ healthy",
            "idle": "· idle",
            "never-fired": "⚠ never-fired",
            "n/a": "· n/a",
        }.get(r.status, r.status)
        desc = r.description[:60]
        out.append(
            f"{r.layer:<22}  {r.loop_id:<8}  {last:<12}  {r.volume_24h:>7}  {status_marker:<13} {desc}"
        )

    # Footer: silences worth surfacing.
    silences = [r for r in rows_sorted if r.status == "never-fired"]
    if silences:
        out.append("")
        out.append("Loops never observed firing (check wiring or just no traffic):")
        for r in silences:
            out.append(f"  • {r.loop_id} ({r.layer}) — expects events: {', '.join(r.expected_event_types)}")
    return "\n".join(out)


def run_loops_cmd(home: Path) -> int:
    """`mimir loops --home <path>` entry point."""
    events_log = home / "logs" / "events.jsonl"
    rows = collect_loops(events_log)
    print(render_table(rows))
    return 0
