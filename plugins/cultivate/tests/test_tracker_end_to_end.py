"""CI-runnable end to end: installed poller -> host trust -> tools -> filing -> publication -> closure.

The live run against a real bot identity is an operator check after merge. Everything
here runs the installed ``tracker-poller`` script and the real host ingest path over
recorded exchanges; only the network, ``gh`` and the Chainlink binary are replaced.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import re
import subprocess
import sys
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from langchain_core.messages import ToolMessage

from mimir import models, pollers
from mimir.access_control import ToolRegistry, classify_protected_result, create_auth_context
from mimir.agent import _effective_persisted_integrity
from mimir.models import TurnRecord
from mimir.runtime import dispatch_session_synthesis
from mimir.turn_logger import TurnLogger
from mimir.event_logger import init_logger
from mimir.plugins.core import policy_scope
from mimir.plugins.defaults import seed_plugin_defaults
from mimir.plugins.manager import load_plugins
from mimir.plugins.skills import install_skill, selected_skills
from mimir.poller_tracker import safe_event_id
from mimir.pollers import discover_pollers, run_poller
from mimir_cultivate.tracker import drain as tracker_drain
from mimir_cultivate.tracker import tools
from mimir_cultivate.tracker import transport as tracker_transport
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.config import TrackerAdapterConfig, TrackerConfig
from mimir_cultivate.tracker.evidence import read_evidence
from mimir_cultivate.tracker.work import ChainlinkWork
from mimir_cultivate.worklink import merge_closure as closure
from mimir_cultivate.worklink import orchestrator
from mimir_cultivate.worklink.evidence import TestResult, WorklinkEvidence
from mimir_cultivate.worklink.planning import LEAF_TEMPLATE_MARKDOWN
from tests.test_worklink_merge_closure import (  # noqa: F401  (autouse fixture)
    PR_URL as MERGE_PR_URL,
    Tracker,
    configured_home,
    evidence,
    forge,
    git_runner,
    selected_repository_provider,
)
from tracker_fakes import FakeChainlink
from tracker_replay import ReplayTransport, load_fixture

SCRIPT = "scripts/poller.py"
GITHUB_REF = "acme/widgets#42"
GITHUB_URL = "https://github.com/acme/widgets/issues/42"
TOKEN = "ghp_" + "a1B2c3D4" * 4 + "e5F6"
ENV = {
    "GITHUB_TOKEN": TOKEN,
    "MIMIR_GITHUB_SELF_LOGIN": "MimirBot",
    "MIMIR_GITHUB_TRUSTED_BOT_LOGINS": "helperbot",
    "LINEAR_API_KEY": "lin_api_distinct_value",
    "JIRA_SITE": "acme.atlassian.net",
    "JIRA_EMAIL": "bot@acme.test",
    "JIRA_API_TOKEN": "jira_token_distinct",
}
CASES: dict[str, dict[str, Any]] = {
    "github_issues": {
        "ref": GITHUB_REF, "url": GITHUB_URL,
        "section": {"repos": ["acme/widgets"]},
    },
    "linear": {
        "ref": "ENG-123", "url": "https://linear.app/acme/issue/ENG-123", "section": {},
    },
    "jira": {
        "ref": "PROJ-45", "url": "https://acme.atlassian.net/browse/PROJ-45",
        "section": {
            "account_id": "acct-bot", "projects": ["PROJ"], "trusted_groups": ["jira-trusted"],
        },
    },
}


def exchanges(adapter: str, scenario: str) -> list[dict[str, Any]]:
    return copy.deepcopy(load_fixture(adapter, scenario)["exchanges"])


def listing(adapter: str) -> list[dict[str, Any]]:
    """The recorded listing, narrowed to the one issue the end-to-end thread is about."""

    recorded = exchanges(adapter, "list_assigned")
    if adapter == "github_issues":
        body = recorded[0]["response"]["json"]
        recorded[0]["response"]["json"] = [item for item in body if item["number"] == 42]
    elif adapter == "linear":
        nodes = recorded[1]["response"]["json"]["data"]["issues"]["nodes"]
        node = dict(nodes[0], id="iss-123", identifier="ENG-123",
                    url="https://linear.app/acme/issue/ENG-123/fix-login-crash")
        recorded[1]["response"]["json"]["data"]["issues"]["nodes"] = [node]
    else:
        body = recorded[0]["response"]["json"]
        body["issues"] = [item for item in body["issues"] if item["key"] == "PROJ-45"]
    return recorded


class Rig:
    """A temp home with the skill installed, plus the queue of replay transports."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str,
                 *, close_on_merge: bool = False) -> None:
        self.monkeypatch = monkeypatch
        self.adapter = adapter
        self.case = CASES[adapter]
        self.home, self.repo = configured_home(tmp_path, monkeypatch)
        (self.home / "logs").mkdir()
        init_logger(self.home / "logs" / "events.jsonl", session_id="tracker-e2e")
        section = {"close_on_merge": close_on_merge, "adapters": {adapter: self.case["section"]}}
        (self.home / "worklink.yaml").write_text(
            yaml.safe_dump({"repository": "example/project", "tracker": section}), encoding="utf-8"
        )
        self.state = self.home / "state" / "pollers" / "tracker-poller"
        monkeypatch.setenv("MIMIR_HOME", str(self.home))
        monkeypatch.setenv("STATE_DIR", str(self.state))
        for key, value in ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MIMIR_SOURCE_DIR", str(Path(__file__).resolve().parents[3]))
        monkeypatch.setenv("CHAINLINK_BIN", "chainlink-e2e-value")
        self.selected = load_plugins(enable="cultivate")
        candidate, = self.selected.policy.selected_plugin_candidates
        seed_plugin_defaults(self.home, selected=(candidate,))
        for skill in selected_skills():
            if skill.name == "tracker-poller":
                install_skill(self.home, skill)
        self.poller = next(
            p for p in discover_pollers(
                self.home / "skills", state_root=self.home / "state" / "pollers",
                policy=self.selected.policy,
            ) if p.name == "tracker-poller"
        )
        self.transports: deque[ReplayTransport] = deque()
        self.used: list[ReplayTransport] = []

        def next_transport() -> ReplayTransport:
            transport = self.transports.popleft()
            self.used.append(transport)
            return transport

        monkeypatch.setattr(tracker_transport, "default_transport", next_transport)
        self.chainlink = FakeChainlink()
        monkeypatch.setattr(tools, "ChainlinkWork", lambda cwd: ChainlinkWork("chainlink", cwd, self.chainlink))
        monkeypatch.setattr(
            pollers, "_github_author_is_trusted",
            lambda repo, author, token, *, timeout=None: author in {"alice", "helperbot"},
        )
        script = self.home / "skills" / "cultivate-tracker-poller" / SCRIPT
        spec = importlib.util.spec_from_file_location("_e2e_tracker_poller", script)
        assert spec is not None and spec.loader is not None
        self.module = importlib.util.module_from_spec(spec)
        sys.modules["_e2e_tracker_poller"] = self.module
        spec.loader.exec_module(self.module)
        monkeypatch.setattr(
            self.module, "ChainlinkWork", lambda cwd=None: ChainlinkWork("chainlink", cwd, self.chainlink)
        )

    def queue(self, *recorded: list[dict[str, Any]]) -> None:
        self.transports.append(ReplayTransport([e for part in recorded for e in part]))

    def poll(self, capsys: pytest.CaptureFixture[str]) -> list[str]:
        capsys.readouterr()
        self.module.main()
        return [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    def finish(self) -> None:
        assert not self.transports, "recorded transports were never built"
        for transport in self.used:
            transport.assert_authorized()
            transport.finish()

    async def deliver(self, lines: list[str]) -> list[models.AgentEvent]:
        """Run the host ingest over the poller's stdout lines."""

        echo = self.home / "echo.py"
        echo.write_text(
            "import sys\nfor line in " + repr(lines) + ":\n    print(line)\n", encoding="utf-8"
        )
        poller = replace(self.poller, command=f"{sys.executable} {echo}")
        events: list[models.AgentEvent] = []

        async def enqueue(event: models.AgentEvent) -> bool:
            events.append(event)
            return True

        await run_poller(poller, enqueue=enqueue, home=self.home)
        return events

    def auth(self, event: models.AgentEvent):
        with policy_scope(self.selected.policy):
            return create_auth_context(
                event, policy=self.selected.policy, enforce=True,
                ifc_labels=models.InformationFlowLabels(),
            )

    def runtime(self, event: models.AgentEvent) -> SimpleNamespace:
        targets = self.auth(event).plugin_state("cultivate").tracker_issue_targets
        context = SimpleNamespace(
            tracker_issue_targets=targets, ifc_state=None, ifc_labels=models.InformationFlowLabels(),
        )
        return SimpleNamespace(context=context)


@pytest.fixture
def capsys_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def make(adapter: str, **kwargs: Any) -> Rig:
        return Rig(tmp_path, monkeypatch, adapter, **kwargs)

    return make


def test_manifest_authority_is_tracker_tools_only(capsys_rig) -> None:
    from mimir_cultivate.policy import TRACKER_TOOL_NAMES

    rig = capsys_rig("github_issues")
    invalid_entries: list[tuple[Path, str, str]] = []
    invalid_manifests: list[tuple[Path, str]] = []
    discovered = [
        p for p in discover_pollers(
            rig.home / "skills", state_root=rig.home / "state" / "pollers",
            policy=rig.selected.policy, invalid_entries=invalid_entries,
            invalid_manifests=invalid_manifests,
        ) if p.name == "tracker-poller"
    ]
    [poller] = discovered
    assert invalid_entries == [] and invalid_manifests == []
    authority = poller.authority
    assert authority is not None
    assert set(authority.capabilities) == set(TRACKER_TOOL_NAMES)
    assert len(authority.capabilities) == len(TRACKER_TOOL_NAMES)
    assert poller.batch_size == 1
    assert poller.trust_source == "tracker"
    assert poller.hooks is not None and poller.hooks.trust_attestor is not None
    declared = next(
        entry for entry in json.loads(poller.manifest_path.read_text(encoding="utf-8"))["pollers"]
        if entry["name"] == "tracker-poller"
    )
    assert declared["authority"]["scoped_roots"] == ["state"]
    assert not {"write_file", "edit_file", "replace_file"} & set(authority.capabilities)
    assert authority.sink_policies == ()
    assert authority.saga_full_corpus_read is False
    assert poller.pass_env == (
        "GITHUB_TOKEN", "MIMIR_GITHUB_SELF_LOGIN", "MIMIR_GITHUB_TRUSTED_BOT_LOGINS",
        "LINEAR_API_KEY", "JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "MIMIR_HOME", "MIMIR_SOURCE_DIR", "CHAINLINK_BIN",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", sorted(CASES))
async def test_item_attests_through_run_poller(
    adapter: str, capsys_rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = capsys_rig(adapter)
    case = rig.case
    read = exchanges(adapter, "attest_trusted")
    rig.queue(listing(adapter), read[1:] if adapter == "linear" else read)
    rig.queue(read)

    lines = rig.poll(capsys)
    records = [json.loads(line) for line in lines]
    [item] = records
    assert "signal" not in item
    assert (item["adapter"], item["ref"], item["url"], item["kind"]) == (
        adapter, case["ref"], case["url"], "triage",
    )
    assert safe_event_id(item["attested_event_id"]) == item["attested_event_id"]
    assert not (set(ENV.values()) & {item["prompt"]})
    secrets = [value for key, value in ENV.items() if key != "JIRA_SITE"]
    assert all(value not in line for value in secrets for line in lines)

    [event] = await rig.deliver(lines)
    assert event.content == item["prompt"]
    [source] = event.ifc_labels.sources
    assert source.integrity == "trusted"
    [delivered] = event.extra["items"]
    assert delivered["tracker_attested"] is True
    assert delivered["attested_event_id"] == item["attested_event_id"]
    targets = rig.auth(event).plugin_state("cultivate").tracker_issue_targets
    assert targets == frozenset({(adapter, case["ref"], case["url"])})
    rig.finish()


class _LinearDown:
    """Routes GitHub to a recorded replay and fails every Linear request."""

    def __init__(self, github: ReplayTransport) -> None:
        self.github = github
        self.linear_requests = 0

    def request(self, method: str, url: str, **kwargs: Any):
        if "linear.app" in url:
            self.linear_requests += 1
            raise tracker_transport.TrackerUnavailable("transport")
        return self.github.request(method, url, **kwargs)


def test_linear_outage_does_not_withhold_the_github_items(
    capsys_rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = capsys_rig("github_issues")
    section = {"adapters": {
        "github_issues": CASES["github_issues"]["section"], "linear": CASES["linear"]["section"],
    }}
    (rig.home / "worklink.yaml").write_text(
        yaml.safe_dump({"repository": "example/project", "tracker": section}), encoding="utf-8"
    )
    read = exchanges("github_issues", "attest_trusted")
    github = ReplayTransport([*listing("github_issues"), *read])
    routed = _LinearDown(github)
    rig.transports.append(routed)
    rig.queue(read)

    capsys.readouterr()
    rig.module.main()
    captured = capsys.readouterr()
    stderr = captured.err
    [item] = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert (item["adapter"], item["ref"]) == ("github_issues", GITHUB_REF)
    assert routed.linear_requests >= 1
    assert (rig.state / "cursor.json").exists()
    assert "adapter_unavailable:linear:transport" in stderr
    assert not any("fatal" in line for line in stderr.splitlines())


def _leaf_description() -> str:
    return LEAF_TEMPLATE_MARKDOWN


@pytest.mark.asyncio
async def test_attested_filing_preserves_turn_integrity_and_dispatches_synthesis(
    capsys_rig, capsys: pytest.CaptureFixture[str],
) -> None:
    rig = capsys_rig("github_issues")
    read = exchanges("github_issues", "attest_trusted")
    rig.queue(listing("github_issues"), read)
    rig.queue(read)
    [event] = await rig.deliver(rig.poll(capsys))
    with policy_scope(rig.selected.policy):
        auth = create_auth_context(
            event, policy=rig.selected.policy, enforce=True, ifc_labels=event.ifc_labels,
        )
    args = {"external_ref": f"github_issues:{GITHUB_REF}", "kind": "leaf",
            "title": "Fix login crash", "description": _leaf_description()}
    with policy_scope(rig.selected.policy):
        decision = ToolRegistry().authorize_tool(
            "tracker_file_work", auth, arguments=args, enforce=True, policy=rig.selected.policy,
        )
        assert decision.allowed and not decision.would_block
        result = tools.tracker_file_work.func(**args, runtime=SimpleNamespace(context=auth))
        assert result["status"] == "created"
        labels = classify_protected_result(
            "tracker_file_work", args, auth, decision,
            result=ToolMessage(content=json.dumps(result), tool_call_id="filing"),
        )
        assert labels is None, labels
    current = auth.ifc_state.current(auth.ifc_labels)
    turns_path = rig.home / "logs" / "turns.jsonl"
    turn = TurnRecord(
        ts="2026-10-30T12:00:00+00:00", turn_id="trusted-tracker-filing",
        session_id="tracker-session", saga_session_id="tracker-session",
        trigger=event.trigger, channel_id=event.channel_id, input=event.content,
        output="Filed work", tool_call_count=1,
        integrity=_effective_persisted_integrity(current, auth),
        integrity_source_counts={
            f"{source.integrity}/{source.integrity_effect}": sum(
                item.integrity == source.integrity and item.integrity_effect == source.integrity_effect
                for item in current.sources
            ) for source in current.sources
        },
        untrusted_active_ingest_domains=sorted({
            source.domain for source in current.sources if source.has_untrusted_active_ingest
            and source.domain is not None
        }),
    )
    await TurnLogger(turns_path).write(turn)
    [record] = [json.loads(line) for line in turns_path.read_text().splitlines()]
    assert record["integrity"] == "trusted"
    assert record["untrusted_active_ingest_domains"] == []
    assert "untrusted/active_ingest" not in record["integrity_source_counts"]
    queued = []

    class Dispatcher:
        async def enqueue(self, item):
            queued.append(item)
            return True

    async def log_event(_name, **_fields):
        pass

    assert await dispatch_session_synthesis(
        SimpleNamespace(saga_session_id="tracker-session", channel_id=event.channel_id,
                        source_acl=None),
        config=SimpleNamespace(turns_log=turns_path, saga_session_idle_minutes=10,
                               plugin_manager=rig.selected),
        dispatcher=Dispatcher(), log_event_fn=log_event,
    ) is True
    assert len(queued) == 1
    rig.finish()


@pytest.mark.asyncio
async def test_filing_then_comment_in_one_trusted_turn_and_real_taint_refuses_writes(
    capsys_rig, capsys: pytest.CaptureFixture[str],
) -> None:
    rig = capsys_rig("github_issues")
    read = exchanges("github_issues", "attest_trusted")
    rig.queue(listing("github_issues"), read)
    rig.queue(read)
    [event] = await rig.deliver(rig.poll(capsys))
    with policy_scope(rig.selected.policy):
        auth = create_auth_context(
            event, policy=rig.selected.policy, enforce=True, ifc_labels=event.ifc_labels,
        )
        ref = f"github_issues:{GITHUB_REF}"
        args = {"external_ref": ref, "kind": "leaf", "title": "Fix login crash",
                "description": _leaf_description()}
        decision = ToolRegistry().authorize_tool(
            "tracker_file_work", auth, arguments=args, enforce=True, policy=rig.selected.policy,
        )
        runtime = SimpleNamespace(context=auth)
        filed = tools.tracker_file_work.func(**args, runtime=runtime)
        assert filed["status"] == "created"
        assert classify_protected_result("tracker_file_work", args, auth, decision, result=filed) is None
        rig.queue(exchanges("github_issues", "comment"))
        comment_args = {"external_ref": ref, "body": "Thanks, filed."}
        comment_decision = ToolRegistry().authorize_tool(
            "tracker_comment", auth, arguments=comment_args, enforce=True, policy=rig.selected.policy,
        )
        assert comment_decision.allowed
        posted = tools.tracker_comment.func(**comment_args, runtime=runtime)
        assert posted == {"status": "posted", "reason": ""}
        assert classify_protected_result(
            "tracker_comment", comment_args, auth, comment_decision, result=posted,
        ) is None
        state_args = {"external_ref": ref, "state": "closed"}
        state_decision = ToolRegistry().authorize_tool(
            "tracker_set_state", auth, arguments=state_args, enforce=True, policy=rig.selected.policy,
        )
        unsupported = tools.tracker_set_state.func(**state_args, runtime=runtime)
        assert unsupported == {"status": "unsupported", "reason": "github_issues"}
        assert classify_protected_result(
            "tracker_set_state", state_args, auth, state_decision, result=unsupported,
        ) is None
        # A valid-looking status cannot clear actual untrusted active ingest.
        source = next(iter(event.ifc_labels.sources))
        auth.ifc_state.merge(models.InformationFlowLabels().with_source(
            replace(source, integrity="untrusted", resource_id="withheld-activity"),
        ))
        assert _effective_persisted_integrity(auth.ifc_state.current(), auth) == "untrusted"
        assert tools.tracker_comment.func(**comment_args, runtime=runtime)["reason"] == "tainted_turn"
        assert tools.tracker_file_work.func(**args, runtime=runtime)["reason"] == "tainted_turn"
        assert tools.tracker_set_state.func(**state_args, runtime=runtime)["reason"] == "tainted_turn"
    rig.finish()


@pytest.mark.asyncio
async def test_tracker_status_exemption_fails_closed_for_scope_shape_and_undeclared_plugin(
    capsys_rig, capsys: pytest.CaptureFixture[str],
) -> None:
    rig = capsys_rig("github_issues")
    read = exchanges("github_issues", "attest_trusted")
    rig.queue(listing("github_issues"), read)
    rig.queue(read)
    [event] = await rig.deliver(rig.poll(capsys))
    with policy_scope(rig.selected.policy):
        auth = create_auth_context(
            event, policy=rig.selected.policy, enforce=True, ifc_labels=event.ifc_labels,
        )
        args = {"external_ref": f"github_issues:{GITHUB_REF}", "kind": "leaf"}
        decision = ToolRegistry().authorize_tool(
            "tracker_file_work", auth, arguments=args, enforce=True, policy=rig.selected.policy,
        )
        result = {"status": "created", "reason": "", "kind": "leaf", "missing": []}
        for invalid_args, invalid_result in (
            ({**args, "external_ref": "github_issues:acme/widgets#43"}, result),
            (args, {**result, "tracker_text": "injected"}),
            (args, {**result, "reason": "injected prose!"}),
            (args, {**result, "status": "unexpected"}),
            (args, {**result, "missing": ["tracker supplied prose"]}),
        ):
            labels = classify_protected_result(
                "tracker_file_work", invalid_args, auth, decision, result=invalid_result,
            )
            assert labels is not None
            assert any(source.has_untrusted_active_ingest for source in labels.sources)
        denied = classify_protected_result(
            "tracker_file_work", args, auth, replace(decision, would_block=True), result=result,
        )
        assert denied is not None and any(source.has_untrusted_active_ingest for source in denied.sources)
        failed = classify_protected_result(
            "tracker_file_work", args, auth, decision, result=result, failed=True,
        )
        assert failed is not None and any(source.has_untrusted_active_ingest for source in failed.sources)
        undeclared = classify_protected_result(
            "unsupported_operation", args, auth, decision, result=result,
        )
        assert undeclared is not None
        assert any(source.domain == "unknown" and source.has_untrusted_active_ingest
                   for source in undeclared.sources)
    rig.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,args_extra,result", [
    ("tracker_comment", {"body": "Thanks, filed."}, {"status": "posted", "reason": ""}),
    ("tracker_set_state", {"state": "closed"}, {"status": "applied", "reason": ""}),
    ("tracker_file_work", {"kind": "leaf"},
     {"status": "created", "reason": "", "kind": "leaf", "missing": []}),
])
@pytest.mark.parametrize("invalid_guard", ["extra_key", "non_cultivate_owner"])
async def test_tracker_status_exact_shape_and_owner_guards_taint_turn(
    tool_name: str, args_extra: dict, result: dict, invalid_guard: str,
    capsys_rig, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = capsys_rig("github_issues")
    read = exchanges("github_issues", "attest_trusted")
    rig.queue(listing("github_issues"), read)
    rig.queue(read)
    [event] = await rig.deliver(rig.poll(capsys))
    with policy_scope(rig.selected.policy):
        auth = create_auth_context(
            event, policy=rig.selected.policy, enforce=True, ifc_labels=event.ifc_labels,
        )
        args = {"external_ref": f"github_issues:{GITHUB_REF}", **args_extra}
        decision = ToolRegistry().authorize_tool(
            tool_name, auth, arguments=args, enforce=True, policy=rig.selected.policy,
        )
        assert decision.allowed and not decision.would_block
        assert _effective_persisted_integrity(auth.ifc_state.current(auth.ifc_labels), auth) == "trusted"
        # Prove every other precondition admits the otherwise identical result.
        assert classify_protected_result(tool_name, args, auth, decision, result=result) is None
        if invalid_guard == "extra_key":
            result = {**result, "tracker_text": "untrusted tracker-supplied message"}
        else:
            registered = rig.selected.policy.tools[tool_name]
            assert registered.owner == "cultivate" and registered.result_domain == "tracker_status"
            monkeypatch.setitem(
                rig.selected.policy._tools, tool_name,
                replace(registered, owner="other_tracker_plugin"),
            )
        # Both raw results and the production ToolMessage form must fail closed.
        for payload in (result, ToolMessage(content=json.dumps(result), tool_call_id="status")):
            labels = classify_protected_result(tool_name, args, auth, decision, result=payload)
            assert labels is not None
            assert any(source.domain == "tracker_status" and source.principal is None
                       and source.has_untrusted_active_ingest for source in labels.sources)
            auth.ifc_state.merge(labels)
            assert _effective_persisted_integrity(auth.ifc_state.current(), auth) == "untrusted"
        assert tools.tracker_comment.func(
            external_ref=args["external_ref"], body="Follow-up", runtime=SimpleNamespace(context=auth),
        )["reason"] == "tainted_turn"
    rig.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_on_merge", [False, True])
async def test_assigned_issue_is_triaged_filed_published_and_closed(
    close_on_merge: bool, capsys_rig, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = capsys_rig("github_issues", close_on_merge=close_on_merge)
    read = exchanges("github_issues", "attest_trusted")
    rig.queue(listing("github_issues"), read)
    rig.queue(read)

    lines = rig.poll(capsys)
    [event] = await rig.deliver(lines)
    [item] = json.loads(lines[0]),
    assert event.content == item["prompt"] and len(lines) == 1
    runtime = rig.runtime(event)

    rig.queue(exchanges("github_issues", "comment"))
    posted = tools.tracker_comment.func(
        external_ref=f"github_issues:{GITHUB_REF}", body="Thanks, filed.", runtime=runtime
    )
    assert posted["status"] == "posted"

    args = {"external_ref": f"github_issues:{GITHUB_REF}", "kind": "leaf", "title": "Fix login crash",
            "description": _leaf_description(), "runtime": runtime}
    assert tools.tracker_file_work.func(**args)["status"] == "created"
    assert tools.tracker_file_work.func(**args)["status"] == "exists"
    [issue_id] = rig.chainlink.issues
    filed = rig.chainlink.issues[issue_id]
    assert issue_id == 9137
    assert f"External: github_issues:{GITHUB_REF} {GITHUB_URL}" in filed["description"]
    assert "worklink:ready" in filed["labels"]

    rig.queue(listing("github_issues"), read)
    assert rig.poll(capsys) == []
    cursor = json.loads((rig.state / "cursor.json").read_text())
    [entry] = cursor["issues"].values()
    assert entry["triage"]["state"] == "filed"
    assert "Fix login crash" not in json.dumps(cursor)

    issue = orchestrator.IssueContext(issue_id, "Fix login crash", filed["description"], {"worklink:ready"})
    template = rig.home / "order.md"
    template.write_text("{title}\n{description}\n{backend} {test_command} {labels} {parent_id}\n")
    order = orchestrator.render_work_order(
        issue, template_path=template, backend_name="fake", test_command="echo ok"
    )
    assert "External:" not in order
    assert "External:" not in orchestrator.render_work_item(issue)
    assert "External:" not in orchestrator._epic_prompt(issue)

    title, body = _open_pr(rig, issue)
    assert body.startswith("Closes #42")
    ref_pattern = re.compile(r"(?<![0-9A-Za-z])9137(?![0-9A-Za-z])")
    for text in (title, body):
        assert not ref_pattern.search(text) and "chainlink" not in text.lower()
    closing = re.findall(r"(?i)\b(?:closes|fixes|resolves)\s+\S*\d\S*", body)
    assert closing == ["Closes #42"]

    rig.finish()
    evidence(rig.home, issue_id)
    tracker = Tracker(issue_id)
    tracker.description = filed["description"]
    outcomes = closure.reconcile_merged_leaves(
        rig.home, chainlink_runner=tracker, gh_runner=forge(issue_id), git_runner=git_runner(rig.repo),
    )
    assert len(outcomes) == 1
    closures = read_evidence(rig.home)["items"] if (rig.home / "state/worklink/tracker/evidence.json").exists() else {}
    if not close_on_merge:
        assert closures == {}
        return
    assert closures[f"github_issues:{GITHUB_REF}"]["closure"]["stage"] == "requested"
    close = [{"request": {"method": "GET", "url": "https://api.github.com/repos/acme/widgets/issues/42"},
              "response": {"status": 200, "json": {"number": 42, "state": "open"}}}]
    replay = ReplayTransport(close + exchanges("github_issues", "close_default"))
    config = TrackerConfig(True, {"github_issues": TrackerAdapterConfig(
        name="github_issues", repos=("acme/widgets",))})
    gh = forge(issue_id)
    drained = tracker_drain.drain(rig.home, config, ENV, replay, gh, lambda notice: None)
    assert drained == 1
    result = read_evidence(rig.home)["items"][f"github_issues:{GITHUB_REF}"]["closure"]
    assert result["result"] == "closed" and result["stage"] == "done"
    before = len(replay.requests)
    assert tracker_drain.drain(rig.home, config, ENV, replay, gh, lambda notice: None) == 0
    assert len(replay.requests) == before
    replay.assert_authorized()
    replay.finish()


def _open_pr(rig: Rig, issue: orchestrator.IssueContext) -> tuple[str, str]:
    checkout = rig.home / "checkout"
    checkout.mkdir()
    calls: list[list[str]] = []

    def runner(args: list[str]):
        calls.append(args)
        if args[:4] == ["git", "-C", str(checkout), "config"]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:acme/widgets.git\n", "")
        return subprocess.CompletedProcess(args, 0, "https://github.com/acme/widgets/pull/4\n", "")

    result = WorklinkEvidence(
        issue=issue.issue_id, attempt=1, backend="opencode", branch=f"issue/{issue.issue_id}-a1",
        checkout=str(checkout), started_at="2026-10-01T10:00:00+00:00",
        finished_at="2026-10-01T10:05:00+00:00", files_changed=["change.txt"],
        diff_stat="1 file changed", commands=[],
        tests=TestResult(cmd="./tools/verify", exit_code=0, gate_run_id="0" * 32),
        pr_url=None, status="completed", model="openai/test-model", transcript=None,
    )
    section = (
        f"Implements Chainlink issue #{issue.issue_id} on issue/{issue.issue_id}-a1. "
        f"Fixes #{issue.issue_id}. See chainlink-{issue.issue_id} and #{issue.issue_id}."
    )
    orchestrator._open_pr(
        checkout, issue, f"issue/{issue.issue_id}-a1", result, base="main", runner=runner,
        pr_body_section=section,
        subject=orchestrator._fallback_commit_subject(issue.title, issue_id=issue.issue_id),
    )
    command = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    assert command[command.index("--base") + 1] == "main"
    return command[command.index("--title") + 1], command[command.index("--body") + 1]
