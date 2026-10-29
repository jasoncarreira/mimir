"""Backstop hooks in merge closure: queue locally under the flock, close from the drain."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from mimir_cultivate.tracker import drain as drain_module
from mimir_cultivate.tracker.adapters.base import ActionResult
from mimir_cultivate.tracker.config import TrackerAdapterConfig, TrackerConfig
from mimir_cultivate.tracker.drain import drain
from mimir_cultivate.tracker.evidence import evidence_path, read_evidence
from mimir_cultivate.worklink import merge_closure as closure
from mimir_cultivate.worklink.evidence import TestResult, WorklinkEvidence
from mimir_cultivate.worklink.orchestrator import _write_evidence
from tests.test_worklink_merge_closure import (  # noqa: F401  (autouse fixture)
    PR_URL,
    Tracker,
    configured_home,
    evidence,
    forge,
    unresolved_reasons,
    git_runner,
    selected_repository_provider,
)

EXT = "linear:ENG-1"
DESCRIPTION = "Goal: x\n\nExternal: linear:ENG-1 https://linear.app/acme/issue/ENG-1"
EPIC_LABELS = {"worklink:review", "worklink:epic"}


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, close: bool) -> tuple[Path, Path]:
    home, repo = configured_home(tmp_path, monkeypatch)
    section = {"close_on_merge": close, "adapters": {"linear": {"transitions": {}}}}
    (home / "worklink.yaml").write_text(
        yaml.safe_dump({"repository": "example/project", "tracker": section}), encoding="utf-8"
    )
    return home, repo


def _reconcile(home: Path, repo: Path, tracker: Tracker, issue_id: int, **forge_args):
    return closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(issue_id, **forge_args),
        git_runner=git_runner(repo),
    )


def _leaf(issue_id: int) -> Tracker:
    tracker = Tracker(issue_id)
    tracker.description = DESCRIPTION
    return tracker


def _epic(issue_id: int) -> Tracker:
    tracker = Tracker(issue_id, labels=set(EPIC_LABELS))
    tracker.description = DESCRIPTION
    return tracker


def _requests(home: Path) -> dict:
    return read_evidence(home)["items"]


@pytest.mark.parametrize("base", ["main", "feature/plugins"])
def test_off_zero_requests_leaf_default_and_feature_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=False)
    evidence(home, 31, base_ref=base)
    assert len(_reconcile(home, repo, _leaf(31), 31, base=base)) == 1
    assert not evidence_path(home).exists()


@pytest.mark.parametrize("base", ["main", "feature/plugins"])
def test_on_leaf_queues_request_and_still_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 32, base_ref=base)
    tracker = _leaf(32)
    assert len(_reconcile(home, repo, tracker, 32, base=base)) == 1
    assert tracker.status == "closed"
    entry = _requests(home)[EXT]["closure"]
    assert (entry["stage"], entry["chainlink_issue"], entry["pr_url"]) == (
        "requested", 32, PR_URL,
    )


def test_leaf_without_external_record_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 33)
    assert len(_reconcile(home, repo, Tracker(33), 33)) == 1
    assert not evidence_path(home).exists()


def test_request_failure_never_blocks_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 34)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(closure, "request_merge_closure", boom)
    tracker = _leaf(34)
    assert len(_reconcile(home, repo, tracker, 34)) == 1
    assert tracker.status == "closed"


def test_request_makes_no_external_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 35)

    def forbidden(*args, **kwargs):
        raise AssertionError("external adapter used inside the merge-closure flock")

    monkeypatch.setattr(drain_module, "build_adapter", forbidden)
    monkeypatch.setattr("mimir_cultivate.tracker.adapters.build_adapter", forbidden)
    assert len(_reconcile(home, repo, _leaf(35), 35)) == 1
    assert EXT in _requests(home)


def test_off_zero_requests_epic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=False)
    evidence(home, 36)
    assert _reconcile(home, repo, _epic(36), 36) == []
    assert not evidence_path(home).exists()


def test_on_epic_queues_only_once_merged_and_keeps_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 37)
    assert _reconcile(home, repo, _epic(37), 37, merged=False, state="open") == []
    assert not evidence_path(home).exists()
    assert _reconcile(home, repo, _epic(37), 37) == []
    assert _requests(home)[EXT]["closure"]["pr_url"] == PR_URL
    assert unresolved_reasons(home) == ["epic_not_leaf"]


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def current_state(self, ref: str) -> str | None:
        return "In Progress"

    def close(self, ref: str) -> ActionResult:
        self.calls.append(("close", ref))
        return ActionResult("applied")


def test_on_epic_closes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    _write_evidence(
        home,
        WorklinkEvidence(
            issue=38, attempt=1, backend="codex", branch="issue/38-a1",
            checkout=str(tmp_path / "checkout"), started_at="2026-09-20T10:00:00+00:00",
            finished_at="2026-09-20T10:05:00+00:00", files_changed=["mimir/example.py"],
            diff_stat="1 file changed", commands=[], tests=TestResult(cmd="pytest", exit_code=0),
            pr_url=PR_URL, status="completed",
        ),
    )
    _reconcile(home, repo, _epic(38), 38)
    _reconcile(home, repo, _epic(38), 38)
    adapter = _Adapter()
    monkeypatch.setattr(drain_module, "build_adapter", lambda *a, **k: adapter)
    config = TrackerConfig(True, {"linear": TrackerAdapterConfig(name="linear")})

    def gh(args):
        return forge(38)(args)

    for _ in range(3):
        drain(home, config, {}, object(), gh, lambda s: None)  # type: ignore[arg-type]
    assert adapter.calls == [("close", "ENG-1")]
    assert _requests(home)[EXT]["closure"]["result"] == "closed"


def test_on_leaf_closes_once_via_reconcile_and_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 39, base_ref="feature/plugins")
    _reconcile(home, repo, _leaf(39), 39, base="feature/plugins")
    adapter = _Adapter()
    monkeypatch.setattr(drain_module, "build_adapter", lambda *a, **k: adapter)
    config = TrackerConfig(True, {"linear": TrackerAdapterConfig(name="linear")})
    seen: list[subprocess.CompletedProcess[str]] = []

    def gh(args):
        seen.append(forge(39, base="feature/plugins")(args))
        return seen[-1]

    drain(home, config, {}, object(), gh, lambda s: None)  # type: ignore[arg-type]
    drain(home, config, {}, object(), gh, lambda s: None)  # type: ignore[arg-type]
    assert adapter.calls == [("close", "ENG-1")] and len(seen) == 1
    assert _requests(home)[EXT]["closure"]["base_ref"] == "feature/plugins"


@pytest.mark.parametrize("error", [closure.ClosureReadError("pr_read_failed"), OSError("disk")])
def test_epic_hook_failure_keeps_the_epic_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 40)

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(closure, "discover_associations", boom)
    assert _reconcile(home, repo, _epic(40), 40) == []
    assert unresolved_reasons(home) == ["epic_not_leaf"]
    assert not evidence_path(home).exists()


def test_epic_on_a_foreign_base_repository_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 41)
    _reconcile(home, repo, _epic(41), 41, slug="other/project")
    assert not evidence_path(home).exists()
    assert unresolved_reasons(home) == ["epic_not_leaf"]


def test_epic_dry_run_queues_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, repo = _home(tmp_path, monkeypatch, close=True)
    evidence(home, 42)
    closure.reconcile_merged_leaves(
        home, dry_run=True, chainlink_runner=_epic(42), gh_runner=forge(42),
        git_runner=git_runner(repo),
    )
    assert not evidence_path(home).exists()
