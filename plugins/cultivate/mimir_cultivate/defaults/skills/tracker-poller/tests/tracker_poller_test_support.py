"""Fakes and a fire driver for the tracker-poller tests."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mimir_cultivate.tracker.config import TrackerAdapterConfig, TrackerConfig
from mimir_cultivate.tracker.thread import Activity, Comment, TrackerIssue, activity_id
from mimir_cultivate.tracker.transport import TrackerUnavailable
from mimir_cultivate.tracker.work import ChainlinkWork

_MODULE_NAME = "_tracker_poller_under_test"
_POLLER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "poller.py"
_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _POLLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
poller = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = poller
_SPEC.loader.exec_module(poller)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
REF = "acme/widgets#42"
URL = "https://github.com/acme/widgets/issues/42"
ENTRY = "github_issues:9042"
CONFIG = TrackerConfig(
    adapters={"github_issues": TrackerAdapterConfig(name="github_issues", repos=("acme/widgets",))}
)


def event(raw: str) -> str:
    return activity_id("github_issues", raw)


def issue(
    *,
    ref: str = REF,
    stable_id: str = "9042",
    updated_at: str | None = "2026-10-01T12:00:00+00:00",
    activities: Sequence[Activity] | None = None,
    comments: Sequence[Comment] = (),
    title: str = "Login crash",
    **fields: Any,
) -> TrackerIssue:
    number = ref.rsplit("#", 1)[1]
    base = TrackerIssue(
        adapter="github_issues", stable_id=stable_id, ref=ref,
        url=f"https://github.com/{ref.split('#')[0]}/issues/{number}", project=None,
        title=title, body="It crashes.", author_id="alice", is_open=True, assigned_to_bot=True,
        activities=tuple(
            activities if activities is not None else (Activity("assigned", event("AE_1"), T0, "alice"),)
        ),
        comments=tuple(comments), truncated=False, bot_id="bot", updated_at=updated_at,
    )
    return replace(base, **fields)


class FakeAdapter:
    """Lists and reads from dicts the test mutates; ``trust`` answers from ``trusted``."""

    def __init__(self) -> None:
        self.full: dict[str, TrackerIssue] = {}
        self.listed: list[TrackerIssue] = []
        self.complete = True
        self.list_error: str | None = None
        self.read_error: str | None = None
        self.reads: list[str] = []
        self.trusted: dict[str, bool | None] = {"alice": True, "mallory": False}

    def put(self, full: TrackerIssue, *, listed: bool = True) -> None:
        self.full[full.ref] = full
        self.listed = [i for i in self.listed if i.ref != full.ref]
        if listed:
            self.listed.append(replace(full, body="", activities=(), comments=()))

    def list_assigned(self) -> tuple[list[TrackerIssue], bool]:
        if self.list_error:
            raise TrackerUnavailable(self.list_error)
        return list(self.listed), self.complete

    def read(self, ref: str) -> TrackerIssue:
        self.reads.append(ref)
        if self.read_error:
            raise TrackerUnavailable(self.read_error)
        return self.full[ref]

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        return self.trusted.get(actor_id)


class FakeChainlink:
    """Minimal Chainlink CLI: search, show, create and label."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail: set[str] = set()

    def add(self, description: str, *, labels: Sequence[str] = (), comments: Sequence[str] = (),
            status: str = "open") -> int:
        issue_id = 9137 + len(self.issues)
        self.issues[issue_id] = {
            "id": issue_id, "title": "x", "description": description, "status": status,
            "labels": list(labels), "comments": [{"content": c} for c in comments],
        }
        return issue_id

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv[1:])
        self.calls.append(args)
        verb = args[1]
        if verb in self.fail:
            return subprocess.CompletedProcess([], 1, "", "failed")
        if verb == "search":
            hits = [
                {"id": i["id"], "title": i["title"], "description": i["description"]}
                for i in self.issues.values() if args[2] in i["description"]
            ]
            return subprocess.CompletedProcess([], 0, json.dumps(hits), "")
        if verb == "show":
            return subprocess.CompletedProcess([], 0, json.dumps(self.issues[int(args[2])]), "")
        if verb == "label":
            self.issues[int(args[2])]["labels"].append(args[3])
            return subprocess.CompletedProcess([], 0, "", "")
        return subprocess.CompletedProcess([], 1, "", "unexpected")

    def labels_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[:2] == ("issue", "label")]


def external_description(ref: str = REF, url: str = URL) -> str:
    return f"Goal: x\n\nExternal: github_issues:{ref} {url}"


class Rig:
    """One poller process's worth of state over a temp home."""

    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.state = self.home / "state" / "pollers" / "tracker-poller"
        self.state.mkdir(parents=True)
        monkeypatch.setattr(poller, "STATE_DIR", self.state)
        monkeypatch.setattr(poller, "CURSOR_FILE", self.state / "cursor.json")
        self.adapter = FakeAdapter()
        self.chainlink = FakeChainlink()
        self.now = NOW
        self.config = CONFIG
        monkeypatch.setattr(poller, "build_adapter", lambda *a, **k: self.adapter)

    def fire(self, *, now: datetime | None = None, deadline: float = 1e12) -> list[dict[str, Any]]:
        return poller.run_fire(
            home=self.home, config=self.config, env={}, transport=object(),
            work=ChainlinkWork(runner=self.chainlink), gh_runner=_no_gh,
            now=now or self.now, deadline=deadline,
        )

    def cursor(self) -> dict[str, Any]:
        return json.loads((self.state / "cursor.json").read_text())

    def entry(self) -> dict[str, Any]:
        return self.cursor()["issues"][ENTRY]

    def receipt(self, key: str) -> None:
        directory = self.state / ".delivery-receipts"
        directory.mkdir(exist_ok=True)
        (directory / hashlib.sha256(key.encode()).hexdigest()).touch()

    def later(self, **delta: float) -> datetime:
        return self.now + timedelta(**delta)


def _no_gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise AssertionError("the poller tests make no gh call")


def items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if "signal" not in r]


def signals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if "signal" in r]
