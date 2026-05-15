"""Scoring-logic tests for chainlink #140 (Sub B)."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.file_search_autopass_ab.score import (
    ProbeResult,
    extract_metrics_from_turn,
    load_results,
    render_markdown,
    summarise,
    write_results,
)


# ----------------------------------------------------------------------
# extract_metrics_from_turn
# ----------------------------------------------------------------------


def test_extract_metrics_counts_file_search_tool_calls():
    rec = {
        "channel_id": "bench-fsap-on-001",
        "duration_ms": 1234,
        "total_cost_usd": 0.0125,
        "output": "answered using memory/issues/foo.md",
        "events": [
            {"type": "tool_call", "name": "mcp__mimir__file_search"},
            {"type": "tool_call", "name": "mcp__mimir__file_search"},
            {"type": "tool_call", "name": "Read"},
            {"type": "tool_call", "name": "Grep"},
            {"type": "tool_call", "name": "Glob"},
            {"type": "tool_call", "name": "Bash"},
            {"type": "reasoning", "content": "thinking…"},
        ],
    }
    m = extract_metrics_from_turn(rec, expected_target="memory/issues/foo.md")
    assert m["file_search_count"] == 2
    assert m["grep_glob_count"] == 2
    assert m["read_count"] == 1
    assert m["total_tool_calls"] == 6
    assert m["duration_ms"] == 1234
    assert m["total_cost_usd"] == 0.0125
    assert m["hit"] is True
    assert m["reply"] == "answered using memory/issues/foo.md"


def test_extract_metrics_hit_is_case_insensitive():
    rec = {
        "duration_ms": 0,
        "output": "See MEMORY/Core/30-Reflection-Policy.md for details.",
        "events": [],
    }
    m = extract_metrics_from_turn(
        rec, expected_target="memory/core/30-reflection-policy.md",
    )
    assert m["hit"] is True


def test_extract_metrics_miss():
    rec = {
        "duration_ms": 0,
        "output": "I'm not sure where that lives.",
        "events": [],
    }
    m = extract_metrics_from_turn(rec, expected_target="memory/core/00-identity.md")
    assert m["hit"] is False


def test_extract_metrics_empty_output_returns_none_hit():
    rec = {"duration_ms": 0, "output": "", "events": []}
    m = extract_metrics_from_turn(rec, expected_target="anything")
    assert m["hit"] is None


# ----------------------------------------------------------------------
# round-trip + summarise
# ----------------------------------------------------------------------


def _make_result(idx: int, arm: str, *,
                 file_search=0, total=0, hit=True, duration=100, cost=0.001,
                 shape="fingerprinted-error",
                 expected_target="memory/issues/foo.md") -> ProbeResult:
    return ProbeResult(
        probe_index=idx,
        probe_text=f"probe {idx}",
        expected_target=expected_target,
        shape=shape,
        arm=arm,
        file_search_count=file_search,
        grep_glob_count=0,
        read_count=0,
        total_tool_calls=total,
        duration_ms=duration,
        total_cost_usd=cost,
        hit=hit,
        reply="…",
    )


def test_write_results_round_trips(tmp_path: Path):
    out = tmp_path / "arm.jsonl"
    write_results(out, [_make_result(1, "on"), _make_result(2, "on", hit=False)])
    rows = load_results(out)
    assert len(rows) == 2
    assert rows[0].probe_index == 1
    assert rows[0].hit is True
    assert rows[1].hit is False


def test_summarise_reports_means_and_hit_rates():
    on = [
        _make_result(1, "on", file_search=0, total=3, hit=True, duration=1000, cost=0.01),
        _make_result(2, "on", file_search=1, total=4, hit=True, duration=1200, cost=0.012),
        _make_result(3, "on", file_search=0, total=2, hit=False, duration=900, cost=0.008),
    ]
    off = [
        _make_result(1, "off", file_search=2, total=6, hit=False, duration=1500, cost=0.015),
        _make_result(2, "off", file_search=2, total=7, hit=True, duration=1700, cost=0.018),
        _make_result(3, "off", file_search=1, total=5, hit=False, duration=1400, cost=0.013),
    ]
    s = summarise(on, off)
    by_name = {m.name: m for m in s.metrics}
    # Autopass-on should show lower file_search calls.
    assert by_name["file_search tool calls"].on_mean < by_name["file_search tool calls"].off_mean
    # Total-tool delta negative (autopass-on cuts tool calls).
    assert by_name["total tool calls"].delta_mean < 0
    # Hit rates.
    assert s.on_hit_rate == 2 / 3
    assert s.off_hit_rate == 1 / 3
    # Per-probe table matches probe indices.
    assert [r["probe_index"] for r in s.per_probe] == [1, 2, 3]


def test_summarise_handles_uncaptured_probes_without_crashing():
    on = [
        _make_result(1, "on"),
        ProbeResult(
            probe_index=2, probe_text="x", expected_target="y",
            shape="procedural", arm="on", captured=False, error="boom",
        ),
    ]
    off = [_make_result(1, "off", hit=False), _make_result(2, "off")]
    s = summarise(on, off)
    # The uncaptured probe still appears in the per-probe table.
    indices = [r["probe_index"] for r in s.per_probe]
    assert 1 in indices and 2 in indices
    # Hit-rate computed only over captured.
    assert s.on_hit_rate == 1.0
    assert s.off_hit_rate == 0.5


# ----------------------------------------------------------------------
# render_markdown
# ----------------------------------------------------------------------


def test_render_markdown_emits_all_three_sections():
    on = [_make_result(i, "on", file_search=0, total=2, hit=True) for i in range(1, 4)]
    off = [_make_result(i, "off", file_search=2, total=5, hit=False) for i in range(1, 4)]
    s = summarise(on, off)
    md = render_markdown(s, run_tag="unittest")
    assert "Per-metric comparison" in md
    assert "Per-probe outcomes" in md
    assert "Recommendation" in md
    # Recommendation is one of the three documented values.
    for value in (
        "ship Sub A as-is, skip Sub C",
        "ship Sub A + proceed to Sub C",
        "don't ship",
    ):
        if value in md:
            break
    else:
        raise AssertionError(f"no documented recommendation in rendered md:\n{md}")


def test_render_markdown_picks_proceed_when_hit_rate_jumps():
    on = [_make_result(i, "on", total=2, hit=True) for i in range(1, 11)]
    off = [_make_result(i, "off", total=2, hit=False) for i in range(1, 11)]
    s = summarise(on, off)
    md = render_markdown(s, run_tag="t")
    assert "ship Sub A + proceed to Sub C" in md


def test_render_markdown_picks_dont_ship_when_no_lift_and_cost_up():
    on = [
        _make_result(i, "on", total=5, hit=False, cost=0.05, duration=2000)
        for i in range(1, 11)
    ]
    off = [
        _make_result(i, "off", total=5, hit=False, cost=0.01, duration=1000)
        for i in range(1, 11)
    ]
    s = summarise(on, off)
    md = render_markdown(s, run_tag="t")
    assert "don't ship" in md
