from __future__ import annotations

import importlib
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mimir.poller_hooks import ItemAttestation
from mimir_cultivate.tracker import attest, transport
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.attest import attest_item, tracker_poller_hooks
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.prompts import render_prompt
from mimir_cultivate.tracker.thread import Activity, TrackerIssue, activity_id
from mimir_cultivate.tracker.transport import TrackerUnavailable
from tracker_replay import forbid_default_transport  # noqa: F401  (opt-in fixture)

AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
EVENT = activity_id("linear", "h1:assigned")
URL = "https://linear.app/acme/issue/ENG-1"


def issue(**overrides: Any) -> TrackerIssue:
    fields: dict[str, Any] = dict(
        adapter="linear", stable_id="1", ref="ENG-1", url=URL, project="ENG", title="T",
        body="B", author_id="alice", is_open=True, assigned_to_bot=True,
        activities=(Activity("assigned", EVENT, AT, "alice"),), comments=(), truncated=False,
        bot_id="bot",
    )
    fields.update(overrides)
    return TrackerIssue(**fields)


class FakeAdapter:
    def __init__(self, result: Any, trusted: bool | None = True) -> None:
        self.result = result
        self.trusted = trusted
        self.reads: list[str] = []

    def read(self, ref: str) -> TrackerIssue:
        self.reads.append(ref)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        return self.trusted


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "worklink.yaml").write_text("tracker:\n  adapters:\n    linear: {}\n")
    return tmp_path


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, home: Path):
    built: list[tuple[str, Any, Any]] = []

    def install(adapter: FakeAdapter):
        def fake_build(name: str, config: Any, env: Any, tp: Any) -> FakeAdapter:
            built.append((name, config, tp))
            return adapter

        monkeypatch.setattr(attest, "build_adapter", fake_build)
        return built

    return install


def extras(**overrides: Any) -> dict[str, Any]:
    base = {
        "adapter": "linear", "ref": "ENG-1", "url": URL, "kind": "triage",
        "attested_event_id": EVENT, "delivery_key": "trk-" + "0" * 24,
    }
    base.update(overrides)
    return base


def env_for(home: Path) -> dict[str, str]:
    return {"MIMIR_HOME": str(home)}


def test_trusted_issue_returns_actor_and_the_rendered_prompt(wire, home, monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(transport, "default_transport", lambda: sentinel)
    built = wire(FakeAdapter(issue()))

    result = attest_item(extras(), env_for(home))

    assert result == ItemAttestation(True, actor="alice", prompt=render_prompt("triage", issue()))
    assert built[0][0] == "linear" and isinstance(built[0][1], TrackerAdapterConfig)
    assert built[0][2] is sentinel


def test_update_kind_renders_the_update_prompt(wire, home) -> None:
    wire(FakeAdapter(issue()))

    result = attest_item(extras(kind="update"), env_for(home))

    assert result.prompt == render_prompt("update", issue())


@pytest.mark.parametrize(
    "overrides",
    [
        {"adapter": "Linear"},
        {"adapter": "jira"},
        {"ref": "../x"},
        {"ref": "ENG-2"},
        {"url": "http://linear.app/acme/issue/ENG-1"},
        {"url": "https://linear.app/acme/issue/ENG-2"},
        {"kind": "other"},
        {"attested_event_id": "nope"},
        {"attested_event_id": "ev" + "1" * 24},
    ],
)
def test_mismatched_or_invalid_extras_are_refused(wire, home, overrides) -> None:
    wire(FakeAdapter(issue()))

    assert attest_item(extras(**overrides), env_for(home)) == ItemAttestation(False)


def test_invalid_extras_never_reach_the_network(forbid_default_transport, home) -> None:
    assert attest_item(extras(kind="other"), env_for(home)) == ItemAttestation(False)
    assert attest_item(extras(adapter="jira"), env_for(home)) == ItemAttestation(False)


def test_canonical_url_drift_is_refused(wire, home) -> None:
    wire(FakeAdapter(issue(url="https://linear.app/acme/issue/ENG-1/")))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_issue_reporting_another_ref_is_refused(wire, home) -> None:
    wire(FakeAdapter(issue(ref="ENG-2")))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_untrusted_latest_activity_is_refused(wire, home) -> None:
    late = Activity("comment", "ev-late", AT.replace(minute=5), "mallory")
    wire(FakeAdapter(issue(activities=(issue().activities[0], late)), trusted=False))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_a_newer_cutoff_than_emitted_is_refused(wire, home) -> None:
    newer = Activity("comment", "ev-newer", AT.replace(minute=5), "alice")
    wire(FakeAdapter(issue(activities=(issue().activities[0], newer))))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_skipped_issue_is_refused(wire, home) -> None:
    wire(FakeAdapter(issue(is_open=False)))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


@pytest.mark.parametrize("code", ["timeout", "http_503", "malformed_payload"])
def test_read_failure_is_unavailable(wire, home, code) -> None:
    wire(FakeAdapter(TrackerUnavailable(code)))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(None)


def test_unavailable_trust_is_unavailable(wire, home) -> None:
    wire(FakeAdapter(issue(), trusted=None))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(None)


def test_unconfigured_adapter_is_refused(wire, home) -> None:
    (home / "worklink.yaml").write_text("tracker:\n  adapters:\n    jira:\n      account_id: a\n      projects: [PROJ]\n      trusted_groups: [g]\n")
    wire(FakeAdapter(issue()))

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_missing_home_or_bad_config_is_unavailable(wire, home) -> None:
    wire(FakeAdapter(issue()))
    (home / "worklink.yaml").write_text("tracker: [unclosed\n")

    assert attest_item(extras(), {}) == ItemAttestation(None)
    assert attest_item(extras(), env_for(home)) == ItemAttestation(None)
    (home / "worklink.yaml").write_text("tracker:\n  bogus: 1\n")
    assert attest_item(extras(), env_for(home)) == ItemAttestation(None)


def test_unavailable_adapter_module_is_refused(home, monkeypatch) -> None:
    def broken(*args: Any) -> Any:
        raise ValueError("tracker adapter linear is unavailable")

    monkeypatch.setattr(attest, "build_adapter", broken)

    assert attest_item(extras(), env_for(home)) == ItemAttestation(False)


def test_hooks_register_the_attestor() -> None:
    assert tracker_poller_hooks().trust_attestor is attest_item


# ---- build_adapter ----------------------------------------------------------------------------


def test_build_adapter_rejects_unknown_names() -> None:
    config = TrackerAdapterConfig(name="nope")

    for name in ("nope", "base", "../linear", ""):
        with pytest.raises(ValueError, match="unknown tracker adapter"):
            build_adapter(name, config, {}, object())  # type: ignore[arg-type]


def test_build_adapter_calls_the_module_build(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("mimir_cultivate.tracker.adapters.linear")
    module.build = lambda config, env, tp: ("built", config, dict(env), tp)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mimir_cultivate.tracker.adapters.linear", module)
    config = TrackerAdapterConfig(name="linear")

    assert build_adapter("linear", config, {"K": "v"}, "tp") == ("built", config, {"K": "v"}, "tp")  # type: ignore[arg-type]


def test_build_adapter_import_failure_is_a_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", failing)

    with pytest.raises(ValueError, match="linear is unavailable"):
        build_adapter("linear", TrackerAdapterConfig(name="linear"), {}, object())  # type: ignore[arg-type]


def test_importing_the_package_does_not_import_adapter_modules() -> None:
    code = (
        "import sys, mimir_cultivate.tracker.adapters as a;"
        "loaded=[m for m in sys.modules if m.startswith('mimir_cultivate.tracker.adapters.')];"
        "print(sorted(loaded))"
    )

    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "['mimir_cultivate.tracker.adapters.base']"
