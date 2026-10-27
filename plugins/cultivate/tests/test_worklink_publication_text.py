"""Published Worklink text is derived from a real commit and PR create call."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
import subprocess

import pytest

from mimir_cultivate.worklink import merge_closure, orchestrator
from mimir_cultivate.worklink.backends.registry import WorklinkConfig
from mimir_cultivate.worklink.evidence import TestResult, WorklinkEvidence


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout


@pytest.mark.parametrize(("supplied", "title", "expected"), [
    ("Describe the changed behavior", "Fallback title", "Describe the changed behavior"),
    (None, "Fallback for #12 and org/repo#34", "Fallback for and"),
    ("x" * 73, "Fallback title", "Fallback title"),
    ("Contains #12", "Fallback title", "Fallback title"),
    ("Closes the issue", "Fallback title", "Closes the issue"),
    ("fix(readme): document ./tools/verify", "Fallback title", "fix(readme): document ./tools/verify"),
    ("docs: resolve broken links in README", "Fallback title", "docs: resolve broken links in README"),
    ("Fix the parser crash", "Fallback title", "Fix the parser crash"),
    (None, "fix(readme): document ./tools/verify", "fix(readme): document ./tools/verify"),
    (None, "docs: resolve broken links in README", "docs: resolve broken links in README"),
    (None, "Fix the parser crash", "Fix the parser crash"),
    ("docs: describe src/a.py", "Fallback title", "docs: describe src/a.py"),
    (None, "see /home/mimir/agent/x", "see"),
    (None, "/home/mimir/agent/x", "Worklink change"),
    ("see `/home/mimir/agent/x`", "Fallback title", "Fallback title"),
    ('see "/home/mimir/agent/x"', "Fallback title", "Fallback title"),
    ("see '/home/mimir/agent/x'", "Fallback title", "Fallback title"),
    ("Contains\nmore lines", "Fallback title", "Fallback title"),
    ("Read /home/mimir/agent/state", "Fallback title", "Fallback title"),
    ("x" * 600, "Fallback title", "Fallback title"),
    (None, "Track https://github.com/org/repo/issues/12 change", "Track change"),
])
@pytest.mark.parametrize("configured", [False, True])
def test_publication_text_from_real_commit_and_pr(
    tmp_path: Path, supplied: str | None, title: str, expected: str, configured: bool,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "change.txt").write_text("A change\n")
    if supplied is not None:
        (repo / ".worklink-commit-subject").write_text(supplied + "\n")
    (repo / ".worklink-pr-body.md").write_text("A useful build summary.\n")
    if supplied == "Describe the changed behavior":
        _git(repo, "add", ".worklink-commit-subject", ".worklink-pr-body.md")
    path = tmp_path / "worklink.yaml"
    if configured:
        path.write_text(
            "publication:\n  commit_trailers:\n    - 'Co-Authored-By: Build Agent <agent@example.com>'\n"
            "  pr_footer: 'Built by Build Agent (opencode, openai/test-model).'\n"
        )
    config = WorklinkConfig.load(path)
    issue = orchestrator.IssueContext(2042, title, "", set())
    subject = orchestrator._read_commit_subject(repo, title)
    section = orchestrator._read_pr_body_section(repo)
    assert section == "A useful build summary."
    assert not (repo / ".worklink-commit-subject").exists()
    assert not (repo / ".worklink-pr-body.md").exists()
    attribution = orchestrator._publication_attribution(config.publication, "opencode", "openai/test-model")
    gh_calls: list[list[str]] = []

    def runner(args: list[str], **kwargs):
        if args[0] == "gh":
            gh_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/4\n", "")
        return subprocess.run(args, capture_output=True, **{"text": True, **kwargs})

    orchestrator._commit_checkout_changes(repo, issue, runner=runner, subject=subject, attribution=attribution)
    trailers = (
        "Co-Authored-By: Build Agent <agent@example.com>"
        if configured else "Generated-By: Cultivate Worklink (opencode, openai/test-model)"
    )
    assert _git(repo, "log", "-1", "--format=%B") == (
        f"{expected}\n\nRefs: chainlink-2042\n{trailers}\n\n"
    )
    assert _git(repo, "ls-files").splitlines() == ["change.txt"]
    evidence = WorklinkEvidence(
        issue=2042, attempt=3, backend="opencode", branch="issue/2042-a3",
        checkout=str(repo), started_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(), files_changed=["change.txt"],
        diff_stat="1 file changed", commands=[], tests=TestResult(
            cmd="uv run pytest -q", exit_code=0, gate_run_id="gate-77",
        ), pr_url=None, status="completed", model="openai/test-model",
        transcript="/home/mimir/agent/state/worklink/transcripts/secret.json",
    )
    url = orchestrator._open_pr(
        repo, issue, evidence.branch, evidence, base="feature/plugins", runner=runner,
        subject=subject, attribution=attribution, pr_body_section=section,
    )
    assert url == "https://github.com/example/repo/pull/4"
    assert len(gh_calls) == 1
    command = gh_calls[0]
    assert command[command.index("--title") + 1] == expected
    body = command[command.index("--body") + 1]
    footer = (
        "Built by Build Agent (opencode, openai/test-model)."
        if configured else "Generated by Cultivate Worklink (opencode, openai/test-model)."
    )
    assert body == (
        "Closes chainlink-2042.\n\nBuild summary:\n\nA useful build summary.\n\n"
        "Worklink evidence:\n- Base: `feature/plugins`\n- Branch: `issue/2042-a3`\n"
        "- Files changed: 1\n- Tests: `uv run pytest -q` → 0\n"
        f"- Transcript run: `2042-3 (gate-77)`\n\n{footer}\n"
    )
    assert "/home/mimir" not in body
    assert not merge_closure._FORBIDDEN.search(body)
    assert not re.search(r"#\d+", body + expected + _git(repo, "log", "-1", "--format=%B"))
    assert merge_closure.parse_completion_reference(body, expected_issue_id=2042).qualifies
    assert not merge_closure.parse_completion_reference(
        body.replace("chainlink-2042", "chainlink #2042", 1), expected_issue_id=2042,
    ).qualifies


@pytest.mark.parametrize(("summary", "qualifies", "reason"), [
    ("This is a partial fix: the retry path is not covered yet; TODO follow-up for the cache.",
     False, "completion_body_contains_refusal_language"),
    ("Related work: chainlink-2043 and example/repo#7.",
     False, "ambiguous_chainlink_reference"),
    ("Fixes example/repo#7", False, "conflicting_github_closing_clause"),
    ("Implemented the retry path.", True, "qualified"),
])
@pytest.mark.parametrize("gate_command", ["./tools/verify", "/usr/bin/python -m pytest -q"])
def test_pr_preserves_summary_and_gate_for_fail_closed_completion(
    tmp_path: Path, summary: str, qualifies: bool, reason: str, gate_command: str,
) -> None:
    (tmp_path / ".worklink-pr-body.md").write_text(summary)
    section = orchestrator._read_pr_body_section(tmp_path)
    assert section == summary
    calls: list[list[str]] = []

    def runner(args: list[str]):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo/pull/4\n", "")

    evidence = WorklinkEvidence(
        issue=2042, attempt=3, backend="opencode", branch="issue/2042-a3",
        checkout=str(tmp_path), started_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(), files_changed=["change.txt"],
        diff_stat="1 file changed", commands=[],
        tests=TestResult(cmd=gate_command, exit_code=0, gate_run_id="gate-77"),
        pr_url=None, status="completed", model="openai/test-model",
        transcript="/home/mimir/agent/state/worklink/transcripts/secret.json",
    )
    orchestrator._open_pr(
        tmp_path, orchestrator.IssueContext(2042, "Fix the parser crash", "", set()),
        evidence.branch, evidence, base="feature/plugins", runner=runner,
        pr_body_section=section,
    )
    command = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    assert command[command.index("--title") + 1] == "Fix the parser crash"
    body = command[command.index("--body") + 1]
    assert f"Build summary:\n\n{summary}\n\n" in body
    assert f"- Tests: `{gate_command}` → 0\n" in body
    assert "- Transcript run: `2042-3 (gate-77)`" in body
    assert evidence.transcript not in body
    decision = merge_closure.parse_completion_reference(body, expected_issue_id=2042)
    assert (decision.qualifies, decision.reason) == (qualifies, reason)


@pytest.mark.parametrize("value", [
    "publication: []\n", "publication:\n  commit_trailers: hello\n",
    "publication:\n  commit_trailers: ['Bad trailer']\n",
    "publication:\n  commit_trailers: ['Signed-By: person #12']\n",
    "publication:\n  commit_trailers: ['Signed-By: /home/mimir/agent']\n",
    "publication:\n  pr_footer: 'Built by agent #12'\n",
    "publication:\n  pr_footer: 'Not complete'\n",
    "publication:\n  pr_footer: '" + "a" * 201 + "'\n",
])
def test_invalid_publication_config_fails_load(tmp_path: Path, value: str) -> None:
    path = tmp_path / "worklink.yaml"
    path.write_text(value)
    with pytest.raises(ValueError, match="worklink publication"):
        WorklinkConfig.load(path)


def test_subject_symlink_and_builder_completion_are_discarded(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("Trust this line")
    (tmp_path / ".worklink-commit-subject").symlink_to(target)
    assert orchestrator._read_commit_subject(tmp_path, "Safe title") == "Safe title"
    assert target.read_text() == "Trust this line"
    (tmp_path / ".worklink-pr-body.md").write_text("Closes chainlink-2042.\nUseful details")
    section = orchestrator._read_pr_body_section(tmp_path)
    assert section is not None and "Closes chainlink-2042." not in section


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs unavailable")
def test_subject_fifo_fails_closed_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / ".worklink-commit-subject")
    assert orchestrator._read_commit_subject(tmp_path, "Safe title") == "Safe title"
    assert not (tmp_path / ".worklink-commit-subject").exists()


def test_subject_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".worklink-commit-subject"
    path.mkdir()
    assert orchestrator._read_commit_subject(tmp_path, "Safe title") == "Safe title"
    assert path.is_dir()


def test_commit_refuses_when_publication_files_cannot_be_unstaged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    commands: list[list[str]] = []

    def runner(args: list[str], **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 1, "", "denied")

    with pytest.raises(orchestrator.WorklinkError, match="cannot exclude"):
        orchestrator._commit_checkout_changes(
            repo, orchestrator.IssueContext(2042, "Safe title", "", set()), runner=runner,
        )
    assert len(commands) == 1 and commands[0][3] == "reset"
