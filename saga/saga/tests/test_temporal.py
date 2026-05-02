"""Tests for the query-time temporal scope extractor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from saga.temporal import parse_temporal_scope


REF = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)  # Mon 2026-04-20


def test_returns_none_when_no_scope():
    assert parse_temporal_scope("what's my favorite color?", REF) is None
    assert parse_temporal_scope("", REF) is None


def test_today():
    start, end = parse_temporal_scope("what did I do today?", REF)
    assert start.date() == REF.date()
    assert end.date() == REF.date()
    assert end - start < timedelta(days=1)


def test_yesterday():
    start, end = parse_temporal_scope("did I go running yesterday?", REF)
    assert start.date() == (REF - timedelta(days=1)).date()
    assert end.date() == (REF - timedelta(days=1)).date()


def test_n_days_ago_numeric():
    start, end = parse_temporal_scope("3 days ago", REF)
    assert start.date() == (REF - timedelta(days=3)).date()
    assert end.date() == (REF - timedelta(days=3)).date()


def test_spelled_out_numbers_not_supported_yet():
    # Deliberately documents the gap — add number-word expansion if this
    # starts hurting coverage on real queries.
    assert parse_temporal_scope("three days ago", REF) is None


def test_last_week():
    start, end = parse_temporal_scope("what did we talk about last week?", REF)
    assert end.date() == REF.date()
    assert start.date() <= (REF - timedelta(days=7)).date() + timedelta(days=1)


def test_past_n_days():
    start, end = parse_temporal_scope("meals in the past 5 days", REF)
    assert (REF - start).days == 5
    assert abs((end - REF).total_seconds()) < 60


def test_iso_date():
    start, end = parse_temporal_scope("what happened on 2023-05-14?", REF)
    assert start.date() == datetime(2023, 5, 14, tzinfo=timezone.utc).date()
    assert end.date() == datetime(2023, 5, 14, tzinfo=timezone.utc).date()


def test_month_year():
    start, end = parse_temporal_scope("events in june 2023", REF)
    assert start.date() == datetime(2023, 6, 1, tzinfo=timezone.utc).date()
    assert end.date() == datetime(2023, 6, 30, tzinfo=timezone.utc).date()


def test_month_only_uses_reference_year():
    start, end = parse_temporal_scope("dinner plans in september", REF)
    assert start.year == REF.year
    assert start.month == 9


def test_multiple_expressions_return_widest_window():
    start, end = parse_temporal_scope(
        "what did I do yesterday and 5 days ago?", REF
    )
    # Should span from 5 days ago through yesterday.
    assert (REF - start).days >= 5
    assert end.date() == (REF - timedelta(days=1)).date()
