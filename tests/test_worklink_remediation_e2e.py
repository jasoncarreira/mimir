"""Composed retained-remediation coverage across the production seams."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mimir import access_control as ac
from mimir._context import reset_current_turn, set_current_turn
from mimir.agent import _create_turn_auth_context, _initialize_ifc_labels
from mimir.event_logger import init_logger
from mimir.models import InformationFlowLabels, TurnContext
from mimir.pollers import discover_pollers, run_poller
from mimir.readonly_backend import FileToolRouter, WriteGuardBackend, build_file_tool_routes
from mimir.worklink.backends.feature_factory import FactoryStatus
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.dispatch_failures import dispatch_failure_state_dir, record_failure
from mimir.worklink.factory_state import FactoryRunRecord, save_factory_record


class _CapturingEnqueue:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event, **_kwargs: object) -> bool:
        self.events.append(event)
        return True


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 - deterministic model
        return self


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def remediation_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mimir.worklink import worker_client

    home = tmp_path / "home"
    state_root = home / "state" / "pollers"
    state_root.mkdir(parents=True)
    init_logger(home / "logs" / "events.jsonl", session_id="remediation-e2e")
    controller_repo = tmp_path / "controller-repo"
    controller_repo.mkdir()
    retained_root = tmp_path / "worklink"
    checkout = retained_root / "mimir" / "1811-1" / "checkout"
    sandbox = checkout / ".factory-sandboxes" / "chainlink-1811"
    sandbox.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "instructions.txt").write_text("external instructions\n", encoding="utf-8")

    _git(sandbox, "init", "-q")
    _git(sandbox, "checkout", "-q", "-b", "feature/chainlink-1811")
    _git(sandbox, "config", "user.name", "untrusted")
    _git(sandbox, "config", "user.email", "untrusted@example.invalid")
    (sandbox / ".gitignore").write_text(".factory/\n", encoding="utf-8")
    (sandbox / "fix.txt").write_text("before\n", encoding="utf-8")
    _git(sandbox, "add", ".gitignore", "fix.txt")
    _git(sandbox, "commit", "-q", "-m", "seed")

    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("WORKLINK_REPO", str(controller_repo))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{external}:ro")
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", retained_root)
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh",
        lambda _record: False,
    )

    incident = record_failure(
        dispatch_failure_state_dir(home),
        issue_id=1811,
        attempt=1,
        exit_status=1,
        error="retained checkout requires remediation",
        log_path="run.log",
        run_id="chainlink-1811",
        work_path=str(sandbox),
    )
    status = FactoryStatus(
        run_id="chainlink-1811",
        valid=True,
        sandbox_path=str(sandbox),
        status="needs-human",
    )
    record = FactoryRunRecord(
        run_id="chainlink-1811",
        issue_id=1811,
        attempt=1,
        repository="owner/mimir",
        base_ref="main",
        branch="feature/chainlink-1811",
        launcher="/opt/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=LaunchHandle("local_subprocess", "99999999", 1),
        status=status,
        observed_at=None,
        controller_phase="parked",
    )
    save_factory_record(home, record)
    return SimpleNamespace(
        home=home,
        state_root=state_root,
        controller_repo=controller_repo,
        retained_root=retained_root,
        checkout=checkout,
        sandbox=sandbox,
        external=external,
        incident=incident,
        record=record,
    )


async def _trusted_turn(case):
    skills = Path(__file__).parents[1] / "mimir" / "optional-skills"
    ready = next(
        config
        for config in discover_pollers(skills, state_root=case.state_root)
        if config.name == "worklink-ready-queue"
    )
    capture = _CapturingEnqueue()
    emitted = await run_poller(
        replace(ready, command=f"{sys.executable} scripts/poller.py"),
        enqueue=capture,
        home=case.home,
    )
    assert emitted >= 1
    [event] = [
        candidate
        for candidate in capture.events
        if candidate.extra["items"][0].get("issue_id") == 1811
    ]
    labels = _initialize_ifc_labels(event)
    auth = _create_turn_auth_context(
        event, None, policy_version=None, enforce=True, ifc_labels=labels,
    )
    assert auth.retained_factory_scope is not None
    return event, labels, auth


@pytest.mark.asyncio
async def test_retained_remediation_runs_from_incident_turn_to_exact_recovery_dispatch(
    remediation_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One scripted turn crosses every remediation seam before recovery."""
    from deepagents import create_deep_agent

    from mimir._deepagents_patches import install_deepagents_grep_context_tool
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
    from mimir.project_tests import ProjectTestResult
    from mimir.tools import repo as repo_module
    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir.tools.registry import worklink_resume
    from mimir.worklink import autonomy, detached_dispatch, worker_client

    case = remediation_case
    other_incident = record_failure(
        dispatch_failure_state_dir(case.home),
        issue_id=1812,
        attempt=2,
        exit_status=1,
        error="unrelated failure",
        log_path="other.log",
    )
    event, labels, auth = await _trusted_turn(case)
    scope = auth.retained_factory_scope
    assert scope is not None
    assert labels.has_untrusted_active_ingest is False

    owner_calls: list[tuple[str, ...]] = []

    def owner_control(checkout, argv, *, env, timeout, output_limit):
        assert checkout == case.sandbox
        owner_calls.append(tuple(argv))
        return subprocess.run(
            argv,
            env={**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            check=False,
        )

    class FileClient:
        def factory_file_operation(self, operation, relative_path, **arguments):
            target = case.checkout / relative_path
            if operation == "write_file":
                target.write_text(arguments["content"], encoding="utf-8")
                return {"status": "ok", "path": relative_path}
            old = arguments["old_string"]
            content = target.read_text(encoding="utf-8")
            assert content.count(old) == 1
            target.write_text(content.replace(old, arguments["new_string"]), encoding="utf-8")
            return {"status": "ok", "path": relative_path, "occurrences": 1}

    monkeypatch.setattr(worker_client, "run_factory_control", owner_control)
    monkeypatch.setattr(
        worker_client.WorkerClient,
        "for_factory_checkout",
        lambda *args, **kwargs: FileClient(),
    )

    async def passing_test(self, selectors=(), *, suite=None):
        assert self._retained_scope == scope
        return ProjectTestResult(True, "tests_passed", 0)

    monkeypatch.setattr(repo_module.RepoProjectTests, "execute", passing_test)
    monkeypatch.setattr(
        autonomy,
        "make_claims",
        lambda home: SimpleNamespace(_active_worklink_lock_ids_for_scope=lambda **kwargs: set()),
    )
    launches: list[dict[str, object]] = []
    monkeypatch.setattr(
        detached_dispatch,
        "launch_detached_worklink",
        lambda **kwargs: launches.append(kwargs)
        or detached_dispatch.DetachedWorklinkProcess(
            4321, dispatch_failure_state_dir(case.home) / "run-epic-1811.log",
        ),
    )

    backend = FileToolRouter(
        default=WriteGuardBackend(case.home, ["state"], guard_outside_root=True),
        routes=build_file_tool_routes([
            (str(case.external), "ro"),
            (str(case.retained_root), "retained"),
        ]),
    )
    calls = [
        ("read_file", {"file_path": str(case.sandbox / "fix.txt")}),
        ("write_file", {"file_path": str(case.sandbox / "new.txt"), "content": "new\n"}),
        ("edit_file", {
            "file_path": str(case.sandbox / "fix.txt"),
            "old_string": "before",
            "new_string": "after",
        }),
        ("repo_status", {"repository": scope.repository, "pull_request": scope.issue_id}),
        ("repo_diff", {"repository": scope.repository, "pull_request": scope.issue_id}),
        ("repo_test", {"repository": scope.repository, "pull_request": scope.issue_id}),
        ("repo_stage", {
            "repository": scope.repository,
            "pull_request": scope.issue_id,
            "paths": ["fix.txt", "new.txt"],
        }),
        ("repo_commit", {
            "repository": scope.repository,
            "pull_request": scope.issue_id,
            "paths": ["fix.txt", "new.txt"],
            "message": "retained remediation",
        }),
        ("worklink_resume", {}),
    ]
    messages = [
        AIMessage(content="", tool_calls=[{
            "name": name,
            "args": arguments,
            "id": f"remediation-{index}",
            "type": "tool_call",
        }])
        for index, (name, arguments) in enumerate(calls)
    ]
    messages.append(AIMessage(content="recovery dispatched"))
    model = _ScriptedModel(messages=iter(messages))
    install_deepagents_grep_context_tool()
    tools = [
        tool for tool in repo_module.REPO_TOOLS
        if tool.name in {"repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"}
    ] + [worklink_resume]
    graph = create_deep_agent(
        model=model,
        tools=tools,
        backend=backend,
        middleware=[BudgetGateMiddleware()],
        system_prompt="repair the retained factory checkout",
        context_schema=type(auth),
    )
    turn = TurnContext(
        turn_id="retained-remediation-e2e",
        session_id=event.channel_id,
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=0.0,
        auth_context=auth,
        ifc_labels=labels,
    )
    token = set_current_turn(turn)
    try:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="repair and resume")]}, context=auth,
        )
    finally:
        reset_current_turn(token)

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == len(calls)
    assert all(message.status != "error" for message in tool_messages)
    assert not any("ifc_label_blocked:" in str(message.content) for message in tool_messages)
    assert case.sandbox.joinpath("fix.txt").read_text(encoding="utf-8") == "after\n"
    assert case.sandbox.joinpath("new.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(case.sandbox, "status", "--short") == ""
    assert _git(case.sandbox, "show", "-s", "--format=%s") == "retained remediation"
    assert _git(case.sandbox, "show", "-s", "--format=%an <%ae>") == (
        f"{DEFAULT_USER_NAME} <{DEFAULT_USER_EMAIL}>"
    )
    assert owner_calls
    assert launches and launches[0]["recovery"] == detached_dispatch.FactoryRecoveryIdentity(
        scope.signature, scope.occurrence_id, scope.run_id, scope.attempt, scope.session,
    )

    # This is the exact guarded retirement used by the orchestrator's successful
    # autonomous recovery path. A sibling occurrence must remain byte-for-byte current.
    from mimir.worklink.dispatch_failures import (
        load_failure_state,
        resolve_failure_if_current,
    )

    before_other = load_failure_state(dispatch_failure_state_dir(case.home))["issues"]["1812"]
    assert resolve_failure_if_current(
        dispatch_failure_state_dir(case.home),
        scope.issue_id,
        scope.signature,
        scope.occurrence_id,
    )
    ledger = load_failure_state(dispatch_failure_state_dir(case.home))["issues"]
    assert ledger["1811"]["active"] is False
    assert ledger["1812"] == before_other
    assert ledger["1812"]["occurrence_id"] == other_incident["occurrence_id"]


@pytest.mark.asyncio
async def test_non_incident_prompts_and_other_services_never_receive_retained_authority(
    remediation_case,
) -> None:
    case = remediation_case
    event, labels, auth = await _trusted_turn(case)
    assert auth.retained_factory_scope is not None

    prompts = (
        {"kind": "factory_start", "issue_id": 1811},
        {"kind": "factory_success", "issue_id": 1811},
        {"kind": "worklink_merge_reconciliation", "issue_id": 1811},
    )
    for item in prompts:
        other_event = replace(event, extra={"poller_name": "worklink-ready-queue", "items": [item]})
        other = _create_turn_auth_context(
            other_event, None, policy_version=None, enforce=True,
            ifc_labels=_initialize_ifc_labels(other_event),
        )
        assert other.retained_factory_scope is None
        assert "not an incident" in (other.retained_factory_scope_refusal or "")

    foreign_service = replace(event, service_principal="poller:other-service")
    foreign = _create_turn_auth_context(
        foreign_service, None, policy_version=None, enforce=True,
        ifc_labels=InformationFlowLabels(),
    )
    assert foreign.retained_factory_scope is None
    foreign_principal = ac.get_trusted_service_from_auth_context(foreign)
    if foreign_principal is not None:
        assert case.retained_root not in ac.service_filesystem_read_roots(
            foreign_principal, auth_context=foreign,
        )


@pytest.mark.asyncio
async def test_external_read_taints_the_same_retained_turn_and_blocks_every_effect_class(
    remediation_case,
) -> None:
    case = remediation_case
    _event, _labels, auth = await _trusted_turn(case)
    external = case.external / "instructions.txt"
    source = ac.protected_result_source(
        auth,
        principal="filesystem",
        domain="filesystem",
        resource_id=str(external),
        bridge_instance="filesystem",
    )
    assert (source.integrity, source.integrity_effect) == ("untrusted", "active_ingest")
    tainted = auth.ifc_state.merge(InformationFlowLabels().with_source(source))
    registry = ac.ToolRegistry()
    checks = (
        ("write_file", {"file_path": str(case.sandbox / "blocked.txt")}),
        ("edit_file", {"file_path": str(case.sandbox / "fix.txt")}),
        ("repo_stage", {"repository": "owner/mimir", "pull_request": 1811}),
        ("repo_commit", {"repository": "owner/mimir", "pull_request": 1811}),
        ("worklink_resume", {}),
    )
    for name, arguments in checks:
        decision = registry.authorize_tool(
            name,
            auth,
            enforce=True,
            target_channel=arguments.get("file_path"),
            arguments=arguments,
            ifc_labels=tainted,
        )
        assert decision.allowed is False
        assert decision.reason.startswith("ifc_label_blocked:"), (name, decision.reason)
    assert not (case.sandbox / "blocked.txt").exists()
