"""Per-skill refine/retire candidates in the introspection report (chainlink #267).

The reflection turn reads these and authors operator-gated refine/retire
proposed-changes. Drives `_build_skill_health` by mocking its three inputs
(skill_outcomes success-rate, negative-learning count, installed inventory)
rather than fabricating turns.jsonl + a saga db.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from mimir.reflection.introspection_report import (
    Report,
    SkillHealth,
    _build_skill_health,
    render_markdown,
)
from mimir.skill_outcomes import SkillOutcome

NOW = datetime(2026, 5, 29, tzinfo=timezone.utc)


def _patch(monkeypatch, *, outcomes=None, installed=None, negatives=None):
    import mimir.skill_defs as sd
    import mimir.skill_memory as sm
    import mimir.skill_outcomes as so

    monkeypatch.setattr(so, "aggregate", lambda *a, **k: dict(outcomes or {}))
    monkeypatch.setattr(so, "load_skill_success_criteria", lambda home: {})
    monkeypatch.setattr(sd, "installed_skill_names", lambda home: list(installed or []))
    neg = negatives or {}
    monkeypatch.setattr(
        sm, "count_negative_learnings",
        lambda conn, skill, **k: int(neg.get(skill, 0)),
    )


def _run(tmp_path, skill_counts, *, saga_conn=None):
    return _build_skill_health(
        home=tmp_path, turns_log=tmp_path / "turns.jsonl",
        days=7, now=NOW, skill_counts=Counter(skill_counts), saga_conn=saga_conn,
    )


def test_refine_on_low_success_rate(tmp_path, monkeypatch):
    _patch(monkeypatch, outcomes={"flaky": SkillOutcome(skill="flaky", success=1, failure=4)})
    out = _run(tmp_path, {"flaky": 5})
    assert len(out) == 1
    sh = out[0]
    assert sh.skill == "flaky" and sh.refine_candidate and not sh.retire_candidate
    assert "success rate 20%" in "; ".join(sh.reasons)


def test_low_rate_below_min_runs_not_flagged(tmp_path, monkeypatch):
    # 0% but only 2 runs (< MIN_RUNS=3) — not enough signal to trust.
    _patch(monkeypatch, outcomes={"new": SkillOutcome(skill="new", failure=2)})
    assert _run(tmp_path, {"new": 2}) == []


def test_refine_on_negative_learnings(tmp_path, monkeypatch):
    # Healthy success rate, but 3 negative learnings → refine candidate.
    _patch(
        monkeypatch,
        outcomes={"memory": SkillOutcome(skill="memory", success=10)},
        negatives={"memory": 3},
    )
    out = _run(tmp_path, {"memory": 10}, saga_conn=object())
    sh = next(s for s in out if s.skill == "memory")
    assert sh.refine_candidate
    assert "3 negative learning(s)" in "; ".join(sh.reasons)


def test_negatives_ignored_without_conn(tmp_path, monkeypatch):
    # Same negatives, but no saga conn → that input is dropped, skill healthy.
    _patch(
        monkeypatch,
        outcomes={"memory": SkillOutcome(skill="memory", success=10)},
        negatives={"memory": 9},
    )
    assert _run(tmp_path, {"memory": 10}) == []


def test_retire_on_zero_usage(tmp_path, monkeypatch):
    _patch(monkeypatch, installed=["dormant"])
    out = _run(tmp_path, {})
    sh = next(s for s in out if s.skill == "dormant")
    assert sh.retire_candidate and not sh.refine_candidate
    assert "no usage" in "; ".join(sh.reasons)


def test_used_and_healthy_not_flagged(tmp_path, monkeypatch):
    _patch(
        monkeypatch,
        installed=["active"],
        outcomes={"active": SkillOutcome(skill="active", success=5)},
    )
    assert _run(tmp_path, {"active": 5}) == []


def test_no_home_returns_empty(tmp_path):
    assert _build_skill_health(
        home=None, turns_log=tmp_path / "t.jsonl", days=7, now=NOW,
        skill_counts=Counter(),
    ) == []


def test_render_includes_candidates_section():
    report = Report(days=7, generated_at=NOW, skill_health=[
        SkillHealth(
            skill="flaky", invocations=5, success_rate=0.2, runs=5,
            negative_learnings=0, refine_candidate=True, retire_candidate=False,
            reasons=["success rate 20% over 5 run(s)"],
        ),
    ])
    md = render_markdown(report)
    assert "## Skill refine/retire candidates" in md
    assert "flaky" in md and "refine" in md


def test_render_omits_section_when_empty():
    md = render_markdown(Report(days=7, generated_at=NOW))
    assert "Skill refine/retire candidates" not in md
