"""Planning contract shared by the Worklink decomposer and executor."""

from __future__ import annotations

import re
from datetime import UTC, datetime

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

# Slice 2 tightened the executor/planner contract. Earlier Chainlink issues were
# not authored with the Worklink-notes sections, so strict refusal would orphan
# already queued leaves. New issues are strict; pre-contract issues are
# advisory-warned so they can drain or be migrated in normal planning work.
STRICT_VALIDATION_CREATED_AFTER = datetime(2026, 6, 12, tzinfo=UTC)


def render_decompose_prompt(template: str, **values: object) -> str:
    """Render the planner prompt with the canonical leaf template injected."""

    rendered = template.replace("{leaf_template}", LEAF_TEMPLATE_MARKDOWN)
    if not values:
        return rendered
    return rendered.format(**values)


def missing_leaf_template_parts(description: str) -> list[str]:
    """Return planner-template parts absent from a candidate Worklink leaf."""

    missing: list[str] = []
    for section in _REQUIRED_SECTIONS:
        if section.lower() not in description.lower():
            missing.append(section.rstrip(":"))
    if not re.search(r"(?m)^- \[[ xX]\] ", description):
        missing.append("acceptance checklist item")
    return missing


def uses_strict_leaf_validation(created_at: datetime | None) -> bool:
    """Return whether missing planner-template parts are fatal for a leaf."""

    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at >= STRICT_VALIDATION_CREATED_AFTER


def _strip_markdown_command_delimiters(value: str) -> str:
    """Remove common Markdown wrappers from a single-line command."""

    value = value.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value.strip("`").strip()
    return value


def suggested_test_command(description: str) -> str | None:
    """Extract the Worklink planner's suggested test command, if present."""

    match = re.search(r"(?im)^- Suggested test command:\s*(.+?)\s*$", description)
    if not match:
        return None
    value = _strip_markdown_command_delimiters(match.group(1))
    if not value or value.casefold() in {"<command the executor should run>", "n/a", "none"}:
        return None
    return value
