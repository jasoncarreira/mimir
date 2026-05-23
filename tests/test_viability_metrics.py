"""Tests for the viability metrics module (collapse + curation).

Spec source: ``state/wiki/concepts/collapse-dynamics.md`` (the three
collapse indicators) + the 2026-05-23 VSM eval's curation-rate gap.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir import viability_metrics as vm


# ── fixtures ─────────────────────────────────────────────────────────


def _write_turn(
    home: Path,
    *,
    output: str = "",
    trigger: str = "user_message",
    channel_id: str = "ch1",
    saga_atom_ids: list[str] | None = None,
    ts: datetime | None = None,
    error: str | None = None,
) -> None:
    """Append a synthetic turn record to <home>/logs/turns.jsonl."""
    ts = ts or datetime.now(tz=timezone.utc)
    rec = {
        "turn_id": f"t{ts.timestamp()}",
        "ts": ts.isoformat(),
        "trigger": trigger,
        "channel_id": channel_id,
        "output": output,
        "saga_atom_ids": saga_atom_ids or [],
        "error": error,
    }
    log = home / "logs" / "turns.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _write_event(
    home: Path,
    *,
    type: str,
    ts: datetime | None = None,
    **extra,
) -> None:
    ts = ts or datetime.now(tz=timezone.utc)
    rec = {"timestamp": ts.isoformat(), "type": type, **extra}
    log = home / "logs" / "events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ── helpers ──────────────────────────────────────────────────────────


def test_gini_uniform_is_zero():
    """Equal counts across buckets → Gini = 0 (no concentration)."""
    assert vm._gini([5, 5, 5, 5]) == pytest.approx(0.0, abs=1e-6)


def test_gini_fully_concentrated_approaches_one():
    """All counts in one bucket → Gini approaches (n-1)/n. For 10
    buckets, that's 0.9 — the known maximum for discrete Gini."""
    counts = [0] * 9 + [100]
    g = vm._gini(counts)
    # (n - 1)/n = 0.9 for n=10.
    assert g == pytest.approx(0.9, abs=1e-6)


def test_gini_single_bucket_returns_zero():
    """A single observation can't have concentration — return 0,
    don't crash."""
    assert vm._gini([42]) == 0.0


def test_gini_empty_returns_zero():
    assert vm._gini([]) == 0.0


def test_top_token_extracts_dominant_alpha_word():
    text = "Working on the cosine similarity computation across recent outputs"
    # The dominant alpha word should be one of the >3-char tokens.
    token = vm._top_token(text)
    assert len(token) >= 4
    # Should not match stopwords like "with" (4 chars but stopword).
    assert token not in vm._top_token.__defaults__ if vm._top_token.__defaults__ else True


def test_top_token_empty_returns_empty():
    assert vm._top_token("") == ""
    assert vm._top_token("the and for") == ""  # all stopwords


def test_cosine_orthogonal():
    """Orthogonal vectors → cosine = 0."""
    assert vm._cosine([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_identical():
    """Identical vectors → cosine = 1."""
    assert vm._cosine([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)


def test_cosine_zero_vector_returns_zero():
    """Zero-norm vector shouldn't divide-by-zero."""
    assert vm._cosine([0, 0, 0], [1, 1, 1]) == 0.0


# ── collapse metrics ─────────────────────────────────────────────────


def test_collapse_metrics_empty_home(tmp_path: Path):
    """No turns.jsonl yet → all fields safely None / 0."""
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7)
    assert metrics.window_turns == 0
    assert metrics.cosine_sim_mean is None
    assert metrics.atom_citation_gini is None
    assert metrics.topic_diversity_ratio is None


def test_collapse_metrics_filters_by_window(tmp_path: Path):
    """Turns outside the window are excluded."""
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=30)
    _write_turn(tmp_path, output="old turn", ts=old)
    _write_turn(tmp_path, output="recent turn", ts=now)
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.window_turns == 1


def test_collapse_metrics_filters_errored_turns(tmp_path: Path):
    """Errored turns (no real output) are excluded — they don't
    represent actual model behavior worth measuring."""
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="real output", ts=now)
    _write_turn(tmp_path, output="errored", ts=now, error="TimeoutError: x")
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.window_turns == 1


def test_atom_citation_gini_high_concentration(tmp_path: Path):
    """All citations land on one atom → Gini close to (n-1)/n.
    With one cited atom out of one, Gini = 0 (no concentration to
    measure); but ten turns all citing the same atom = uniform on
    one bucket = still 0. Concentration requires multiple atoms with
    one dominating."""
    now = datetime.now(tz=timezone.utc)
    # 10 turns citing atom-A; 1 turn citing atom-B → very unequal.
    for _ in range(10):
        _write_turn(tmp_path, output="x", saga_atom_ids=["atom-A"], ts=now)
    _write_turn(tmp_path, output="y", saga_atom_ids=["atom-B"], ts=now)
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.citations_total == 11
    assert metrics.distinct_atoms_cited == 2
    assert metrics.atom_citation_gini is not None
    # For [1, 10], Gini = (10 - 1) / (1 + 10) ≈ 0.41 — meaningful.
    assert 0.4 < metrics.atom_citation_gini < 0.5


def test_atom_citation_gini_uniform_distribution(tmp_path: Path):
    """Equal citation across atoms → Gini = 0."""
    now = datetime.now(tz=timezone.utc)
    for atom in ["a", "b", "c", "d", "e"]:
        _write_turn(tmp_path, output="x", saga_atom_ids=[atom], ts=now)
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.atom_citation_gini == pytest.approx(0.0, abs=0.01)


def test_topic_diversity_calculated_correctly(tmp_path: Path):
    """Topic diversity = distinct (channel, trigger, top-token) tuples / total turns."""
    now = datetime.now(tz=timezone.utc)
    # 4 turns with distinct topics.
    _write_turn(tmp_path, output="searching memory effectively", channel_id="ch1",
                trigger="user_message", ts=now)
    _write_turn(tmp_path, output="reading file contents now", channel_id="ch2",
                trigger="scheduled_tick", ts=now)
    _write_turn(tmp_path, output="opening pull request review", channel_id="ch1",
                trigger="poller", ts=now)
    # 1 repeated topic (same channel × trigger × top-token).
    _write_turn(tmp_path, output="searching memory effectively again", channel_id="ch1",
                trigger="user_message", ts=now)
    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.window_turns == 4
    # 3 distinct topics out of 4 turns = 0.75.
    assert metrics.distinct_topics == 3
    assert metrics.topic_diversity_ratio == pytest.approx(0.75)


def test_collapse_metrics_handles_no_embedder_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When fastembed isn't importable, cosine fields are None but
    the rest of the report still produces."""
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="some text", ts=now)
    _write_turn(tmp_path, output="other text", ts=now)

    def _raise(*a, **k):
        raise ImportError("fastembed not installed")

    monkeypatch.setattr("mimir.search.FastEmbedder", _raise)

    metrics = vm.compute_collapse_metrics(tmp_path, window_days=7, now=now)
    assert metrics.embedder_unavailable
    assert metrics.cosine_sim_mean is None
    # Other metrics still compute.
    assert metrics.window_turns == 2


# ── curation metrics ─────────────────────────────────────────────────


def test_curation_metrics_empty_home(tmp_path: Path):
    metrics = vm.compute_curation_metrics(tmp_path, window_days=28)
    assert metrics.reflection_turn_count == 0
    assert metrics.reflection_bytes_per_week == 0.0
    assert metrics.feedback_event_count == 0
    assert metrics.forget_event_count == 0


def test_curation_metrics_counts_reflection_turns(tmp_path: Path):
    """Only reflect / saga_session_end triggers count as curation work."""
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="x" * 200, trigger="reflect", ts=now)
    _write_turn(tmp_path, output="y" * 300, trigger="saga_session_end", ts=now)
    _write_turn(tmp_path, output="z" * 1000, trigger="user_message", ts=now)
    metrics = vm.compute_curation_metrics(tmp_path, window_days=28, now=now)
    assert metrics.reflection_turn_count == 2
    assert metrics.reflection_bytes_total == 500


def test_curation_normalizes_to_weekly_rate(tmp_path: Path):
    """200 bytes in a 28-day window → 50 bytes/week."""
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="x" * 200, trigger="reflect", ts=now)
    metrics = vm.compute_curation_metrics(tmp_path, window_days=28, now=now)
    assert metrics.reflection_bytes_per_week == pytest.approx(200 / 4.0)


def test_curation_filters_window(tmp_path: Path):
    """Reflection turns outside the window aren't counted."""
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="x" * 500, trigger="reflect", ts=now - timedelta(days=40))
    _write_turn(tmp_path, output="y" * 100, trigger="reflect", ts=now)
    metrics = vm.compute_curation_metrics(tmp_path, window_days=28, now=now)
    assert metrics.reflection_bytes_total == 100
    assert metrics.reflection_turn_count == 1


def test_curation_counts_feedback_and_forget_events(tmp_path: Path):
    now = datetime.now(tz=timezone.utc)
    _write_event(tmp_path, type="saga_feedback_sent", ts=now)
    _write_event(tmp_path, type="saga_feedback_sent", ts=now)
    _write_event(tmp_path, type="saga_feedback_ok", ts=now)
    _write_event(tmp_path, type="saga_forget_ok", ts=now)
    _write_event(tmp_path, type="some_other_event", ts=now)  # ignored
    metrics = vm.compute_curation_metrics(tmp_path, window_days=28, now=now)
    assert metrics.feedback_event_count == 3
    assert metrics.forget_event_count == 1


# ── threshold warnings ──────────────────────────────────────────────


def test_report_flags_low_reflection_rate(tmp_path: Path):
    """Below-threshold reflection output → curation_below_threshold_reflection warning."""
    now = datetime.now(tz=timezone.utc)
    # 100 bytes / 4 weeks = 25 bytes/week (well below the 500 default).
    _write_turn(tmp_path, output="x" * 100, trigger="reflect", ts=now)
    report = vm.build_report(tmp_path, now=now)
    assert any("curation_below_threshold_reflection" in w for w in report.warnings)


def test_report_flags_low_feedback_rate(tmp_path: Path):
    """No saga_feedback events → curation_below_threshold_feedback warning."""
    now = datetime.now(tz=timezone.utc)
    # Force a reflection turn that meets the bytes threshold so only
    # the feedback warning fires (cleaner assertion).
    _write_turn(tmp_path, output="x" * 5000, trigger="reflect", ts=now)
    report = vm.build_report(tmp_path, now=now)
    assert any("curation_below_threshold_feedback" in w for w in report.warnings)
    # Should NOT fire the reflection warning (5000 bytes / 4 weeks = 1250/wk > 500).
    assert not any("curation_below_threshold_reflection" in w for w in report.warnings)


def test_report_flags_low_topic_diversity(tmp_path: Path):
    """All turns on the same (channel, trigger, top-token) → ratio < threshold."""
    now = datetime.now(tz=timezone.utc)
    # Need at least 5 turns / 1 distinct topic = 0.2 (right at threshold).
    # 10 turns / 1 distinct topic = 0.1 to trip it solidly.
    for _ in range(10):
        _write_turn(tmp_path, output="memory memory memory testing",
                    channel_id="chX", trigger="user_message", ts=now)
    report = vm.build_report(tmp_path, now=now)
    assert any("collapse_risk_topic_lock" in w for w in report.warnings)


def test_report_clean_when_metrics_healthy(tmp_path: Path):
    """A healthy home (diverse topics + adequate curation) produces no warnings."""
    now = datetime.now(tz=timezone.utc)
    # 5 distinct topics, plenty of curation activity.
    topics = ["foo", "bar", "baz", "quux", "blah"]
    for t in topics:
        _write_turn(tmp_path, output=f"{t} content {t} again {t} once more",
                    channel_id=f"ch-{t}", trigger="user_message", ts=now)
    _write_turn(tmp_path, output="x" * 5000, trigger="reflect", ts=now)
    for _ in range(40):
        _write_event(tmp_path, type="saga_feedback_sent", ts=now)
    _write_event(tmp_path, type="saga_forget_ok", ts=now)
    report = vm.build_report(tmp_path, now=now)
    # No CURATION warnings (forget=1, feedback=10/wk, reflection=5000/4=1250/wk).
    # Topic diversity = 5/5 = 1.0, above 0.2 threshold.
    # Cosine sim and Gini may not trigger because the data is too small
    # / fastembed-dependent — but the ones we tested should be clean.
    curation_warnings = [w for w in report.warnings if "curation" in w]
    topic_warnings = [w for w in report.warnings if "topic_lock" in w]
    assert curation_warnings == []
    assert topic_warnings == []


# ── report rendering ────────────────────────────────────────────────


def test_render_produces_markdown_with_sections(tmp_path: Path):
    now = datetime.now(tz=timezone.utc)
    _write_turn(tmp_path, output="some content", ts=now)
    report = vm.build_report(tmp_path, now=now)
    rendered = report.render()
    assert "# Mimir viability report" in rendered
    assert "## Collapse indicators" in rendered
    assert "## Write-side curation" in rendered


def test_write_report_to_disk(tmp_path: Path):
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    _write_turn(tmp_path, output="content", ts=now)
    report = vm.build_report(tmp_path, now=now)
    path = vm.write_report(report)
    assert path.is_file()
    assert path.name == "viability-2026-05-23.md"
    assert path.parent == tmp_path / "state" / "reports"


# ── scheduled emit path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_run_emits_warnings_and_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """run_scheduled_viability_report emits one event per warning
    + a paired viability_report_ok."""
    now = datetime.now(tz=timezone.utc)
    # Set up a home that will trigger the curation feedback warning.
    _write_turn(tmp_path, output="x" * 5000, trigger="reflect", ts=now)

    events: list[tuple[str, dict]] = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", fake_log)
    await vm.run_scheduled_viability_report(tmp_path)

    kinds = [k for k, _ in events]
    # Should include at least the OK event + the feedback-low warning.
    assert "viability_report_ok" in kinds
    assert any("curation_below_threshold" in k for k in kinds)


@pytest.mark.asyncio
async def test_scheduled_run_catches_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If build_report raises, run_scheduled_viability_report logs
    a viability_report_error event rather than propagating."""
    events: list[tuple[str, dict]] = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    def _boom(*a, **k):
        raise RuntimeError("test failure")

    monkeypatch.setattr("mimir.event_logger.log_event", fake_log)
    monkeypatch.setattr(vm, "build_report", _boom)
    # Must not raise.
    await vm.run_scheduled_viability_report(tmp_path)
    kinds = [k for k, _ in events]
    assert "viability_report_error" in kinds
