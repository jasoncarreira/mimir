"""GEPA skill guardrails."""

from __future__ import annotations

from pathlib import Path

from mimir.skill_md import parse_frontmatter


SKILL_PATH = Path(__file__).parent.parent / "mimir" / "skills" / "gepa" / "SKILL.md"


def _body() -> str:
    return SKILL_PATH.read_text()


def test_gepa_skill_trigger_is_narrow() -> None:
    fm = parse_frontmatter(_body())
    desc = fm["description"]
    assert "bounded textual artifact" in desc
    assert "success can be measured" in desc
    assert "Do not use" in desc
    assert "governance" in desc


def test_gepa_skill_requires_asi_budget_and_adoption_gate() -> None:
    body = _body()
    for required in (
        "Actionable Side Information",
        "max_metric_calls",
        "adoption gate",
        "never auto-replaces",
        "holdout",
    ):
        assert required in body


def test_gepa_skill_examples_pin_first_pilot_and_anti_targets() -> None:
    body = _body()
    assert "Commitment extraction prompt" in body
    assert "mimir/commitments/extractor.py" in body
    assert "Weekly reflection as a whole" in body
    assert "Core identity / persona / values blocks" in body
