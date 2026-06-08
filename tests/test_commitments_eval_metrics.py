"""Reference-free metrics + adapter for the commitments GEPA pilot (#404, Path A)."""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.commitments_extraction import adapter, metrics


# ── artifact_ids ─────────────────────────────────────────────────────


def test_artifact_ids_numbers_paths_and_commit_ids():
    ids = metrics.artifact_ids(
        "Merge PR #199 and chainlink #147; fix skill_outcomes.py (c-feeb930cfc)"
    )
    assert ids == {"#199", "#147", "skill_outcomes.py", "c-feeb930cfc"}


def test_artifact_ids_normalizes_pr_hash_forms():
    assert metrics.artifact_ids("PR #604") == metrics.artifact_ids("see #604")


def test_artifact_ids_no_false_positive_on_abbreviations():
    assert metrics.artifact_ids("meet at 14:00, e.g. tomorrow") == set()


# ── per-text quality via score_extraction ────────────────────────────


def test_self_contained_text_scores_high():
    src = "Review and merge PR #199 (skill_outcomes.py streaming fix) before the release."
    ev = metrics.score_extraction(
        src,
        ["Review & merge PR #199 (skill_outcomes.py streaming tool_result fix) before release"],
        baseline_count=1,
    )
    assert ev.score == pytest.approx(1.0)
    assert ev.texts[0].retained_ids == {"#199", "skill_outcomes.py"}
    assert not ev.texts[0].hallucinated_ids


def test_over_compressed_is_penalized():
    ev = metrics.score_extraction("Fix the FTS5 cherry-pick on main.", ["Fix FTS5"], baseline_count=1)
    assert ev.texts[0].over_compressed
    assert ev.score == pytest.approx(0.5)  # 1.0 - 0.5, no count penalty
    assert "OVER-COMPRESSED" in ev.asi


def test_hallucinated_id_is_strongly_penalized():
    src = "Cleaned up the dispatcher; nothing references a PR here."
    ev = metrics.score_extraction(
        src, ["Merge PR #999 to main once the review passes later today"], baseline_count=1
    )
    assert ev.texts[0].hallucinated_ids == {"#999"}
    assert ev.score == pytest.approx(0.2)  # 1.0 - 0.8
    assert "HALLUCINATED" in ev.asi


def test_over_long_is_penalized():
    src = "Do the thing with PR #1."
    long_text = "x" * 130
    ev = metrics.score_extraction(src, [long_text], baseline_count=1)
    assert ev.texts[0].over_long
    assert ev.score == pytest.approx(0.7)  # 1.0 - 0.3


# ── volume anchor (anti-Goodhart) ────────────────────────────────────


def test_extracting_nothing_when_baseline_found_some_scores_zero():
    ev = metrics.score_extraction("Source mentions PR #1 and PR #2.", [], baseline_count=2)
    assert ev.score == pytest.approx(0.0)
    assert "MISS" in ev.asi


def test_extracting_nothing_when_baseline_also_nothing_is_fine():
    ev = metrics.score_extraction("Pure status, nothing actionable.", [], baseline_count=0)
    assert ev.score == pytest.approx(1.0)


def test_over_extraction_relative_to_baseline_is_penalized():
    src = "Two follow-ups: merge PR #1 and merge PR #2."
    # 4 perfectly fine texts but baseline only found 2 → volume penalty applies.
    texts = [f"Merge PR #{n} to main once CI is green and the review is in" for n in (1, 2, 3, 4)]
    ev = metrics.score_extraction(src, texts, baseline_count=2)
    assert ev.count_penalty > 0
    assert ev.score < 1.0
    assert "VOLUME" in ev.asi


def test_score_always_in_unit_interval():
    src = "PR #1 here."
    for texts, bc in (([], 5), (["x" * 200], 1), (["hi"], 0), (["ok " * 30], 3)):
        ev = metrics.score_extraction(src, texts, baseline_count=bc)
        assert 0.0 <= ev.score <= 1.0


# ── aggregate ────────────────────────────────────────────────────────


def test_aggregate_reports_rubric_rates():
    src = "Merge PR #1 and PR #2 and PR #3."
    evals = [
        metrics.score_extraction(src, ["Merge PR #1 once review is in and CI passes today"], baseline_count=1),
        metrics.score_extraction(src, ["too short"], baseline_count=1),
    ]
    agg = metrics.aggregate(evals)
    assert 0.0 <= agg["mean_score"] <= 1.0
    assert agg["over_compressed_rate"] == pytest.approx(0.5)  # 1 of 2 texts
    assert agg["avg_commitments_per_example"] == pytest.approx(1.0)


# ── corpus + adapter ─────────────────────────────────────────────────


def test_corpus_loads_and_splits():
    train = adapter.load_corpus(split="train")
    holdout = adapter.load_corpus(split="holdout")
    assert train and holdout
    assert all(e.split == "train" for e in train)
    assert all(e.split == "holdout" for e in holdout)
    assert all(len(e.source_text) >= 100 for e in train + holdout)  # extractor MIN_OUTPUT_LEN


def test_adapter_evaluate_and_reflective_dataset():
    pytest.importorskip("gepa")

    examples = [
        adapter.Example(id="a", split="train", source_text="Merge PR #1 once review is in."),
        adapter.Example(id="b", split="train", source_text="Nothing actionable this cycle."),
    ]
    canned = {
        "Merge PR #1 once review is in.": ["Merge PR #1 to main once the review is in and CI passes"],
        "Nothing actionable this cycle.": [],
    }

    async def stub_extract(system: str, source: str) -> list[str]:
        return canned[source]

    ad = adapter.CommitmentsAdapter(examples, {"a": 1, "b": 0}, stub_extract)
    batch = examples
    result = ad.evaluate(batch, {adapter.COMPONENT_SYSTEM: "SYS"}, capture_traces=True)

    assert len(result.scores) == 2
    assert result.scores[0] == pytest.approx(1.0)  # self-contained
    assert result.scores[1] == pytest.approx(1.0)  # empty + baseline 0 → fine
    assert result.trajectories is not None and len(result.trajectories) == 2

    reflective = ad.make_reflective_dataset(
        {adapter.COMPONENT_SYSTEM: "SYS"}, result, [adapter.COMPONENT_SYSTEM]
    )
    recs = reflective[adapter.COMPONENT_SYSTEM]
    assert len(recs) == 2
    assert all("Feedback" in r and r["Feedback"] for r in recs)


def test_adapter_evaluate_no_traces_omits_trajectories():
    pytest.importorskip("gepa")
    ex = [adapter.Example(id="a", split="train", source_text="Merge PR #1 once review is in.")]

    async def stub(system, source):
        return ["Merge PR #1 to main after the review is in and CI is green"]

    ad = adapter.CommitmentsAdapter(ex, {"a": 1}, stub)
    result = ad.evaluate(ex, {adapter.COMPONENT_SYSTEM: "SYS"}, capture_traces=False)
    assert result.trajectories is None
    assert len(result.scores) == 1


def test_make_extract_fn_returns_async_callable():
    # No model call — just that the factory builds an awaitable-returning fn.
    fn = adapter.make_extract_fn()
    assert asyncio.iscoroutinefunction(fn)


# ── load_turns_corpus (real in-home turns; never committed) ──────────


def _write_turns(home, rows):
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "turns.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )


def test_load_turns_corpus_filters_trigger_and_length(tmp_path):
    long_out = "Worked on PR #1 today. " * 8  # > 100 chars
    _write_turns(
        tmp_path,
        [
            {"turn_id": "t1", "trigger": "saga_session_end", "output": long_out},
            {"turn_id": "t2", "trigger": "user_message", "output": long_out},  # wrong trigger
            {"turn_id": "t3", "trigger": "saga_session_end", "output": "too short"},  # < 100
            {"turn_id": "t4", "trigger": "saga_session_end", "output": long_out},
            {"turn_id": "t5", "trigger": "saga_session_end"},  # no output
        ],
    )
    ex = adapter.load_turns_corpus(tmp_path)
    assert {e.id for e in ex} == {"t1", "t4"}
    assert all(e.source_text == long_out for e in ex)
    assert all("never committed" in e.notes for e in ex)


def test_load_turns_corpus_bucketing_stable_and_splits_partition(tmp_path):
    out = "x" * 150
    _write_turns(
        tmp_path,
        [{"turn_id": f"id{i}", "trigger": "saga_session_end", "output": out} for i in range(20)],
    )
    a = {e.id: e.split for e in adapter.load_turns_corpus(tmp_path)}
    b = {e.id: e.split for e in adapter.load_turns_corpus(tmp_path)}
    assert a == b  # deterministic, process-stable
    train = adapter.load_turns_corpus(tmp_path, split="train")
    holdout = adapter.load_turns_corpus(tmp_path, split="holdout")
    assert {e.id for e in train}.isdisjoint({e.id for e in holdout})
    assert len(train) + len(holdout) == 20


def test_load_turns_corpus_limit_takes_most_recent(tmp_path):
    out = "y" * 150
    _write_turns(
        tmp_path,
        [{"turn_id": f"r{i}", "trigger": "saga_session_end", "output": out} for i in range(10)],
    )
    ex = adapter.load_turns_corpus(tmp_path, limit=3)
    assert {e.id for e in ex} == {"r7", "r8", "r9"}
