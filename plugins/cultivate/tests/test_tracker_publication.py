"""Tracker publication: Chainlink id scrub, tagging line and builder-facing inputs."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mimir_cultivate.tracker.record import ExternalRecord, render_external_record
from mimir_cultivate.worklink import orchestrator
from mimir_cultivate.worklink.evidence import TestResult, WorklinkEvidence

ID = 9137
REMOTE = "git@github.com:acme/app.git"
LEAF = (
    "Goal: x\n\nAcceptance criteria:\n- [ ] do it\n\nReview criteria:\n- ok\n\n"
    "Worklink notes:\n- Scope: s\n- Out of scope: o\n- Suggested test command: echo ok\n"
)
GITHUB = ExternalRecord("github_issues", "acme/app#5", "https://github.com/acme/app/issues/5")
OTHER = ExternalRecord("github_issues", "other/repo#5", "https://github.com/other/repo/issues/5")
LINEAR = ExternalRecord("linear", "ENG-123", "https://linear.app/t/issue/ENG-123")
JIRA = ExternalRecord("jira", "PROJ-45", "https://example.atlassian.net/browse/PROJ-45")
PR_URL = "https://github.com/acme/app/pull/4"


def _evidence(tmp_path: Path) -> WorklinkEvidence:
    now = datetime.now(UTC).isoformat()
    return WorklinkEvidence(
        issue=ID, attempt=1, backend="opencode", branch=f"issue/{ID}-a1",
        checkout=str(tmp_path), started_at=now, finished_at=now,
        files_changed=["change.txt"], diff_stat="1 file changed", commands=[],
        tests=TestResult(cmd="./tools/verify", exit_code=0, gate_run_id="gate-77"),
        pr_url=None, status="completed", model="openai/test-model", transcript=None,
    )


def _open(
    tmp_path: Path,
    *,
    description: str = LEAF,
    title: str = "Fix the parser",
    issue_id: int = ID,
    subject: str | None = None,
    section: str | None = None,
    remote: str | None = REMOTE,
) -> tuple[str, str]:
    def runner(args: list[str]):
        if args[:4] == ["git", "-C", str(tmp_path), "config"]:
            return subprocess.CompletedProcess(args, 0 if remote else 1, (remote or "") + "\n", "")
        return subprocess.CompletedProcess(args, 0, PR_URL + "\n", "")

    calls: list[list[str]] = []

    def recording(args: list[str]):
        calls.append(args)
        return runner(args)

    orchestrator._open_pr(
        tmp_path, orchestrator.IssueContext(issue_id, title, description, set()),
        f"issue/{issue_id}-a1", _evidence(tmp_path), base="main", runner=recording,
        pr_body_section=section, subject=subject,
    )
    command = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    return command[command.index("--title") + 1], command[command.index("--body") + 1]


def _described(record: ExternalRecord) -> str:
    return f"{LEAF}\n{render_external_record(record)}"


@pytest.mark.parametrize(("subject", "expected"), [
    ("Chainlink issue #9137 parser", "the source issue parser"),
    ("Chainlink issue 9137 parser", "the source issue parser"),
    ("Work on issue/9137-a2 now", "Work on the work branch now"),
    ("Handle task 9137 edge", "Handle the source issue edge"),
    ("Handle epic #9137 edge", "Handle the source issue edge"),
    ("Fixes #9137", "Fixes the source issue"),
    ("Closes # 9137 now", "Closes the source issue now"),
    ("Cleanup 9137 leftovers", "Cleanup the source issue leftovers"),
    ("chainlink-9137 thing", "thing"),
    ("Fixes #12", "Refs #12"),
    ("Handle 91370 items", "Handle 91370 items"),
])
def test_title_scrubs_id_forms(tmp_path: Path, subject: str, expected: str) -> None:
    title, body = _open(tmp_path, subject=subject)
    assert title == expected
    assert "9137" not in body.replace("gate-77", "")


def test_small_id_keeps_bare_numeral(tmp_path: Path) -> None:
    title, _ = _open(tmp_path, issue_id=42, subject="Handle 42 items, task 42 and #42")
    assert title == "Handle 42 items, the source issue and the source issue"


def test_scrub_runs_before_defang() -> None:
    assert orchestrator._publishable_text("Fixes #9137", ID) == "Fixes the source issue"
    assert orchestrator._publishable_text("Fixes #9137 and Fixes #12", ID) == (
        "Fixes the source issue and Refs #12"
    )


def test_jira_title_prefix(tmp_path: Path) -> None:
    title, _ = _open(tmp_path, description=_described(JIRA), subject="Fix the parser")
    assert title == "PROJ-45: Fix the parser"


def test_title_has_no_prefix_for_other_adapters(tmp_path: Path) -> None:
    for record in (GITHUB, LINEAR):
        title, _ = _open(tmp_path, description=_described(record), subject="Fix the parser")
        assert title == "Fix the parser"


def test_builder_prose_scrubs_work_order_phrase_and_branch(tmp_path: Path) -> None:
    prose = (
        f"You are executing Chainlink issue #{ID}. Branch issue/{ID}-a1 holds it.\n"
        f"See #{ID}, Fixes #{ID}, chainlink-{ID} and bare {ID}. Also fixes #12."
    )
    title, body = _open(tmp_path, section=prose)
    assert str(ID) not in title
    summary = body.split("Worklink evidence:")[0]
    assert str(ID) not in summary
    assert "You are executing the source issue." in summary
    assert "the work branch" in summary
    assert "Fixes the source issue" in summary
    assert "fixes #12" not in summary and "Refs #12" in summary


def test_small_id_prose_keeps_bare_numeral(tmp_path: Path) -> None:
    _, body = _open(tmp_path, issue_id=42, section="Ran 42 checks for issue #42.")
    assert "Ran 42 checks for the source issue." in body


@pytest.mark.parametrize(("record", "line"), [
    (GITHUB, "Closes #5"),
    (OTHER, "Closes other/repo#5"),
    (LINEAR, "Fixes ENG-123"),
    (JIRA, "Fixes PROJ-45"),
])
def test_tagging_line_per_adapter(tmp_path: Path, record: ExternalRecord, line: str) -> None:
    _, body = _open(tmp_path, description=_described(record))
    assert body.startswith(f"{line}\n\n")
    assert body.count(line) == 1


def test_tagging_line_unknown_repo_uses_full_ref(tmp_path: Path) -> None:
    _, body = _open(tmp_path, description=_described(GITHUB), remote=None)
    assert body.startswith("Closes acme/app#5\n\n")


def test_tagging_line_exempt_from_id_scrub(tmp_path: Path) -> None:
    _, body = _open(tmp_path, description=_described(GITHUB), issue_id=5, subject="Fix it")
    assert body.startswith("Closes #5\n\n")


@pytest.mark.parametrize("description", [
    LEAF,
    f"{LEAF}\nExternal: bogus",
    f"{LEAF}\nExternal: linear:ENG-1 not-a-url",
    f"{LEAF}\n{render_external_record(LINEAR)}\n{render_external_record(JIRA)}",
])
def test_no_or_malformed_record_adds_no_tagging_line(
    tmp_path: Path, description: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(orchestrator, "_log_event", lambda name, **kw: events.append((name, kw)))
    title, body = _open(tmp_path, description=description, subject="Fix it")
    assert title == "Fix it"
    assert body.startswith("Worklink evidence:")
    invalid = [kw for name, kw in events if name == "worklink_external_record_invalid"]
    assert invalid == ([] if description == LEAF else [{"issue_id": ID}])


def test_ci_body_edit_preserves_tagging_line(tmp_path: Path) -> None:
    _, body = _open(tmp_path, description=_described(LINEAR))
    edits: list[list[str]] = []

    def runner(args: list[str]):
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"body": body}), "")
        edits.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    orchestrator._update_ci_pr_body(runner, PR_URL, (("tests", "success"),), 1, "a" * 40)
    updated = edits[0][edits[0].index("--body") + 1]
    assert updated.startswith("Fixes ENG-123\n\n")
    assert "- CI repair rounds: 1" in updated


@pytest.mark.parametrize(("written", "expected"), [
    ("Chainlink issue 9137 parser", "the source issue parser"),
    ("Work on issue/9137-a2", "Work on the work branch"),
    ("Handle task 9137 edge", "Handle the source issue edge"),
    ("Fixes 9137 now", "Fixes the source issue now"),
    ("Handle 91370 items", "Handle 91370 items"),
])
def test_commit_subject_scrubs_id_forms(tmp_path: Path, written: str, expected: str) -> None:
    (tmp_path / ".worklink-commit-subject").write_text(written + "\n")
    subject = orchestrator._read_commit_subject(tmp_path, "Fallback title", issue_id=ID)
    assert subject == expected


def test_commit_subject_is_recapped_after_scrub(tmp_path: Path) -> None:
    written = "x" * 60 + " 9137 tail"
    (tmp_path / ".worklink-commit-subject").write_text(written + "\n")
    subject = orchestrator._read_commit_subject(tmp_path, "Fallback title", issue_id=ID)
    assert len(subject) <= 72
    assert "9137" not in subject


def test_commit_subject_rejection_falls_back_to_scrubbed_title(tmp_path: Path) -> None:
    (tmp_path / ".worklink-commit-subject").write_text("Contains #12\n")
    subject = orchestrator._read_commit_subject(tmp_path, "Chainlink issue 9137 parser", issue_id=ID)
    assert subject == "the source issue parser"


@pytest.mark.parametrize(("title", "expected"), [
    ("Chainlink issue 9137 parser", "the source issue parser"),
    ("Branch issue/9137-a3 cleanup", "Branch the work branch cleanup"),
    ("Handle epic 9137 edge", "Handle the source issue edge"),
    ("Cleanup 9137 leftovers", "Cleanup the source issue leftovers"),
])
def test_fallback_subject_scrubs_id_forms(title: str, expected: str) -> None:
    assert orchestrator._fallback_commit_subject(title, issue_id=ID) == expected


def test_fallback_subject_without_id_is_unchanged() -> None:
    assert orchestrator._fallback_commit_subject("Cleanup 9137 leftovers") == "Cleanup 9137 leftovers"


def test_pr_title_from_chainlink_title_is_scrubbed(tmp_path: Path) -> None:
    title, _ = _open(tmp_path, title="Chainlink issue 9137 parser")
    assert title == "the source issue parser"


def test_builder_inputs_strip_external_record() -> None:
    record = render_external_record(LINEAR)
    issue = orchestrator.IssueContext(700, "epic", f"{LEAF}\n{record}", {"worklink:epic"})
    assert issue.description.endswith(record)
    work_item = json.loads(orchestrator.render_work_item(issue))
    assert "External:" not in work_item["body"]
    assert "Goal: x" in work_item["body"]
    assert "External:" not in orchestrator._epic_prompt(issue)
    assert "Goal: x" in orchestrator._epic_prompt(issue)


def test_work_order_strips_external_record(tmp_path: Path) -> None:
    template = tmp_path / "order.md"
    template.write_text("#{issue_id} {title}\n{description}\n{backend} {test_command} {labels} {parent_id}\n")
    issue = orchestrator.IssueContext(700, "leaf", _described(JIRA), set())
    rendered = orchestrator.render_work_order(
        issue, template_path=template, backend_name="fake", test_command="echo ok",
    )
    assert "External:" not in rendered
    assert "PROJ-45" not in rendered
    assert "Goal: x" in rendered


def test_filed_leaf_with_record_validates() -> None:
    issue = orchestrator.IssueContext(700, "leaf", _described(LINEAR), {"worklink:ready"})
    orchestrator.validate_leaf(issue)
    assert orchestrator.parse_external_record(issue.description) == LINEAR
