"""
Lightweight query-time temporal scope extractor.

Given a natural-language query and a reference date, parse common English
time expressions ("yesterday", "last week", "three days ago", an ISO date)
into an inclusive (start, end) datetime window. Returns None when the query
doesn't mention a scope — callers then skip the temporal retrieval pathway
entirely, keeping this cheap on queries that don't need it.

Regex-only, no LLM call. Good enough for LongMemEval's common phrasings;
we'll layer in a model call if coverage turns out to be too thin.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DAY = timedelta(days=1)
_WEEK = timedelta(days=7)


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


def parse_temporal_scope(
    query: str, reference_date: Optional[datetime] = None
) -> Optional[tuple[datetime, datetime]]:
    """
    Parse a temporal scope out of ``query`` relative to ``reference_date``.

    Returns an inclusive ``(start, end)`` UTC window, or ``None`` if no
    time expression is detected. When multiple expressions are present the
    widest window covering all of them is returned (cheap approximation).
    """
    if not query:
        return None
    q = query.lower()
    ref = reference_date or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    windows: list[tuple[datetime, datetime]] = []

    # ─── Day-level absolute phrases ─────────────────────────────────────
    if re.search(r"\btoday\b", q) or re.search(r"\bright now\b", q) or re.search(r"\bcurrently\b", q):
        windows.append((_start_of_day(ref), _end_of_day(ref)))
    if re.search(r"\byesterday\b", q):
        y = ref - _DAY
        windows.append((_start_of_day(y), _end_of_day(y)))
    if re.search(r"\btomorrow\b", q):
        t = ref + _DAY
        windows.append((_start_of_day(t), _end_of_day(t)))

    # ─── "N days/weeks/months ago" ───────────────────────────────────────
    m = re.search(r"\b(\d+)\s+(day|days|week|weeks|month|months|year|years)\s+ago\b", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            target = ref - timedelta(days=n)
            windows.append((_start_of_day(target), _end_of_day(target)))
        elif unit.startswith("week"):
            end = ref - timedelta(days=n * 7)
            windows.append((end - _WEEK, end))
        elif unit.startswith("month"):
            windows.append((ref - timedelta(days=n * 30), ref))
        elif unit.startswith("year"):
            windows.append((ref - timedelta(days=n * 365), ref))

    # ─── "past/last N days" / "last week/month/year" ────────────────────
    m = re.search(r"\b(?:past|last|previous)\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            windows.append((ref - timedelta(days=n), ref))
        elif unit.startswith("week"):
            windows.append((ref - timedelta(days=n * 7), ref))
        elif unit.startswith("month"):
            windows.append((ref - timedelta(days=n * 30), ref))
        elif unit.startswith("year"):
            windows.append((ref - timedelta(days=n * 365), ref))
    if re.search(r"\blast\s+week\b", q) or re.search(r"\bpast\s+week\b", q) or re.search(r"\bthis\s+past\s+week\b", q):
        windows.append((ref - _WEEK, ref))
    if re.search(r"\blast\s+month\b", q) or re.search(r"\bpast\s+month\b", q):
        windows.append((ref - timedelta(days=30), ref))
    if re.search(r"\blast\s+year\b", q) or re.search(r"\bpast\s+year\b", q):
        windows.append((ref - timedelta(days=365), ref))

    # ─── Absolute ISO / YYYY-MM-DD ───────────────────────────────────────
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", q):
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            windows.append((_start_of_day(d), _end_of_day(d)))
        except ValueError:
            continue

    # ─── "in <Month>" or "in <Month> <YYYY>" ─────────────────────────────
    m = re.search(r"\bin\s+(" + "|".join(_MONTHS.keys()) + r")(?:\s+(\d{4}))?\b", q)
    if m:
        month = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else ref.year
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)
        windows.append((start, end))

    if not windows:
        return None

    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    return (start, end)
