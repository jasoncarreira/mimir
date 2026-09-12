from __future__ import annotations

from pathlib import Path


DOC = Path(__file__).resolve().parent.parent / "docs" / "authorization.md"


def _section(text: str, heading: str) -> str:
    marker = f"### {heading}"
    assert text.count(marker) == 1, f"expected one {marker!r} section"
    remainder = text.split(marker, 1)[1]
    return remainder.split("\n### ", 1)[0]


def test_mid_turn_injection_documents_principal_not_audience_authority() -> None:
    section = _section(DOC.read_text(encoding="utf-8"), "Mid-turn injection")
    for phrase in (
        "frozen `AuthContext`", "Same-principal aliases", "unknown identities",
        "shadow mode", "fresh turn", "FIFO", "IFC `authorized_principals`",
        "before consent is", "different operator cannot inject",
    ):
        assert phrase in section


def test_contained_execution_surfaces_are_not_listed_as_agent_user() -> None:
    text = DOC.read_text(encoding="utf-8")
    residual = _section(text, "Surfaces that still execute as the agent user")

    assert "`repo_test`" not in residual
    assert "`spawn_open_code`" not in residual
    assert "Feature-factory runs" not in residual
    factory = _section(text, "Worklink And Factory Runs")
    assert "`launch_factory`" in factory
    assert "no agent-user fallback" in factory


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
