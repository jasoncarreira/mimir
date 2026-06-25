"""Unit tests for _warn_category_skew in the longmemeval bench runner.

These tests do NOT require the ``saga`` package — they only exercise the
pure-Python warning helper that checks category balance after ``--limit``
is applied.
"""
from __future__ import annotations

import sys
from io import StringIO

import pytest

from benchmarks.longmemeval_via_mimir.runner import (
    _LONGMEMEVAL_CATEGORIES,
    _filter_question_types as _filter_mimir_question_types,
    _warn_category_skew,
)
from benchmarks.longmemeval_via_memory.runner import (
    _filter_question_types as _filter_memory_question_types,
)


def _make_dataset(*question_types: str) -> list[dict]:
    """Build a minimal dataset with the given sequence of question_type values."""
    return [{"question_id": f"q{i}", "question_type": qt} for i, qt in enumerate(question_types)]


def _capture_stderr(fn, *args, **kwargs) -> str:
    """Run fn(*args, **kwargs) and return anything printed to stderr."""
    buf = StringIO()
    old, sys.stderr = sys.stderr, buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stderr = old
    return buf.getvalue()


def _capture_stderr_and_result(fn, *args, **kwargs) -> tuple[str, object]:
    """Run fn(*args, **kwargs) and return stderr plus the function result."""
    buf = StringIO()
    old, sys.stderr = sys.stderr, buf
    try:
        result = fn(*args, **kwargs)
    finally:
        sys.stderr = old
    return buf.getvalue(), result


@pytest.mark.parametrize(
    "filter_fn",
    [_filter_mimir_question_types, _filter_memory_question_types],
)
def test_question_types_two_category_slice_and_counts(filter_fn):
    dataset = _make_dataset(
        "single-session-user",
        "single-session-preference",
        "multi-session",
        "knowledge-update",
        "single-session-preference",
        "multi-session",
        "temporal-reasoning",
    )
    output, filtered = _capture_stderr_and_result(
        filter_fn,
        dataset,
        " single-session-preference, multi-session ",
    )

    assert [item["question_id"] for item in filtered] == ["q1", "q2", "q4", "q5"]
    assert "Selected question types:" in output
    assert "single-session-preference=2" in output
    assert "multi-session=2" in output


@pytest.mark.parametrize(
    "filter_fn",
    [_filter_mimir_question_types, _filter_memory_question_types],
)
def test_question_types_unknown_type_rejected_with_valid_types(filter_fn):
    dataset = _make_dataset(
        "single-session-user",
        "multi-session",
    )

    with pytest.raises(ValueError) as exc_info:
        filter_fn(dataset, "multi-session,missing-type")

    msg = str(exc_info.value)
    assert "unknown --question-types value(s): missing-type" in msg
    assert "Valid dataset question types:" in msg
    assert "single-session-user" in msg
    assert "multi-session" in msg


@pytest.mark.parametrize(
    "filter_fn",
    [_filter_mimir_question_types, _filter_memory_question_types],
)
def test_question_types_filter_applies_before_limit(filter_fn):
    dataset = _make_dataset(
        "single-session-user",
        "single-session-user",
        "single-session-preference",
        "multi-session",
        "multi-session",
    )

    _output, filtered = _capture_stderr_and_result(
        filter_fn,
        dataset,
        "single-session-preference,multi-session",
    )
    limited = filtered[:2]

    assert [item["question_id"] for item in limited] == ["q2", "q3"]


# ---------------------------------------------------------------------------
# No-warning cases
# ---------------------------------------------------------------------------

def test_no_warning_when_all_six_categories_present():
    dataset = _make_dataset(
        "single-session-user",
        "multi-session",
        "single-session-assistant",
        "single-session-preference",
        "knowledge-update",
        "temporal-reasoning",
    )
    output = _capture_stderr(_warn_category_skew, dataset, 6)
    assert output == "", f"Expected no warning but got: {output!r}"


def test_no_warning_when_limit_covers_all_categories():
    # Same as all-present — limit value doesn't matter to the function,
    # only the already-sliced dataset content does.
    dataset = _make_dataset(
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "knowledge-update",
        "temporal-reasoning",
        "multi-session",
    )
    output = _capture_stderr(_warn_category_skew, dataset, 6)
    assert output == ""


# ---------------------------------------------------------------------------
# Warning cases
# ---------------------------------------------------------------------------

def test_warning_when_four_categories_missing():
    """The canonical --limit 100 shape: only 2 of 6 categories present."""
    dataset = _make_dataset(
        *["single-session-user"] * 70,
        *["multi-session"] * 30,
    )
    output = _capture_stderr(_warn_category_skew, dataset, 100)
    assert "WARNING" in output
    assert "4 of 6" in output
    assert "single-session-assistant" in output
    assert "single-session-preference" in output
    assert "knowledge-update" in output
    assert "temporal-reasoning" in output
    assert "--limit 100" in output


def test_warning_includes_present_category_counts():
    dataset = _make_dataset(
        *["single-session-user"] * 3,
        *["multi-session"] * 2,
    )
    output = _capture_stderr(_warn_category_skew, dataset, 5)
    assert "single-session-user=3" in output
    assert "multi-session=2" in output


def test_warning_when_only_one_category():
    dataset = _make_dataset("knowledge-update", "knowledge-update", "knowledge-update")
    output = _capture_stderr(_warn_category_skew, dataset, 3)
    assert "WARNING" in output
    assert "5 of 6" in output
    assert "knowledge-update=3" in output


def test_warning_mentions_nan_consequence():
    dataset = _make_dataset("single-session-user")
    output = _capture_stderr(_warn_category_skew, dataset, 1)
    assert "NaN" in output or "nan" in output.lower()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_dataset_no_crash():
    """Empty dataset after slicing should not crash."""
    output = _capture_stderr(_warn_category_skew, [], 0)
    # All categories will be 0 — warns about all 6
    assert "WARNING" in output
    assert "6 of 6" in output


def test_unknown_question_type_not_counted_as_known_category():
    """Items with unknown question_type values don't suppress the warning."""
    dataset = _make_dataset("mystery-type", "another-unknown")
    output = _capture_stderr(_warn_category_skew, dataset, 2)
    assert "WARNING" in output
    # All 6 known categories should be listed as missing
    assert "6 of 6" in output


def test_category_constant_has_six_entries():
    assert len(_LONGMEMEVAL_CATEGORIES) == 6


def test_category_constant_matches_known_benchmark_types():
    expected = {
        "single-session-user",
        "multi-session",
        "single-session-assistant",
        "single-session-preference",
        "knowledge-update",
        "temporal-reasoning",
    }
    assert _LONGMEMEVAL_CATEGORIES == expected
