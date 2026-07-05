"""Tests for the feature-factory epic driver (chainlink #834).

The external ``feature-factory`` CLI is simulated by a stateful fake runner that
mutates ``run.json`` on each ``factory start``/``resume`` — so the driver's gate
loop is exercised end-to-end deterministically without a real factory. Mirrors
the runner-injection + CompletedProcess mocking style of
``test_worklink_orchestrator.py`` / ``test_spawn_opencode.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mimir.worklink.claims import ClaimRecord, ClaimResult
from mimir.worklink.factory import (
    FactoryEpicRunner,
    FactoryReview,
    FactoryReviewContext,
    _parse_review,
    factory_bin_from_env,
)
from mimir.worklink.orchestrator import WorklinkError

ISSUE_ID = 834
FACTORY_BIN = ("feature-factory",)


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _issue_json() -> str:
    return json.dumps(
        {
            "id": ISSUE_ID,
            "title": "Epic: chat skills",
            "description": "Build the chat skills feature.",
            "labels": ["worklink:epic", "worklink:ready"],
            "comments": [],
        }
    )


class FakeClaims:
    """Injected claims collaborator: records transitions, always claims."""

    def __init__(self, *, claimed: bool = True) -> None:
        self.transitions: list[tuple[int, str, bool, str | None]] = []
        self.released: list[int] = []
        record = ClaimRecord(
            issue_id=ISSUE_ID, attempt=1, agent_id="mimir-factory", claimed_at=datetime.now(UTC)
        )
        self._result = ClaimResult(claimed=claimed, record=record if claimed else None,
                                   reason=None if claimed else "cap reached")

    def claim_issue(self, issue_id: int, *args, **kwargs) -> ClaimResult:
        return self._result

    def heartbeat_issue(self, record: ClaimRecord) -> ClaimRecord:
        return record

    def transition_issue(self, issue_id, *, status, review_ready, attempt=None, reason=None) -> None:
        self.transitions.append((issue_id, status, review_ready, reason))

    def release_issue(self, issue_id: int) -> None:
        self.released.append(issue_id)


class FakeFactoryCLI:
    """Simulates ``feature-factory``: advances run.json on each start/resume."""

    def __init__(self, repo: Path, run_id: str) -> None:
        self.repo = repo
        self.run_id = run_id
        self.run_dir = repo / ".opencode" / "factory" / run_id
        self.gates_dir = self.run_dir / "gates"
        self.answers: list[tuple[str, str]] = []
        self.gh_calls: list[list[str]] = []
        self.launches = 0
        self.pr_url = "https://github.com/o/r/pull/42"
        self._mtime = 1000

    # -- run.json / question authoring -------------------------------------
    def _write_run(self, gates: dict, *, status: str = "needs-human", pr_url: str | None = None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload: dict = {"run_id": self.run_id, "status": status, "gates": gates}
        if pr_url is not None:
            payload["pr_url"] = pr_url
        (self.run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")

    def _write_question(self, gate: str) -> None:
        self.gates_dir.mkdir(parents=True, exist_ok=True)
        q = self.gates_dir / f"{gate}.question.md"
        q.write_text(f"gate {gate}", encoding="utf-8")
        self._mtime += 1000  # distinct, increasing mtimes so re-opened gates differ
        os.utime(q, (self._mtime, self._mtime))

    def _advance(self) -> None:
        self.launches += 1
        story_done = ("story", "approve") in self.answers
        brief_done = ("brief", "approve") in self.answers
        pre = [a for a in self.answers if a[0] == "pre_pr"]
        base = {"story": {"status": "approved"}, "brief": {"status": "approved"}}
        if not story_done:
            self._write_run({"story": {"status": "pending"}})
            self._write_question("story")
        elif not brief_done:
            self._write_run({"story": {"status": "approved"}, "brief": {"status": "pending"}})
            self._write_question("brief")
        elif not pre:
            self._write_run({**base, "pre_pr": {"status": "pending"}})
            self._write_question("pre_pr")
        elif pre[-1][1] == "approve":
            self._write_run({**base, "pre_pr": {"status": "approved"}}, status="done", pr_url=self.pr_url)
        elif pre[-1][1].startswith("changes"):
            self._write_run({**base, "pre_pr": {"status": "pending"}})
            self._write_question("pre_pr")  # re-open with newer mtime
        else:  # stop / unknown → terminal without PR
            self._write_run(base, status="blocked")

    # -- the injectable runner ---------------------------------------------
    def runner(self, argv):
        argv = list(argv)
        if argv[:3] == ["chainlink", "issue", "show"]:
            return _cp(stdout=_issue_json())
        if "factory" in argv and "start" in argv:
            self._advance()
            return _cp()
        if "factory" in argv and "answer" in argv:
            i = argv.index("answer")
            self.answers.append((argv[i + 2], argv[i + 3]))
            return _cp()
        if argv[:2] == ["gh", "pr"]:
            self.gh_calls.append(argv)
            return _cp()
        return _cp()  # git config / anything else


def _scripted_reviewer(*verdicts: str):
    calls = {"n": 0}
    seq = list(verdicts)

    def review(ctx: FactoryReviewContext) -> FactoryReview:
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return FactoryReview(verdict=v, rationale=f"because {v}")

    review.calls = calls  # type: ignore[attr-defined]
    return review


def _runner_for(tmp_path: Path, *, reviewer=None, claims=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake = FakeFactoryCLI(repo, f"chainlink-{ISSUE_ID}")
    claims = claims or FakeClaims()
    runner = FactoryEpicRunner(
        home=tmp_path / "home",
        repo=repo,
        factory_bin=FACTORY_BIN,
        runner=fake.runner,
        reviewer=reviewer or _scripted_reviewer("no_concerns"),
        claims=claims,
    )
    return runner, fake, claims


# ── pure helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("no_concerns", "approve"),
        ("nits", "approve"),
        ("approve", "approve"),
        ("important", "changes:"),
        ("changes", "changes:"),
        ("blocker", "stop"),
        ("stop", "stop"),
    ],
)
def test_review_verdict_maps_to_gate_answer(verdict, expected):
    answer = FactoryReview(verdict=verdict, rationale="x").gate_answer
    if expected == "changes:":
        assert answer.startswith("changes:")
    else:
        assert answer == expected


def test_parse_review_reads_final_json_line():
    text = "some reasoning\nmore\n{\"verdict\": \"nits\", \"rationale\": \"minor\"}"
    review = _parse_review(text)
    assert review.verdict == "nits"
    assert review.rationale == "minor"


def test_parse_review_fails_safe_to_changes_when_unparseable():
    review = _parse_review("no json here, just prose")
    assert review.gate_answer.startswith("changes:")
    assert review.verdict == "important"


def test_factory_bin_from_env(monkeypatch):
    monkeypatch.delenv("MIMIR_FEATURE_FACTORY_BIN", raising=False)
    assert factory_bin_from_env() == ("feature-factory",)
    monkeypatch.setenv("MIMIR_FEATURE_FACTORY_BIN", "node /opt/ff/cli.js")
    assert factory_bin_from_env() == ("node", "/opt/ff/cli.js")


# ── driver gate loop ────────────────────────────────────────────────────────

async def test_happy_path_auto_approves_gates_reviews_pre_pr_and_opens_pr(tmp_path):
    runner, fake, claims = _runner_for(tmp_path, reviewer=_scripted_reviewer("no_concerns"))
    result = await runner.run(ISSUE_ID, autonomous=True)

    assert result.status == "review_ready"
    assert result.review_ready is True
    assert result.pr_url == fake.pr_url
    # story + brief auto-approved; pre_pr approved after review
    assert fake.answers == [("story", "approve"), ("brief", "approve"), ("pre_pr", "approve")]
    # PR promoted to ready + mimir reviewer requested
    assert any(c[:3] == ["gh", "pr", "ready"] for c in fake.gh_calls)
    assert any("--add-reviewer" in c for c in fake.gh_calls)
    # epic moved to review, claim released
    assert (ISSUE_ID, "review", True, None) in claims.transitions
    assert claims.released == [ISSUE_ID]


async def test_pre_pr_changes_triggers_factory_loop_then_approves(tmp_path):
    # First review requests changes, second approves — mirrors the live #783 run.
    reviewer = _scripted_reviewer("important", "no_concerns")
    runner, fake, claims = _runner_for(tmp_path, reviewer=reviewer)
    result = await runner.run(ISSUE_ID, autonomous=True)

    assert result.status == "review_ready"
    pre_pr_answers = [a for a in fake.answers if a[0] == "pre_pr"]
    assert pre_pr_answers[0][1].startswith("changes:")
    assert pre_pr_answers[1][1] == "approve"
    assert reviewer.calls["n"] == 2  # re-reviewed after the changes loop


async def test_pre_pr_blocker_stops_run_and_blocks_epic(tmp_path):
    runner, fake, claims = _runner_for(tmp_path, reviewer=_scripted_reviewer("blocker"))
    result = await runner.run(ISSUE_ID, autonomous=True)

    assert result.status == "blocked"
    assert ("pre_pr", "stop") in fake.answers
    assert not any(c[:3] == ["gh", "pr", "ready"] for c in fake.gh_calls)  # no PR promotion
    assert any(t[1] == "blocked" for t in claims.transitions)
    assert claims.released == [ISSUE_ID]


async def test_claim_declined_returns_refused_without_launching(tmp_path):
    claims = FakeClaims(claimed=False)
    runner, fake, _ = _runner_for(tmp_path, claims=claims)
    result = await runner.run(ISSUE_ID, autonomous=True)

    assert result.status == "refused"
    assert fake.launches == 0
    assert fake.answers == []


async def test_terminal_without_pr_url_marks_epic_blocked(tmp_path):
    # Factory reaches a terminal state but never produced a PR (e.g. push blocked).
    runner, fake, claims = _runner_for(tmp_path)

    # Override advance: after brief, jump straight to a blocked terminal (no gate, no pr_url).
    orig = fake._advance

    def advance_to_blocked():
        orig()
        if ("brief", "approve") in fake.answers:
            fake._write_run(
                {"story": {"status": "approved"}, "brief": {"status": "approved"}},
                status="blocked",
            )
            (fake.run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": fake.run_id,
                        "status": "blocked",
                        "gates": {"story": {"status": "approved"}, "brief": {"status": "approved"}},
                        "blocked_reason": "push denied: 403",
                    }
                ),
                encoding="utf-8",
            )

    fake._advance = advance_to_blocked  # type: ignore[method-assign]
    result = await runner.run(ISSUE_ID, autonomous=True)

    assert result.status == "blocked"
    assert result.pr_url is None
    assert any("403" in (t[3] or "") for t in claims.transitions)


async def test_missing_run_json_fails_the_epic(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    claims = FakeClaims()

    def runner_fn(argv):
        argv = list(argv)
        if argv[:3] == ["chainlink", "issue", "show"]:
            return _cp(stdout=_issue_json())
        return _cp()  # factory start writes nothing → no run.json

    runner = FactoryEpicRunner(
        home=tmp_path / "home", repo=repo, factory_bin=FACTORY_BIN,
        runner=runner_fn, reviewer=_scripted_reviewer("no_concerns"), claims=claims,
    )
    result = await runner.run(ISSUE_ID, autonomous=True)
    assert result.status == "failed"
    assert claims.released == [ISSUE_ID]  # claim always released


async def test_launch_and_answer_argv_target_the_repo(tmp_path):
    runner, fake, _ = _runner_for(tmp_path)
    captured: list[list[str]] = []
    inner = fake.runner

    def recording(argv):
        captured.append(list(argv))
        return inner(argv)

    runner = FactoryEpicRunner(
        home=tmp_path / "home", repo=fake.repo, factory_bin=FACTORY_BIN,
        runner=recording, reviewer=_scripted_reviewer("no_concerns"), claims=FakeClaims(),
    )
    await runner.run(ISSUE_ID, autonomous=True)

    starts = [a for a in captured if "start" in a and "factory" in a]
    assert starts, "factory start was never invoked"
    for argv in starts:
        assert "--headless" in argv
        assert "--repo" in argv and str(fake.repo) in argv
    answers = [a for a in captured if "answer" in a and "factory" in a]
    for argv in answers:
        assert argv[:2] == ["feature-factory", "factory"]
        assert "--repo" in argv


# ── poller routing (chainlink #834) ─────────────────────────────────────────

def _load_poller():
    path = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "optional-skills" / "chainlink-orchestrator" / "poller.py"
    )
    name = "_cl_poller_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # register so @dataclass can resolve __module__
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_dispatch_item_command_routes_epic_to_factory():
    poller = _load_poller()
    assert poller.DispatchItem(issue_id=1, mode="leaf").command == "run"
    assert poller.DispatchItem(issue_id=2, mode="epic").command == "factory"


def test_factory_epics_enabled_flag(monkeypatch):
    poller = _load_poller()
    monkeypatch.delenv("MIMIR_FACTORY_EPICS_ENABLED", raising=False)
    assert poller._factory_epics_enabled() is False
    monkeypatch.setenv("MIMIR_FACTORY_EPICS_ENABLED", "true")
    assert poller._factory_epics_enabled() is True
    monkeypatch.setenv("MIMIR_FACTORY_EPICS_ENABLED", "0")
    assert poller._factory_epics_enabled() is False
