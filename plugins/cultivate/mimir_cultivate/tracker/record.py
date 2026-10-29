"""External record: the line that ties a Chainlink issue to its tracker issue.

This is a leaf module (stdlib only) so publication code can import it at module
level without an import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REF_PATTERNS = {
    "github_issues": re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+#[1-9]\d*"),
    "linear": re.compile(r"[A-Z][A-Z0-9]{0,9}-[1-9]\d*"),
    "jira": re.compile(r"[A-Z][A-Z0-9_]{1,9}-[1-9]\d*"),
}

_LINE = re.compile(r"External: (github_issues|linear|jira):(\S+) (https://\S+)")
_PREFIX = "External:"


class ExternalRecordError(ValueError):
    """The description holds an invalid or duplicated External line."""


@dataclass(frozen=True)
class ExternalRecord:
    adapter: str
    ref: str
    url: str

    def external_ref(self) -> str:
        return f"{self.adapter}:{self.ref}"

    def render_external_record(self) -> str:
        return f"External: {self.adapter}:{self.ref} {self.url}"


def external_ref(record: ExternalRecord) -> str:
    return record.external_ref()


def render_external_record(record: ExternalRecord) -> str:
    return record.render_external_record()


def _is_record_line(line: str) -> bool:
    return line.lstrip().startswith(_PREFIX)


def parse_external_record(description: str) -> ExternalRecord | None:
    """Return the record in ``description``, or None when it has no External line."""

    lines = [line for line in description.splitlines() if _is_record_line(line)]
    if not lines:
        return None
    if len(lines) > 1:
        raise ExternalRecordError("more than one External line")
    match = _LINE.fullmatch(lines[0])
    if match is None:
        raise ExternalRecordError("External line does not match the record grammar")
    adapter, ref, url = match.groups()
    if REF_PATTERNS[adapter].fullmatch(ref) is None:
        raise ExternalRecordError(f"External ref is not a valid {adapter} ref")
    return ExternalRecord(adapter, ref, url)


def strip_external_record(text: str) -> str:
    """Drop every External line; text without one is returned unchanged."""

    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not _is_record_line(line)]
    if len(kept) == len(lines):
        return text
    return "".join(kept).rstrip()


def tagging_line(record: ExternalRecord, pr_repo_slug: str | None) -> str:
    """Return the PR-body line that links the pull request to the tracker issue."""

    if record.adapter == "github_issues":
        repo, _, number = record.ref.partition("#")
        if pr_repo_slug is not None and repo.casefold() == pr_repo_slug.casefold():
            return f"Closes #{number}"
        return f"Closes {record.ref}"
    return f"Fixes {record.ref}"


def title_prefix(record: ExternalRecord) -> str:
    """Return the PR-title prefix; only Jira keys are prefixed."""

    return f"{record.ref}: " if record.adapter == "jira" else ""
