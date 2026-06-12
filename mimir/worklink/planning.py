"""Planning contract shared by the Worklink decomposer and executor."""

from __future__ import annotations

import re

LEAF_TEMPLATE_MARKDOWN = """Acceptance criteria:
- [ ] <observable, testable outcome>
- [ ] <focused validation command or evidence requirement>

Review criteria:
- <what a reviewer/operator should verify before approval>

Worklink notes:
- Scope: <files/subsystems expected to change, or \"docs only\">
- Out of scope: <nearby work not included in this leaf>
- Suggested test command: <command the executor should run>"""

_REQUIRED_SECTIONS = (
    "Acceptance criteria:",
    "Review criteria:",
    "Worklink notes:",
    "- Scope:",
    "- Out of scope:",
    "- Suggested test command:",
)


def missing_leaf_template_parts(description: str) -> list[str]:
    """Return planner-template parts absent from a candidate Worklink leaf."""

    missing: list[str] = []
    for section in _REQUIRED_SECTIONS:
        if section.lower() not in description.lower():
            missing.append(section.rstrip(":"))
    if not re.search(r"(?m)^- \[[ xX]\] ", description):
        missing.append("acceptance checklist item")
    return missing


def suggested_test_command(description: str) -> str | None:
    """Extract the Worklink planner's suggested test command, if present."""

    match = re.search(r"(?im)^- Suggested test command:\s*(.+?)\s*$", description)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value in {"<command the executor should run>", "n/a", "none"}:
        return None
    return value
