"""Tracker drain: queued transitions and backstop closures, applied once from tracker evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest
from mimir.poller_tracker import safe_url

from mimir_cultivate.tracker import drain as drain_module
from mimir_cultivate.tracker.adapters.base import ActionResult
from mimir_cultivate.tracker.config import (
    TrackerAdapterConfig,
    TrackerConfig,
)
from mimir_cultivate.tracker.drain import drain, poller_state_dir
from mimir_cultivate.tracker.evidence import evidence_path, evidence_transaction, read_evidence
from mimir_cultivate.tracker.transport import TrackerUnavailable

EXT = "linear:ENG-1"
LINEAR_URL = "https://linear.app/acme/issue/ENG-1"
PR = "https://github.com/acme/app/pull/5"
SHA = "abcdef1234567890"


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.state: str | None = "Todo"
        self.close_result = ActionResult("applied")
        self.transition_result = ActionResult("applied")
        self.raises: list[TrackerUnavailable] = []
        self.read_raises = False

    def _maybe_raise(self) -> None:
        if self.raises:
            raise self.raises.pop(0)

    def read(self, ref: str) -> SimpleNamespace:
        self.calls.append(("read", ref))
        if self.read_raises:
            raise TrackerUnavailable("timeout")
        return SimpleNamespace(url=f"https://linear.app/acme/issue/{ref}")

    def current_state(self, ref: str) -> str | None:
        self.calls.append(("current_state", ref))
        return self.state

    def transition(self, ref: str, state: str) -> ActionResult:
        self.calls.append(("transition", ref, state))
        self._maybe_raise()
        return self.transition_result

    def close(self, ref: str) -> ActionResult:
        self.calls.append(("close", ref))
        self._maybe_raise()
        return self.close_result


class Gh:
    def __init__(self, *, merged: bool = True, state: str = "closed") -> None:
        self.merged = merged
        self.state = state
        self.calls = 0

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        payload = {
            "number": 5,
            "html_url": PR,
            "body": "",
            "state": self.state,
            "merged": self.merged,
            "merged_at": "2026-10-29T00:00:00Z" if self.merged else None,
            "merge_commit_sha": SHA if self.merged else None,
            "base": {"repo": {"full_name": "acme/app"}, "ref": "release/next"},
        }
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    fake = FakeAdapter()
    monkeypatch.setattr(drain_module, "build_adapter", lambda *a, **k: fake)
    return fake


def _config(*, close: bool = True, transitions: dict[str, str] | None = None) -> TrackerConfig:
    return TrackerConfig(
        close_on_merge=close,
        adapters={"linear": TrackerAdapterConfig(name="linear", transitions=transitions or {})},
    )


def _closure(home: Path, **fields: Any) -> None:
    entry = {
        "stage": "requested", "chainlink_issue": 7, "pr_url": PR, "result": None,
        "reason": None, "attempts": 0, "merge_commit_sha": None, "base_ref": None,
        "requested_at": "t", "recorded_at": None,
    }
    entry.update(fields)
    with evidence_transaction(home) as data:
        data["items"].setdefault(EXT, {})["closure"] = entry


def _transition(home: Path, event: str = "build_started", **fields: Any) -> None:
    entry = {
        "stage": "requested", "state": "In Progress", "result": None, "reason": None,
        "attempts": 0, "requested_at": "t", "recorded_at": None,
    }
    entry.update(fields)
    with evidence_transaction(home) as data:
        data["items"].setdefault(EXT, {}).setdefault("transitions", {})[event] = entry


def _run(home: Path, config: TrackerConfig, gh: Gh | None = None) -> tuple[int, list[dict]]:
    emitted: list[dict] = []
    count = drain(home, config, {}, object(), gh or Gh(), emitted.append)  # type: ignore[arg-type]
    return count, emitted


def _evidence(home: Path) -> dict:
    return read_evidence(home)


def test_on_leaf_closes_once_records_result(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    count, _ = _run(tmp_path, _config())
    assert count == 1
    closure = _evidence(tmp_path)["items"][EXT]["closure"]
    assert (closure["stage"], closure["result"], closure["attempts"]) == ("done", "closed", 1)
    assert (closure["merge_commit_sha"], closure["base_ref"]) == (SHA, "release/next")
    assert ("close", "ENG-1") in adapter.calls
    adapter.calls.clear()
    gh = Gh()
    for _ in range(2):
        assert _run(tmp_path, _config(), gh)[0] == 0
    assert adapter.calls == [] and gh.calls == 0


def test_drain_noop_when_disabled_after_request(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    before = evidence_path(tmp_path).read_text()
    gh = Gh()
    assert _run(tmp_path, _config(close=False), gh)[0] == 0
    assert adapter.calls == [] and gh.calls == 0
    assert evidence_path(tmp_path).read_text() == before


def test_off_with_merged_mapping_zero_calls(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    _run(tmp_path, _config(close=False, transitions={"merged": "Done"}))
    assert adapter.calls == []


def test_closure_requires_forge_merge_state(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    gh = Gh(merged=False, state="open")
    _run(tmp_path, _config(), gh)
    assert gh.calls == 1 and adapter.calls == []
    assert _evidence(tmp_path)["items"][EXT]["closure"]["stage"] == "requested"


def test_closed_unmerged_pr_deletes_request(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    _run(tmp_path, _config(), Gh(merged=False, state="closed"))
    assert adapter.calls == [] and _evidence(tmp_path)["items"] == {}


def test_already_closed_by_github(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    adapter.state = "closed"
    _run(tmp_path, _config())
    assert _evidence(tmp_path)["items"][EXT]["closure"]["result"] == "already_closed"
    assert ("close", "ENG-1") not in adapter.calls


def test_close_reporting_already_is_recorded(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    adapter.close_result = ActionResult("already")
    _run(tmp_path, _config())
    assert _evidence(tmp_path)["items"][EXT]["closure"]["result"] == "already_closed"


def test_merged_mapping_is_applied_instead_of_close(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    _run(tmp_path, _config(transitions={"merged": "Done"}))
    assert ("transition", "ENG-1", "Done") in adapter.calls
    assert not [c for c in adapter.calls if c[0] == "close"]
    assert _evidence(tmp_path)["items"][EXT]["closure"]["result"] == "transitioned"


def test_merged_mapping_already_in_state(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    adapter.state = "Done"
    _run(tmp_path, _config(transitions={"merged": "Done"}))
    assert _evidence(tmp_path)["items"][EXT]["closure"]["result"] == "already_in_state"
    assert not [c for c in adapter.calls if c[0] == "transition"]


def test_unavailable_closure_records_notice_not_failure(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _closure(tmp_path)
    adapter.close_result = ActionResult("unavailable", "merged_state_required")
    _run(tmp_path, _config())
    data = _evidence(tmp_path)
    closure = data["items"][EXT]["closure"]
    assert (closure["stage"], closure["result"], closure["reason"]) == (
        "done", "unavailable", "merged_state_required",
    )
    assert [(n["lifecycle_event"], n["reason"]) for n in data["notices"]] == [
        ("merged", "merged_state_required")
    ]


def test_closure_attempts_capped_then_notice(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    adapter.raises = [TrackerUnavailable("timeout") for _ in range(5)]
    stages = []
    for _ in range(4):
        _run(tmp_path, _config())
        stages.append(_evidence(tmp_path)["items"][EXT]["closure"]["stage"])
    assert stages == ["requested", "requested", "done", "done"]
    data = _evidence(tmp_path)
    assert data["items"][EXT]["closure"]["result"] == "failed"
    assert data["items"][EXT]["closure"]["attempts"] == 3
    assert len(data["notices"]) == 1


def test_no_external_call_inside_evidence_lock(
    tmp_path: Path, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    _closure(tmp_path)
    _transition(tmp_path)
    held: list[bool] = []

    def probe() -> None:
        lock = (evidence_path(tmp_path).parent / "evidence.lock").open("a")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            lock.close()

    for name in ("current_state", "transition", "close"):
        original = getattr(adapter, name)

        def wrapped(*args: Any, _original=original) -> Any:
            probe()
            return _original(*args)

        monkeypatch.setattr(adapter, name, wrapped)
    _run(tmp_path, _config(transitions={"build_started": "In Progress"}))
    assert held and not any(held)


def test_started_stage_is_persisted_before_the_call(
    tmp_path: Path, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure(tmp_path)
    seen: list[str] = []
    original = adapter.close

    def close(ref: str) -> ActionResult:
        seen.append(_evidence(tmp_path)["items"][EXT]["closure"]["stage"])
        return original(ref)

    monkeypatch.setattr(adapter, "close", close)
    _run(tmp_path, _config())
    assert seen == ["started"]


def test_transition_started_is_persisted_before_the_call(
    tmp_path: Path, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transition(tmp_path)
    seen: list[str] = []
    original = adapter.transition

    def transition(ref: str, state: str) -> ActionResult:
        seen.append(_evidence(tmp_path)["items"][EXT]["transitions"]["build_started"]["stage"])
        return original(ref, state)

    monkeypatch.setattr(adapter, "transition", transition)
    _run(tmp_path, _config(transitions={"build_started": "In Progress"}))
    assert seen == ["started"]


def test_transition_applied_once(tmp_path: Path, adapter: FakeAdapter) -> None:
    _transition(tmp_path)
    config = _config(transitions={"build_started": "In Progress"})
    assert _run(tmp_path, config)[0] == 1
    assert _run(tmp_path, config)[0] == 0
    entry = _evidence(tmp_path)["items"][EXT]["transitions"]["build_started"]
    assert (entry["stage"], entry["result"]) == ("done", "applied")
    assert adapter.calls == [("transition", "ENG-1", "In Progress")]


def test_transition_now_unset_makes_no_call(tmp_path: Path, adapter: FakeAdapter) -> None:
    _transition(tmp_path)
    _run(tmp_path, _config())
    entry = _evidence(tmp_path)["items"][EXT]["transitions"]["build_started"]
    assert entry["result"] == "unset" and adapter.calls == []


def test_unavailable_transition_is_notice_not_failure(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _transition(tmp_path)
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    count, _ = _run(tmp_path, _config(transitions={"build_started": "In Progress"}))
    data = _evidence(tmp_path)
    assert count == 1
    assert data["items"][EXT]["transitions"]["build_started"]["result"] == "unavailable"
    assert [n["lifecycle_event"] for n in data["notices"]] == ["build_started"]


def test_action_budget_is_bounded(tmp_path: Path, adapter: FakeAdapter) -> None:
    for event in ("build_started", "pr_opened", "filed"):
        _transition(tmp_path, event)
    config = _config(transitions={"build_started": "A", "pr_opened": "B", "filed": "C"})
    count = drain(tmp_path, config, {}, object(), Gh(), lambda s: None, max_actions=2)  # type: ignore[arg-type]
    assert count == 2


def test_notices_emit_until_receipt_then_marked(tmp_path: Path, adapter: FakeAdapter) -> None:
    _transition(tmp_path)
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    config = _config(transitions={"build_started": "In Progress"})
    _run(tmp_path, config)
    _, emitted = _run(tmp_path, config)
    key = _evidence(tmp_path)["notices"][0]["delivery_key"]
    assert key.startswith("trk-") and len(key) == 28
    assert emitted == [
        {
            "kind": "tracker_transition_unavailable", "delivery_key": key,
            "adapter": "linear", "ref": "ENG-1", "url": LINEAR_URL,
            "lifecycle_event": "build_started",
            "reason": "state_not_found",
        }
    ]
    _, again = _run(tmp_path, config)
    assert len(again) == 1
    receipts = poller_state_dir(tmp_path) / ".delivery-receipts"
    receipts.mkdir(parents=True)
    (receipts / hashlib.sha256(key.encode()).hexdigest()).write_text("")
    _, final = _run(tmp_path, config)
    assert final == [] and _evidence(tmp_path)["notices"][0]["notified"] is True


def test_evidence_has_no_tracker_text(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    _transition(tmp_path)
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    _run(tmp_path, _config(transitions={"build_started": "In Progress"}))

    def strings(node: Any) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for v in node.values() for s in strings(v)]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return []

    token = re.compile(r"[A-Za-z0-9_:/.#+\- ]*")
    assert all(token.fullmatch(text) for text in strings(_evidence(tmp_path)))


def test_drain_writes_carry_no_text(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)
    _transition(tmp_path)
    _run(tmp_path, _config(transitions={"build_started": "In Progress"}))
    writes = [c for c in adapter.calls if c[0] in ("transition", "close")]
    assert writes == [("transition", "ENG-1", "In Progress"), ("close", "ENG-1")]


def _seed_notice(home: Path, url: str) -> str:
    key = "trk-" + "0" * 24
    with evidence_transaction(home) as data:
        data["notices"].append(
            {
                "delivery_key": key, "adapter": "linear", "ref": "ENG-1", "url": url,
                "lifecycle_event": "build_started", "reason": "state_not_found",
                "created_at": "t", "notified": False,
            }
        )
    return key


def test_emitted_notice_carries_the_stored_url(tmp_path: Path, adapter: FakeAdapter) -> None:
    _seed_notice(tmp_path, "https://linear.app/acme/issue/ENG-1")
    _, emitted = _run(tmp_path, _config())
    assert [e["url"] for e in emitted] == ["https://linear.app/acme/issue/ENG-1"]


def test_drain_created_notices_carry_a_canonical_url(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _closure(tmp_path)
    _transition(tmp_path)
    adapter.close_result = ActionResult("unavailable", "merged_state_required")
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    config = _config(transitions={"build_started": "In Progress"})
    _run(tmp_path, config)
    _run(tmp_path, config)
    notices = _evidence(tmp_path)["notices"]
    assert {n["lifecycle_event"] for n in notices} == {"merged", "build_started"}
    assert all(safe_url(n["url"], "ENG-1") == LINEAR_URL for n in notices)


@pytest.mark.parametrize(
    ("ext", "env", "expected"),
    [
        ("github_issues:acme/app#9", {}, "https://github.com/acme/app/issues/9"),
        ("jira:PROJ-12", {"JIRA_SITE": "acme.atlassian.net"}, "https://acme.atlassian.net/browse/PROJ-12"),
    ],
)
def test_github_and_jira_notice_urls_are_built_without_a_tracker_read(
    tmp_path: Path, adapter: FakeAdapter, ext: str, env: dict[str, str], expected: str
) -> None:
    name, _, ref = ext.partition(":")
    with evidence_transaction(tmp_path) as data:
        data["items"][ext] = {
            "transitions": {
                "build_started": {
                    "stage": "requested", "state": "X", "result": None, "reason": None,
                    "attempts": 0, "requested_at": "t", "recorded_at": None,
                }
            }
        }
    adapter.transition_result = ActionResult("refused", "nope")
    config = TrackerConfig(
        close_on_merge=True,
        adapters={name: TrackerAdapterConfig(name=name, transitions={"build_started": "X"})},
    )
    drain(tmp_path, config, env, object(), Gh(), lambda s: None)  # type: ignore[arg-type]
    (notice,) = _evidence(tmp_path)["notices"]
    assert notice["url"] == expected and safe_url(expected, ref) == expected
    assert not [c for c in adapter.calls if c[0] == "read"]


def _actions(adapter: FakeAdapter) -> list[tuple[str, ...]]:
    return [c for c in adapter.calls if c[0] != "read"]


def test_unreadable_url_never_extends_a_transition(tmp_path: Path, adapter: FakeAdapter) -> None:
    _transition(tmp_path)
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    adapter.read_raises = True
    config = _config(transitions={"build_started": "In Progress"})
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _, emitted = _run(tmp_path, config)
        assert emitted == []
    data = _evidence(tmp_path)
    entry = data["items"][EXT]["transitions"]["build_started"]
    assert entry["stage"] == "done" and entry["result"] == "unavailable"
    assert len(_actions(adapter)) <= drain_module.MAX_ATTEMPTS
    assert [n["url"] for n in data["notices"]] == [""]


def test_always_unavailable_transition_exhausts_attempts(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _transition(tmp_path)
    adapter.raises = [TrackerUnavailable("timeout")] * 20
    adapter.read_raises = True
    config = _config(transitions={"build_started": "In Progress"})
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _run(tmp_path, config)
    entry = _evidence(tmp_path)["items"][EXT]["transitions"]["build_started"]
    assert (entry["stage"], entry["result"]) == ("done", "failed")
    assert len(_actions(adapter)) == drain_module.MAX_ATTEMPTS


def test_refused_transition_is_a_single_call_with_unreadable_url(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _transition(tmp_path)
    adapter.transition_result = ActionResult("refused", "nope")
    adapter.read_raises = True
    config = _config(transitions={"build_started": "In Progress"})
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _run(tmp_path, config)
    assert len(_actions(adapter)) == 1
    assert _evidence(tmp_path)["items"][EXT]["transitions"]["build_started"]["stage"] == "done"


def test_failing_pr_read_is_bounded_with_unreadable_url(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _closure(tmp_path)
    adapter.read_raises = True

    class Failing(Gh):
        def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            return subprocess.CompletedProcess([], 1, "", "boom")

    gh = Failing()
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _run(tmp_path, _config(), gh)
    assert gh.calls == drain_module.MAX_ATTEMPTS
    assert _evidence(tmp_path)["items"][EXT]["closure"]["stage"] == "done"
    assert _actions(adapter) == []


@pytest.mark.parametrize("section", ["closure", "transitions"])
def test_started_at_cap_with_unreadable_url_makes_no_call(
    tmp_path: Path, adapter: FakeAdapter, section: str
) -> None:
    adapter.read_raises = True
    if section == "closure":
        _closure(tmp_path, stage="started", attempts=drain_module.MAX_ATTEMPTS)
    else:
        _transition(tmp_path, stage="started", attempts=drain_module.MAX_ATTEMPTS)
    gh = Gh()
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _run(tmp_path, _config(transitions={"build_started": "In Progress"}), gh)
    assert _actions(adapter) == [] and gh.calls <= 1
    item = _evidence(tmp_path)["items"][EXT]
    entry = item["closure"] if section == "closure" else item["transitions"]["build_started"]
    assert entry["stage"] == "done"


def test_stuck_entries_do_not_starve_a_later_closure(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    adapter.read_raises = True
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    with evidence_transaction(tmp_path) as data:
        for n in range(10):
            data["items"][f"linear:AAA-{n:02d}"] = {
                "transitions": {
                    "build_started": {
                        "stage": "requested", "state": "X", "result": None, "reason": None,
                        "attempts": 0, "requested_at": "t", "recorded_at": None,
                    }
                }
            }
    _closure(tmp_path)
    config = _config(transitions={"build_started": "In Progress"})
    gh = Gh()
    for _ in range(drain_module.MAX_ATTEMPTS + 2):
        _run(tmp_path, config, gh)
    assert gh.calls >= 1
    assert _evidence(tmp_path)["items"][EXT]["closure"]["stage"] == "done"


def test_empty_url_notice_is_emitted_only_once_resolvable(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    _transition(tmp_path)
    adapter.transition_result = ActionResult("unavailable", "state_not_found")
    adapter.read_raises = True
    config = _config(transitions={"build_started": "In Progress"})
    emitted_all: list[dict] = []
    for _ in range(3):
        emitted_all += _run(tmp_path, config)[1]
    assert emitted_all == []
    assert [n["url"] for n in _evidence(tmp_path)["notices"]] == [""]
    adapter.read_raises = False
    _, emitted = _run(tmp_path, config)
    assert [e["url"] for e in emitted] == [LINEAR_URL]
    assert [n["url"] for n in _evidence(tmp_path)["notices"]] == [LINEAR_URL]
    calls = len(adapter.calls)
    _, again = _run(tmp_path, config)
    assert len(again) == 1 and len(adapter.calls) == calls


def test_url_resolution_does_not_spend_the_action_budget(
    tmp_path: Path, adapter: FakeAdapter
) -> None:
    with evidence_transaction(tmp_path) as data:
        data["notices"].append(
            {
                "delivery_key": "trk-" + "a" * 24, "adapter": "linear", "ref": "ENG-1",
                "url": "", "lifecycle_event": "merged", "reason": "x",
                "created_at": "t", "notified": False,
            }
        )
    count, emitted = _run(tmp_path, _config())
    assert count == 0 and len(emitted) == 1


def test_pr_read_failure_capped_with_one_notice(tmp_path: Path, adapter: FakeAdapter) -> None:
    _closure(tmp_path)

    class Failing(Gh):
        def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            return subprocess.CompletedProcess([], 1, "", "boom")

    gh = Failing()
    for _ in range(3):
        _run(tmp_path, _config(), gh)
    data = _evidence(tmp_path)
    closure = data["items"][EXT]["closure"]
    assert (closure["stage"], closure["result"], closure["reason"]) == (
        "done", "failed", "pr_read_failed",
    )
    assert [(n["lifecycle_event"], n["reason"]) for n in data["notices"]] == [
        ("merged", "pr_read_failed")
    ]
    assert gh.calls == 3
    _run(tmp_path, _config(), gh)
    assert gh.calls == 3 and len(_evidence(tmp_path)["notices"]) == 1


@pytest.mark.parametrize("section", ["closure", "transition"])
def test_started_entry_at_the_attempt_cap_fails_without_a_call(
    tmp_path: Path, adapter: FakeAdapter, section: str
) -> None:
    if section == "closure":
        _closure(tmp_path, stage="started", attempts=drain_module.MAX_ATTEMPTS)
    else:
        _transition(tmp_path, stage="started", attempts=drain_module.MAX_ATTEMPTS)
    _run(tmp_path, _config(transitions={"build_started": "In Progress"}))
    data = _evidence(tmp_path)
    item = data["items"][EXT]
    entry = item["closure"] if section == "closure" else item["transitions"]["build_started"]
    assert (entry["stage"], entry["result"], entry["attempts"]) == (
        "done", "failed", drain_module.MAX_ATTEMPTS,
    )
    assert len(data["notices"]) == 1
    assert [c for c in adapter.calls if c[0] != "read"] == []


def test_missing_adapter_records_one_notice(tmp_path: Path, adapter: FakeAdapter) -> None:
    ext = "github_issues:acme/app#9"
    with evidence_transaction(tmp_path) as data:
        data["items"][ext] = {
            "closure": {
                "stage": "requested", "chainlink_issue": 7, "pr_url": PR, "result": None,
                "reason": None, "attempts": 0, "merge_commit_sha": None, "base_ref": None,
                "requested_at": "t", "recorded_at": None,
            }
        }
    gh = Gh()
    for _ in range(3):
        drain(tmp_path, _config(), {}, object(), gh, lambda s: None)  # type: ignore[arg-type]
    notices = _evidence(tmp_path)["notices"]
    assert [(n["adapter"], n["reason"], n["url"]) for n in notices] == [
        ("github_issues", "adapter_unavailable", "https://github.com/acme/app/issues/9")
    ]
    assert gh.calls == 0


def test_adapter_build_failure_records_one_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("unavailable")

    monkeypatch.setattr(drain_module, "build_adapter", broken)
    ext = "jira:PROJ-12"
    with evidence_transaction(tmp_path) as data:
        data["items"][ext] = {
            "transitions": {
                "build_started": {
                    "stage": "requested", "state": "X", "result": None, "reason": None,
                    "attempts": 0, "requested_at": "t", "recorded_at": None,
                }
            }
        }
    config = TrackerConfig(
        close_on_merge=True,
        adapters={"jira": TrackerAdapterConfig(name="jira", transitions={"build_started": "X"})},
    )
    for _ in range(2):
        drain(tmp_path, config, {"JIRA_SITE": "acme.atlassian.net"}, object(), Gh(), lambda s: None)  # type: ignore[arg-type]
    assert [n["reason"] for n in _evidence(tmp_path)["notices"]] == ["adapter_unavailable"]
