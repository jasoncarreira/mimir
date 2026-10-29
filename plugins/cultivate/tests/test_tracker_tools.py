"""Tracker tools: scope binding, taint refusal, filing and fixed-shape results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mimir.models import InformationFlowLabels, SourceLabel
from mimir.tools.refusals import ToolPolicyRefusal
from mimir_cultivate.tracker import tools
from mimir_cultivate.tracker.adapters.base import ActionResult
from mimir_cultivate.tracker.evidence import read_evidence
from mimir_cultivate.tracker.record import ExternalRecord, render_external_record
from mimir_cultivate.tracker.transport import TrackerUnavailable
from mimir_cultivate.tracker.work import ChainlinkWork
from mimir_cultivate.worklink.planning import EPIC_TEMPLATE_MARKDOWN, LEAF_TEMPLATE_MARKDOWN
from tracker_fakes import FakeChainlink

LINEAR = ExternalRecord("linear", "ENG-1", "https://linear.app/acme/issue/ENG-1")
GITHUB = ExternalRecord("github_issues", "acme/app#4", "https://github.com/acme/app/issues/4")
MARKER = "TRACKER-PROSE-MARKER"


def _runtime(*records: ExternalRecord, labels: InformationFlowLabels | None = None):
    context = SimpleNamespace(
        tracker_issue_targets=frozenset((r.adapter, r.ref, r.url) for r in records),
        ifc_state=None,
        ifc_labels=labels or InformationFlowLabels(),
    )
    return SimpleNamespace(context=context)


def _untrusted() -> InformationFlowLabels:
    return InformationFlowLabels().with_source(SourceLabel(
        principal="poller:tracker-poller", domain="tracker", resource_id="tracker:linear:ENG-1",
        bridge_instance="tracker", sensitivity="public",
        authorized_principals=frozenset({"poller:tracker-poller"}), source_kind="channel",
        integrity="untrusted", integrity_effect="active_ingest",
    ))


class FakeAdapter:
    def __init__(self, result: ActionResult | Exception = ActionResult("posted")) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def _answer(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def comment(self, ref: str, body: str) -> ActionResult:
        self.calls.append(("comment", ref, body))
        return self._answer()

    def transition(self, ref: str, state: str) -> ActionResult:
        self.calls.append(("transition", ref, state))
        return self._answer()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    section = {"adapters": {"linear": {"transitions": {"filed": "Todo", "merged": "Done"}},
                            "github_issues": {"repos": ["acme/app"]}}}
    (tmp_path / "worklink.yaml").write_text(yaml.safe_dump({"tracker": section}), encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def chainlink(monkeypatch: pytest.MonkeyPatch) -> FakeChainlink:
    fake = FakeChainlink()
    monkeypatch.setattr(tools, "ChainlinkWork", lambda cwd: ChainlinkWork("chainlink", cwd, fake))
    return fake


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    fake = FakeAdapter()
    monkeypatch.setattr(tools, "build_adapter", lambda *_: fake)
    monkeypatch.setattr(tools.transport, "default_transport", lambda: object())
    return fake


def _file(runtime, **overrides):
    args = {"external_ref": LINEAR.external_ref(), "kind": "leaf", "title": "Fix it",
            "description": LEAF_TEMPLATE_MARKDOWN, **overrides}
    return tools.tracker_file_work.func(runtime=runtime, **args)


@pytest.mark.parametrize("call", [
    lambda rt, ref: tools.tracker_file_work.func(
        external_ref=ref, kind="leaf", title="t", description=LEAF_TEMPLATE_MARKDOWN, runtime=rt),
    lambda rt, ref: tools.tracker_comment.func(external_ref=ref, body="hi", runtime=rt),
    lambda rt, ref: tools.tracker_set_state.func(external_ref=ref, state="Done", runtime=rt),
])
@pytest.mark.parametrize("ref", ["linear:ENG-2", "jira:ENG-1", "", None])
def test_other_issue_is_outside_scope(call, ref, home, chainlink, adapter) -> None:
    with pytest.raises(ToolPolicyRefusal, match="outside this turn's poller scope"):
        call(_runtime(LINEAR), ref)
    assert chainlink.calls == [] and adapter.calls == []


def test_no_targets_is_outside_scope(home, chainlink) -> None:
    with pytest.raises(ToolPolicyRefusal):
        _file(_runtime())
    assert chainlink.calls == []


@pytest.mark.parametrize("call", [
    lambda rt: tools.tracker_file_work.func(
        external_ref=LINEAR.external_ref(), kind="leaf", title="t",
        description=LEAF_TEMPLATE_MARKDOWN, runtime=rt),
    lambda rt: tools.tracker_comment.func(external_ref=LINEAR.external_ref(), body="hi", runtime=rt),
    lambda rt: tools.tracker_set_state.func(external_ref=LINEAR.external_ref(), state="Done", runtime=rt),
])
def test_untrusted_turn_is_refused_without_side_effects(call, home, chainlink, adapter) -> None:
    result = call(_runtime(LINEAR, labels=_untrusted()))
    assert result["status"] == "refused" and result["reason"] == "tainted_turn"
    assert chainlink.calls == [] and adapter.calls == []


def test_live_ifc_state_overrides_clean_fallback(home, chainlink) -> None:
    runtime = _runtime(LINEAR)
    runtime.context.ifc_state = SimpleNamespace(current=lambda fallback=None: _untrusted())
    assert _file(runtime)["reason"] == "tainted_turn"
    assert chainlink.calls == []


def test_file_leaf_creates_armed_issue_with_record_and_requests_filed(home, chainlink) -> None:
    result = _file(_runtime(LINEAR))
    assert result == {"status": "created", "reason": "", "kind": "leaf", "missing": []}
    assert chainlink.count("search") == 2 and chainlink.count("create") == 1
    issue = next(iter(chainlink.issues.values()))
    assert issue["labels"] == ["worklink:ready"]
    assert issue["description"].endswith(render_external_record(LINEAR))
    transitions = read_evidence(home)["items"][LINEAR.external_ref()]["transitions"]
    assert transitions["filed"]["state"] == "Todo"


def test_file_epic_is_labelled_epic(home, chainlink) -> None:
    result = _file(_runtime(LINEAR), kind="epic", description=EPIC_TEMPLATE_MARKDOWN)
    assert result["status"] == "created" and result["kind"] == "epic"
    assert next(iter(chainlink.issues.values()))["labels"] == ["worklink:ready", "worklink:epic"]


def test_search_precedes_create_and_failed_search_never_creates(home, chainlink) -> None:
    chainlink.fail.add("search")
    result = _file(_runtime(LINEAR))
    assert result["status"] == "error" and result["reason"] == "search_unavailable"
    assert chainlink.count("create") == 0 and chainlink.issues == {}


def test_existing_item_is_reported_not_created_or_armed(home, chainlink) -> None:
    existing = chainlink.add(f"Goal\n\n{render_external_record(LINEAR)}")
    result = _file(_runtime(LINEAR))
    assert result["status"] == "exists"
    assert chainlink.count("create") == 0 and chainlink.count("label") == 0
    assert chainlink.issues[existing]["labels"] == []
    assert "filed" not in read_evidence(home)["items"].get(LINEAR.external_ref(), {}).get("transitions", {})


def test_second_call_finds_the_first_filing(home, chainlink) -> None:
    runtime = _runtime(LINEAR)
    assert _file(runtime)["status"] == "created"
    assert _file(runtime)["status"] == "exists"
    assert chainlink.count("create") == 1


def test_create_that_reports_failure_after_filing_is_found_by_the_confirming_search(
    home, monkeypatch,
) -> None:
    class Reporting(FakeChainlink):
        def __call__(self, argv):
            result = super().__call__(argv)
            if tuple(argv[1:3]) == ("issue", "create"):
                return self._done(1, err="timed out")
            return result

    fake = Reporting()
    monkeypatch.setattr(tools, "ChainlinkWork", lambda cwd: ChainlinkWork("chainlink", cwd, fake))
    assert _file(_runtime(LINEAR))["status"] == "created"
    assert _file(_runtime(LINEAR))["status"] == "exists"
    assert fake.count("create") == 1


def test_create_without_a_reported_id_is_still_confirmed(home, chainlink) -> None:
    chainlink.silent_create = True
    assert _file(_runtime(LINEAR))["status"] == "created"
    assert _file(_runtime(LINEAR))["status"] == "exists"


def test_unconfirmed_create_is_an_unknown_outcome(home, chainlink, monkeypatch) -> None:
    class Dropping(FakeChainlink):
        def __call__(self, argv):
            if tuple(argv[1:3]) == ("issue", "create"):
                self.calls.append(tuple(argv[1:]))
                return self._done(0, "Created issue #1\n")
            return super().__call__(argv)

    fake = Dropping()
    monkeypatch.setattr(tools, "ChainlinkWork", lambda cwd: ChainlinkWork("chainlink", cwd, fake))
    result = _file(_runtime(LINEAR))
    assert result["status"] == "error" and result["reason"] == "create_outcome_unknown"
    assert read_evidence(home)["items"] == {}


def test_duplicate_confirmation_is_an_unknown_outcome(home, chainlink, monkeypatch) -> None:
    class Doubling(FakeChainlink):
        def __call__(self, argv):
            result = super().__call__(argv)
            if tuple(argv[1:3]) == ("issue", "create"):
                self.add(self.issues[self.next_id - 1]["description"])
            return result

    fake = Doubling()
    monkeypatch.setattr(tools, "ChainlinkWork", lambda cwd: ChainlinkWork("chainlink", cwd, fake))
    assert _file(_runtime(LINEAR))["reason"] == "create_outcome_unknown"


@pytest.mark.parametrize(("kind", "template"), [
    ("leaf", "Acceptance criteria:\n- [ ] only this"),
    ("epic", "Goal:\nonly this"),
])
def test_incomplete_template_is_refused_with_part_names(kind, template, home, chainlink) -> None:
    result = _file(_runtime(LINEAR), kind=kind, description=template)
    assert result["status"] == "refused" and result["reason"] == "template_incomplete"
    assert result["missing"] and all(isinstance(part, str) for part in result["missing"])
    assert chainlink.calls == []


def test_leaf_template_is_not_enough_for_an_epic(home, chainlink) -> None:
    assert _file(_runtime(LINEAR), kind="epic")["reason"] == "template_incomplete"


@pytest.mark.parametrize("line", [
    "External: linear:ENG-9 https://linear.app/acme/issue/ENG-9",
    "  External: anything",
    "External:",
])
def test_description_with_reserved_external_line_is_refused(line, home, chainlink) -> None:
    result = _file(_runtime(LINEAR), description=f"{LEAF_TEMPLATE_MARKDOWN}\n{line}\n")
    assert result["status"] == "refused" and result["reason"] == "reserved_record_line"
    assert chainlink.calls == []


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"kind": "story"}, "invalid_kind"),
    ({"title": ""}, "invalid_title"),
    ({"title": "x" * 121}, "invalid_title"),
    ({"title": "two\nlines"}, "invalid_title"),
    ({"description": "x" * 20_001}, "description_too_long"),
])
def test_invalid_filing_arguments_are_refused(overrides, reason, home, chainlink) -> None:
    result = _file(_runtime(LINEAR), **overrides)
    assert result["status"] == "refused" and result["reason"] == reason
    assert chainlink.calls == []


def test_missing_home_is_an_error(monkeypatch, chainlink) -> None:
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert _file(_runtime(LINEAR)) == {
        "status": "error", "reason": "home_unavailable", "kind": "leaf", "missing": [],
    }


def test_filing_lock_is_keyed_by_the_ref_digest(home, chainlink) -> None:
    _file(_runtime(LINEAR))
    digest = hashlib.sha256(LINEAR.external_ref().encode()).hexdigest()[:16]
    assert (home / "state/worklink/tracker" / f"filing-{digest}.lock").exists()


def test_results_never_echo_tracker_or_chainlink_text(home, chainlink) -> None:
    result = _file(_runtime(LINEAR), title=f"{MARKER} title",
                   description=f"{LEAF_TEMPLATE_MARKDOWN}\n{MARKER}")
    text = json.dumps(result)
    assert MARKER not in text and "9137" not in text and "#" not in text


def test_comment_posts_to_the_bound_issue(home, adapter) -> None:
    result = tools.tracker_comment.func(
        external_ref=LINEAR.external_ref(), body="Looks good", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "posted", "reason": ""}
    assert adapter.calls == [("comment", "ENG-1", "Looks good")]


@pytest.mark.parametrize("body", [
    "see chainlink #12", "Chainlink-12", "see issue/12-a3 now", "the chainlink tracker",
    "", "x" * 8001, "   ",
])
def test_comment_refuses_internal_references_and_bad_bodies(body, home, adapter) -> None:
    result = tools.tracker_comment.func(
        external_ref=LINEAR.external_ref(), body=body, runtime=_runtime(LINEAR),
    )
    assert result["status"] == "refused"
    assert adapter.calls == []


def test_comment_adapter_failures_are_fixed_tokens(home, adapter) -> None:
    adapter.result = TrackerUnavailable("http_503")
    result = tools.tracker_comment.func(
        external_ref=LINEAR.external_ref(), body="hi", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "error", "reason": "http_503"}
    adapter.result = ActionResult("refused", f"{MARKER} with spaces")
    result = tools.tracker_comment.func(
        external_ref=LINEAR.external_ref(), body="hi", runtime=_runtime(LINEAR),
    )
    assert MARKER not in json.dumps(result)


def test_comment_without_adapter_config_is_an_error(home, monkeypatch) -> None:
    (home / "worklink.yaml").write_text("tracker: {}\n", encoding="utf-8")
    result = tools.tracker_comment.func(
        external_ref=LINEAR.external_ref(), body="hi", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "error", "reason": "adapter_unavailable"}


def test_set_state_applies_a_configured_state(home, adapter) -> None:
    adapter.result = ActionResult("applied")
    result = tools.tracker_set_state.func(
        external_ref=LINEAR.external_ref(), state="Done", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "applied", "reason": ""}
    assert adapter.calls == [("transition", "ENG-1", "Done")]


def test_set_state_refuses_unconfigured_state(home, adapter) -> None:
    result = tools.tracker_set_state.func(
        external_ref=LINEAR.external_ref(), state="Cancelled", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "refused", "reason": "state_not_configured"}
    assert adapter.calls == []


def test_set_state_is_unsupported_for_github_without_a_request(home, adapter) -> None:
    result = tools.tracker_set_state.func(
        external_ref=GITHUB.external_ref(), state="closed", runtime=_runtime(GITHUB),
    )
    assert result["status"] == "unsupported"
    assert adapter.calls == []


def test_set_state_unavailable_records_an_agent_notice_once(home, adapter) -> None:
    adapter.result = ActionResult("unavailable", "http_503")
    for _ in range(2):
        result = tools.tracker_set_state.func(
            external_ref=LINEAR.external_ref(), state="Done", runtime=_runtime(LINEAR),
        )
        assert result == {"status": "unavailable", "reason": "http_503"}
    notices = read_evidence(home)["notices"]
    assert len(notices) == 1
    assert notices[0]["lifecycle_event"] == "agent" and notices[0]["notified"] is False
    assert (notices[0]["adapter"], notices[0]["ref"]) == ("linear", "ENG-1")
    assert notices[0]["delivery_key"].startswith("trk-")


def test_set_state_network_error_is_unavailable_with_notice(home, adapter) -> None:
    adapter.result = TrackerUnavailable("timeout")
    result = tools.tracker_set_state.func(
        external_ref=LINEAR.external_ref(), state="Done", runtime=_runtime(LINEAR),
    )
    assert result == {"status": "unavailable", "reason": "timeout"}
    assert len(read_evidence(home)["notices"]) == 1


def test_credentials_come_from_the_process_environment(monkeypatch) -> None:
    for key in tools.TRACKER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    assert tools._env() == {"LINEAR_API_KEY": "k"}
