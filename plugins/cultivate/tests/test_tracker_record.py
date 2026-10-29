from __future__ import annotations

import pytest

from mimir_cultivate.tracker.record import (
    ExternalRecord,
    ExternalRecordError,
    parse_external_record,
    strip_external_record,
    tagging_line,
    title_prefix,
)

GITHUB = ExternalRecord("github_issues", "acme/app#12", "https://github.com/acme/app/issues/12")
LINEAR = ExternalRecord("linear", "ENG-123", "https://linear.app/acme/issue/ENG-123")
JIRA = ExternalRecord("jira", "PROJ-45", "https://acme.atlassian.net/browse/PROJ-45")


@pytest.mark.parametrize("record", [GITHUB, LINEAR, JIRA])
def test_render_then_parse_round_trips(record: ExternalRecord) -> None:
    description = f"Goal:\nDo it.\n\n{record.render_external_record()}\n"

    assert parse_external_record(description) == record
    assert record.external_ref() == f"{record.adapter}:{record.ref}"


def test_description_without_a_line_has_no_record() -> None:
    assert parse_external_record("Goal:\nNothing external here.\n") is None


@pytest.mark.parametrize(
    "line",
    [
        "External: github_issues:Acme/app#12 https://github.com/acme/app/issues/12",
        "External: github_issues:acme/app#0 https://github.com/acme/app/issues/0",
        "External: linear:eng-1 https://linear.app/a/issue/eng-1",
        "External: linear:ENG-0 https://linear.app/a/issue/ENG-0",
        "External: jira:P-1 https://x.atlassian.net/browse/P-1",
        "External: gitlab:ENG-1 https://example.com/ENG-1",
        "External: linear:ENG-1 http://linear.app/a/issue/ENG-1",
        "External: linear:ENG-1 https://linear.app/a/issue/ENG-1 trailing",
        "External:linear:ENG-1 https://linear.app/a/issue/ENG-1",
        "  External: linear:ENG-1 https://linear.app/a/issue/ENG-1",
        "External: linear:ENG-1",
    ],
)
def test_grammar_failure_raises(line: str) -> None:
    with pytest.raises(ExternalRecordError):
        parse_external_record(f"Goal:\n{line}\n")


def test_more_than_one_line_raises_even_when_identical() -> None:
    line = LINEAR.render_external_record()

    with pytest.raises(ExternalRecordError):
        parse_external_record(f"{line}\n{line}\n")
    with pytest.raises(ExternalRecordError):
        parse_external_record(f"{line}\n{JIRA.render_external_record()}\n")


def test_strip_removes_every_external_line_and_trailing_blanks() -> None:
    text = (
        "Goal:\nDo it.\n\n"
        f"{LINEAR.render_external_record()}\n"
        "  External: anything at all\n\n"
    )

    assert strip_external_record(text) == "Goal:\nDo it."


def test_strip_keeps_lines_around_the_record_in_order() -> None:
    text = f"first\n{JIRA.render_external_record()}\nlast\n"

    assert strip_external_record(text) == "first\nlast"


def test_strip_returns_record_free_text_unchanged() -> None:
    text = "Goal:\n\nline with External: in the middle\n\n\n"

    assert strip_external_record(text) is text


@pytest.mark.parametrize(
    ("record", "slug", "expected"),
    [
        (GITHUB, "acme/app", "Closes #12"),
        (GITHUB, "ACME/App", "Closes #12"),
        (GITHUB, "acme/other", "Closes acme/app#12"),
        (GITHUB, None, "Closes acme/app#12"),
        (LINEAR, "acme/app", "Fixes ENG-123"),
        (JIRA, "acme/app", "Fixes PROJ-45"),
    ],
)
def test_tagging_line_per_adapter(
    record: ExternalRecord, slug: str | None, expected: str
) -> None:
    assert tagging_line(record, slug) == expected


def test_title_prefix_only_for_jira() -> None:
    assert title_prefix(JIRA) == "PROJ-45: "
    assert title_prefix(LINEAR) == ""
    assert title_prefix(GITHUB) == ""
