"""Keep the shipped Worklink leaf contract synchronized with its validator."""

from __future__ import annotations

import re
from pathlib import Path

from mimir.worklink.planning import _REQUIRED_SECTIONS


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "code-building-pipeline.md"


def test_documented_leaf_markers_match_validator() -> None:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(
        r"validator marker contract.*?```text\n(?P<markers>.*?)\n```",
        text,
        flags=re.DOTALL,
    )

    assert match is not None, "operator guide is missing its validator marker block"
    documented = tuple(match.group("markers").splitlines())
    assert documented == _REQUIRED_SECTIONS


def test_documented_leaf_contract_includes_column_zero_checklist() -> None:
    text = DOC.read_text(encoding="utf-8")

    assert "beginning at column 0" in text
    assert "form `- [ ] `" in text
