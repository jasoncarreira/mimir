from __future__ import annotations

from pathlib import Path


DOC = Path(__file__).resolve().parent.parent / "docs" / "authorization.md"


def _section(text: str, heading: str) -> str:
    marker = f"### {heading}"
    assert text.count(marker) == 1, f"expected one {marker!r} section"
    remainder = text.split(marker, 1)[1]
    return remainder.split("\n### ", 1)[0]


def test_contained_execution_surfaces_are_not_listed_as_agent_user() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "### Surfaces that still execute as the agent user" not in text
    worklink = _section(text, "Worklink Runs")
    assert "Feature-factory fresh and recovery workloads" in worklink
    assert "`launch_factory`" in worklink
    assert "irreversible `setresuid` drop" in worklink
    assert "detached descendants inherit that identity" in worklink
    assert "does not add descendant lifetime" in worklink


def test_contained_execution_surface_claims_are_bounded() -> None:
    text = DOC.read_text(encoding="utf-8")
    contained = _section(text, "Contained repository-code execution")
    normalized = contained.lower()

    assert "`repo_test`" in contained
    assert "`spawn_open_code`" in contained
    for phrase in (
        "disposable snapshot",
        "fresh server-issued checkout",
        "projection bytes",
        "fail closed",
        "relative artifact handle",
        "separate trusted action",
    ):
        assert phrase in normalized
    assert "feature_factory" not in normalized
    assert "feature-factory" not in normalized
    assert "general sandbox" not in normalized
