from __future__ import annotations

import pytest

from mimir.worklink.planning import (
    _REQUIRED_SECTIONS,
    missing_leaf_template_parts,
    target_branch_from_description,
)


DESCRIPTION = """Acceptance criteria:
- [ ] do it

Review criteria:
- reviewer checks it

Worklink notes:
- Scope: mimir/worklink
- Out of scope: unrelated code
- Suggested test command: uv run pytest -q
"""


def test_target_branch_is_optional_and_does_not_change_required_sections() -> None:
    required = (
        "Acceptance criteria:",
        "Review criteria:",
        "Worklink notes:",
        "- Scope:",
        "- Out of scope:",
        "- Suggested test command:",
    )

    assert _REQUIRED_SECTIONS == required
    assert missing_leaf_template_parts(DESCRIPTION) == []
    assert target_branch_from_description(DESCRIPTION) is None


def test_target_branch_is_read_only_from_worklink_notes() -> None:
    description = DESCRIPTION.replace(
        "- Suggested test command:",
        "- Target branch: feature/acp\n- Suggested test command:",
    )

    assert target_branch_from_description(description) == "feature/acp"
    assert target_branch_from_description("- Target branch: feature/acp\n" + DESCRIPTION) is None


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-upload-pack=evil",
        "feature acp",
        "feature/../main",
        "feature/.hidden",
        "feature/acp.lock",
        "feature/@{main",
        "feature/acp:touch-pwned",
        "refs/heads/feature/acp",
        "origin/feature/acp",
    ],
)
def test_target_branch_rejects_values_that_are_not_branch_names(branch: str) -> None:
    description = DESCRIPTION.replace(
        "- Suggested test command:",
        f"- Target branch: {branch}\n- Suggested test command:",
    )

    with pytest.raises(ValueError, match="target branch"):
        target_branch_from_description(description)
