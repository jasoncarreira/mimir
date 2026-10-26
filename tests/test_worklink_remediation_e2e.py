"""Composed retained-remediation coverage across the production seams."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import pwd
import grp
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

pytest.importorskip("mimir_cultivate", reason="Worklink remediation belongs to Cultivate")

from mimir import access_control as ac
from mimir._context import reset_current_turn, set_current_turn
from mimir.agent import _create_turn_auth_context, _initialize_ifc_labels
from mimir.cli import main as cli_main
from mimir.event_logger import init_logger
from mimir.models import InformationFlowLabels, TurnContext
from mimir.pollers import discover_pollers, run_poller
from mimir.readonly_backend import FileToolRouter, WriteGuardBackend, build_file_tool_routes
from mimir.plugins.core import policy_scope
from mimir.plugins.manager import load_plugins
from mimir_cultivate.worklink.backends.feature_factory import FactoryStatus
from mimir_cultivate.worklink.compute import LaunchHandle
from mimir_cultivate.worklink.dispatch_failures import (
    dispatch_failure_state_dir,
    load_failure_state,
    record_failure,
)
from mimir_cultivate.worklink.factory_state import FactoryRunRecord, save_factory_record


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
        ["git", "-c", f"safe.directory={path}", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _system_identities():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root with distinct mimir and worklink identities")
    try:
        controller = pwd.getpwnam("mimir")
        worker = pwd.getpwnam("worklink")
        worker_group = grp.getgrnam("worklink")
    except KeyError:
        pytest.skip("requires real mimir and worklink accounts and the worklink group")
    if controller.pw_uid == worker.pw_uid:
        pytest.skip("controller and worker identities must be distinct")
    if worker.pw_gid != worker_group.gr_gid:
        pytest.skip("worklink account must use the worklink group as its primary group")
    if controller.pw_name not in worker_group.gr_mem:
        try:
            controller_groups = os.getgrouplist(controller.pw_name, controller.pw_gid)
        except OSError:
            controller_groups = []
        if worker_group.gr_gid not in controller_groups:
            pytest.skip("mimir account must be a member of the worklink group")
    return SimpleNamespace(
        mimir_uid=controller.pw_uid,
        mimir_gid=controller.pw_gid,
        worklink_uid=worker.pw_uid,
        worklink_gid=worker_group.gr_gid,
    )


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    for candidate in (path, *path.rglob("*")):
        os.chown(candidate, uid, gid, follow_symlinks=False)


def _assert_no_controller_git_processes(controller_uid: int, retained_root: Path) -> None:
    """Fail if the controller identity is currently running Git against retained state."""
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != controller_uid:
                continue
            argv = process.joinpath("cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"git" in argv and os.fsencode(retained_root) in argv:
            raise AssertionError(f"controller uid ran retained git: {argv!r}")


def _read_pipe_json(fd: int, *, timeout: float = 90) -> dict[str, object]:
    readable, _, _ = select.select([fd], [], [], timeout)
    if fd not in readable:
        raise TimeoutError("controller result deadline exceeded")
    chunks: list[bytes] = []
    while True:
        part = os.read(fd, 1_048_576)
        if not part:
            break
        chunks.append(part)
    return json.loads(b"".join(chunks))


class _ExecutorServer:
    def __init__(self, socket_path: Path, *, controller_uid: int) -> None:
        self.socket_path = socket_path
        self.controller_uid = controller_uid
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(socket_path))
        os.chown(socket_path, 0, controller_uid)
        socket_path.chmod(0o660)
        self.listener.listen(32)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.errors: list[str] = []
        self.connections = 0
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=10)
        for worker in self._workers:
            worker.join(timeout=90)
        assert not self.thread.is_alive(), "worker executor listener did not stop"
        assert not any(worker.is_alive() for worker in self._workers), (
            "worker executor connection did not stop"
        )
        assert not self.errors, self.errors

    def _handle(self, connection: socket.socket) -> None:
        from mimir_cultivate.worklink import worker_exec

        try:
            peer = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12,
            )
            uid = int.from_bytes(peer[4:8], sys.byteorder, signed=True)
            if uid != self.controller_uid:
                raise AssertionError(
                    f"executor request came from uid {uid}, expected {self.controller_uid}"
                )
            with self._lock:
                self.connections += 1
            worker_exec.handle_connection(connection)
        except BaseException:
            with self._lock:
                self.errors.append(traceback.format_exc())
            connection.close()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if not self.stop.is_set():
                    with self._lock:
                        self.errors.append(repr(exc))
                return
            worker = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            self._workers.append(worker)
            worker.start()


@pytest.fixture
def remediation_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from importlib.metadata import PackageNotFoundError, version

    try:
        version("mimir-cultivate")
    except PackageNotFoundError:
        pytest.skip("requires installed Cultivate")
    provider_worker_client = pytest.importorskip("mimir_cultivate.worklink.worker_client")
    from mimir_cultivate.worklink import worker_client

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
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(retained_root))
    monkeypatch.setattr(
        "mimir_cultivate.worklink.retained_scope._factory_session_lock_is_fresh",
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
    case = SimpleNamespace(
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
    with policy_scope(load_plugins(enable="cultivate").policy):
        yield case


async def _trusted_turn(case):
    from mimir_cultivate.scheduler import worklink_poller_hooks

    skills = case.home / "skills"
    ready = next(
        config
        for config in discover_pollers(
            skills, state_root=case.state_root,
            policy=load_plugins(enable="cultivate").policy,
        )
        if config.name == "worklink-ready-queue"
    )
    capture = _CapturingEnqueue()
    emitted = await run_poller(
        replace(ready, command=f"{sys.executable} scripts/poller.py", hooks=worklink_poller_hooks()),
        enqueue=capture,
        home=case.home,
    )
    events_path = case.home / "logs" / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    misconfigured = [
        event
        for event in events
        if event.get("type") == "worklink_poller_misconfigured"
    ]
    assert not misconfigured, [event.get("reason") for event in misconfigured]
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
    assert auth.retained_factory_scope is not None, auth.retained_factory_scope_refusal
    return event, labels, auth


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("name", [
    "repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit",
])
async def test_selected_retained_tools_keep_one_live_lease_through_result_classification(
    remediation_case, monkeypatch, name, enforced, asynchronous,
):
    from langchain.agents.middleware import ToolCallRequest
    from langchain.tools import ToolRuntime
    from langgraph.runtime import Runtime
    from mimir.tools import budget_gate
    from mimir_cultivate import repository
    from mimir_cultivate.worklink import retained_scope, worker_client

    case = remediation_case
    _event, _labels, issued = await _trusted_turn(case)
    auth = replace(issued, enforcement_enabled=enforced)
    scope = auth.retained_factory_scope
    assert scope is not None
    selected = load_plugins(enable="cultivate").policy

    def owner_control(checkout, argv, *, env, timeout, output_limit):
        assert checkout == case.sandbox
        return subprocess.run(
            argv, env={**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True, check=False,
        )

    monkeypatch.setattr(worker_client, "run_factory_control", owner_control)
    if name == "repo_test":
        from mimir_cultivate.repository import ProjectTestResult

        async def passing_test(self, selectors=(), *, suite=None):
            assert self._retained_scope is scope
            return ProjectTestResult(True, "tests_passed", 0)

        monkeypatch.setattr(repository.RepoProjectTests, "execute", passing_test)
    if name in {"repo_stage", "repo_commit"}:
        (case.sandbox / "fix.txt").write_text("changed\n", encoding="utf-8")
    tool = selected.tools[name].factory()
    arguments = {"repository": scope.repository, "pull_request": scope.issue_id}
    if name in {"repo_stage", "repo_commit"}:
        arguments["paths"] = ("fix.txt",)
    if name == "repo_commit":
        arguments["message"] = "retained remediation"
    request = ToolCallRequest(
        tool_call={"name": name, "args": arguments, "id": "retained-call", "type": "tool_call"},
        tool=tool, state=None, runtime=Runtime(context=auth),
    )
    runtime = ToolRuntime(
        state={}, context=auth, config={}, stream_writer=lambda _: None,
        tool_call_id="retained-call", store=None,
    )
    captured = []
    authorizations = []
    original_capture = budget_gate._capture_retained_result

    def capture(call, execution, observed_scope, result):
        original_capture(call, execution, observed_scope, result)
        captured.append(execution.retained_receipt)
        authorizations.append(call.authorization)

    monkeypatch.setattr(budget_gate, "_capture_retained_result", capture)
    invoked = []

    async def execute():
        assert ac.retained_invocation_lease_active(scope, auth)
        with retained_scope.retained_factory_effect_lease(case.home, scope) as competing:
            assert competing.scope is None
        invoked.append(name)
        if name == "repo_test":
            return await tool.coroutine(**arguments, runtime=runtime)
        return tool.func(**arguments, runtime=runtime)

    async def async_handler(_request):
        return ToolMessage(content=json.dumps(await execute()), tool_call_id="retained-call")

    def sync_handler(_request):
        return ToolMessage(content=json.dumps(asyncio.run(execute())), tool_call_id="retained-call")

    gate = budget_gate.BudgetGateMiddleware(selected)
    if asynchronous:
        output = await gate.awrap_tool_call(request, async_handler)
    else:
        output = await asyncio.to_thread(gate.wrap_tool_call, request, sync_handler)
    assert output.status != "error", output.content
    assert invoked == [name]
    assert len(captured) == 1 and captured[0] is not None
    receipt = captured[0]
    assert receipt._used
    assert len(receipt.lease_identity) == 5
    assert receipt.execution_id != receipt.lease_identity[-1]
    assert not ac.retained_invocation_lease_active(scope, auth)
    with policy_scope(selected):
        replay = ac.classify_protected_result(
            name, arguments, auth, authorizations[0], result=output,
            retained_receipt=receipt,
        )
    assert replay is not None
    assert all(source.integrity == "untrusted" for source in replay.sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("mutation", [
    "wrong_resource", "forged_service", "missing_scope", "stale_lease", "missing_lease",
])
async def test_selected_retained_middleware_refuses_unissued_authority_before_handler(
    remediation_case, monkeypatch, asynchronous, enforced, mutation,
):
    from contextlib import contextmanager
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.runtime import Runtime
    from mimir.tools.budget_gate import BudgetGateMiddleware

    _event, _labels, issued = await _trusted_turn(remediation_case)
    scope = issued.retained_factory_scope
    assert scope is not None
    selected = load_plugins(enable="cultivate").policy
    auth = replace(issued, enforcement_enabled=enforced)
    arguments = {"repository": scope.repository, "pull_request": scope.issue_id}
    if mutation == "wrong_resource":
        arguments["pull_request"] += 1
    elif mutation == "forged_service":
        auth = replace(auth, service_authority=None)
    elif mutation == "missing_scope":
        auth = replace(auth, retained_factory_scope=None)
    elif mutation == "missing_lease":
        monkeypatch.setattr(selected, "_retained_effect_lease", None)
    else:
        owner, _lease, runner = selected.retained_effect_lease

        @contextmanager
        def stale_lease(_home, _scope):
            yield SimpleNamespace(scope=None, refusal_reason="retained lease changed")

        monkeypatch.setattr(selected, "_retained_effect_lease", (owner, stale_lease, runner))
    request = ToolCallRequest(
        tool_call={"name": "repo_status", "args": arguments, "id": "retained-denied", "type": "tool_call"},
        tool=selected.tools["repo_status"].factory(), state=None,
        runtime=Runtime(context=auth),
    )
    executed = []

    def handler(_request):
        executed.append(True)
        return ToolMessage(content="should not execute", tool_call_id="retained-denied")

    async def async_handler(tool_request):
        return handler(tool_request)

    gate = BudgetGateMiddleware(selected)
    output = (
        await gate.awrap_tool_call(request, async_handler)
        if asynchronous else await asyncio.to_thread(gate.wrap_tool_call, request, handler)
    )
    assert output.status == "error"
    assert not executed


@pytest.mark.asyncio
async def test_retained_remediation_reaches_effects_through_real_tool_surface(
    remediation_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scripted incident turn crosses authorization, BudgetGate, and every tool."""
    from deepagents import create_deep_agent

    from mimir._deepagents_patches import install_deepagents_grep_context_tool
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
    from mimir_cultivate.repository import ProjectTestResult
    from mimir_cultivate import repository as repo_module
    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir.tools import budget_gate
    from mimir_cultivate.registration import worklink_resume
    from mimir_cultivate.worklink import autonomy, detached_dispatch, worker_client
    provider_worker_client = pytest.importorskip("mimir_cultivate.worklink.worker_client")

    case = remediation_case
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
    monkeypatch.setattr(
        provider_worker_client.WorkerClient,
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
    install_deepagents_grep_context_tool()
    tools = [
        tool for tool in repo_module.REPO_TOOLS
        if tool.name in {"repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"}
    ] + [worklink_resume]
    receipts = []
    original_capture = budget_gate._capture_retained_result

    def capture_retained(call, capture, selected_scope, result):
        original_capture(call, capture, selected_scope, result)
        if capture.retained_receipt is not None:
            receipts.append((call.tool_name, selected_scope is scope,
                             capture.retained_receipt.lease_identity,
                             capture.retained_receipt.execution_id))

    monkeypatch.setattr(budget_gate, "_capture_retained_result", capture_retained)
    graph = create_deep_agent(
        model=_ScriptedModel(messages=iter(messages)),
        tools=tools,
        backend=backend,
        middleware=[BudgetGateMiddleware(load_plugins(enable="cultivate").policy)],
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
    for message in tool_messages:
        assert message.status != "error", f"{message.name}: {message.content}"
    assert [name for name, *_ in receipts] == [
        "repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit",
    ]
    assert all(bound is True and len(lease_id) == 5 for _, bound, lease_id, _ in receipts)
    assert len({lease_id[-1] for _, _, lease_id, _ in receipts}) == 5
    assert all(execution_id != lease_id[-1] for _, _, lease_id, execution_id in receipts)
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


@pytest.mark.asyncio
async def test_worklink_resume_shared_launcher_runs_epic_and_retires_only_target(
    remediation_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise worklink_resume -> shared launcher -> CLI -> run_epic success."""
    pytest.importorskip("mimir_cultivate.commands")
    from mimir_cultivate.registration import worklink_resume
    from mimir_cultivate.worklink import autonomy, detached_dispatch
    from mimir_cultivate.worklink.orchestrator import WorklinkRunResult, WorklinkRunner
    from mimir_cultivate.worklink.orchestrator import WorklinkRunner as CultivateWorklinkRunner
    from mimir_cultivate.worklink.detached_dispatch import FactoryRecoveryIdentity

    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")

    case = remediation_case
    _event, _labels, auth = await _trusted_turn(case)
    scope = auth.retained_factory_scope
    assert scope is not None
    sibling = record_failure(
        dispatch_failure_state_dir(case.home), issue_id=1812, attempt=7,
        exit_status=1, error="sibling incident", log_path="sibling.log",
        run_id="chainlink-1812", work_path=str(tmp_path / "sibling"),
    )
    before_sibling = json.dumps(sibling, sort_keys=True, separators=(",", ":")).encode()
    captured_argv: list[str] = []

    def popen(argv, **kwargs):
        captured_argv[:] = list(argv)
        assert kwargs["start_new_session"] is True
        child_result: list[BaseException] = []

        def run_child() -> None:
            try:
                cli_main(list(argv[3:]))
            except BaseException as exc:
                child_result.append(exc)

        child = threading.Thread(target=run_child)
        child.start()
        child.join(timeout=30)
        assert not child.is_alive()
        assert len(child_result) == 1
        assert isinstance(child_result[0], SystemExit)
        assert child_result[0].code == 0
        return SimpleNamespace(pid=4321)

    async def successful_recovery(self, issue_id: int, **kwargs):
        assert issue_id == scope.issue_id
        assert kwargs["autonomous"] is True
        assert kwargs["expected_recovery"] == FactoryRecoveryIdentity(
            scope.signature, scope.occurrence_id, scope.run_id, scope.attempt, scope.session,
        )
        assert self.chainlink_bin == "fake-chainlink"
        return WorklinkRunResult(issue_id, scope.attempt, "completed")

    monkeypatch.setenv("CHAINLINK_BIN", "fake-chainlink")
    monkeypatch.setenv("WORKLINK_RUN_BIN", f"{sys.executable} -m mimir")
    monkeypatch.setattr(
        autonomy,
        "make_claims",
        lambda home: SimpleNamespace(_active_worklink_lock_ids_for_scope=lambda **kwargs: set()),
    )
    real_launch = detached_dispatch.launch_detached_worklink
    monkeypatch.setattr(
        detached_dispatch,
        "launch_detached_worklink",
        lambda **kwargs: real_launch(**kwargs, popen=popen),
    )
    monkeypatch.setattr(WorklinkRunner, "run_epic", successful_recovery)
    monkeypatch.setattr(CultivateWorklinkRunner, "run_epic", successful_recovery)

    output = await worklink_resume.coroutine(runtime=SimpleNamespace(context=auth))

    assert "recovery dispatched" in output
    assert captured_argv[:4] == [sys.executable, "-m", "mimir", "worklink"]
    assert captured_argv[4:7] == ["run-epic", "1811", "--home"]
    expected_flags = {
        "--expected-signature": scope.signature,
        "--expected-occurrence": scope.occurrence_id,
        "--expected-run-id": scope.run_id,
        "--expected-attempt": str(scope.attempt),
        "--expected-session": scope.session,
    }
    for flag, value in expected_flags.items():
        index = captured_argv.index(flag)
        assert captured_argv[index + 1] == value
    state = load_failure_state(dispatch_failure_state_dir(case.home))["issues"]
    assert state["1811"]["active"] is False
    after_sibling = json.dumps(state["1812"], sort_keys=True, separators=(",", ":")).encode()
    assert after_sibling == before_sibling


@pytest.mark.real_worklink_identities
def test_retained_remediation_uses_real_owner_rpc_git_and_contained_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the production remediation effect paths across real Linux identities."""
    provider_worker_client = pytest.importorskip("mimir_cultivate.worklink.worker_client")
    from mimir_cultivate import contained_execution
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
    from mimir_cultivate.repository import (
        GitCommit,
        GitDiff,
        GitStage,
        GitStatus,
        RepoGitTools,
        RepoProjectTests,
        retained_factory_git_runner,
    )
    from mimir_cultivate.worklink import worker_client, worker_exec

    identities = _system_identities()
    boundary = Path("/tmp") / f"mimir-remediation-{uuid.uuid4()}"
    retained_root = boundary / "worklink"
    checkout = retained_root / "mimir" / "1811-1" / "checkout"
    sandbox = checkout / ".factory-sandboxes" / "chainlink-1811"
    home = boundary / "home"
    controller_home = boundary / "controller-home"
    controller_repo = boundary / "controller-repo"
    executor_homes = boundary / "executor-homes"
    snapshots = boundary / "repo-test-checkouts"
    uv_cache = boundary / "uv-cache"
    socket_path = boundary / "executor.sock"
    try:
        sandbox.mkdir(parents=True)
        home.mkdir()
        controller_home.mkdir()
        controller_repo.mkdir()
        executor_homes.mkdir()
        snapshots.mkdir()
        uv_cache.mkdir()
        boundary.chmod(0o755)
        retained_root.chmod(0o755)
        checkout.parent.parent.chmod(0o755)
        os.chown(checkout.parent, identities.mimir_uid, identities.worklink_gid)
        checkout.parent.chmod(0o2750)
        for path in (checkout, checkout / ".factory-sandboxes", sandbox):
            os.chown(path, identities.worklink_uid, identities.worklink_gid)
            path.chmod(0o2770)
        os.chown(home, identities.mimir_uid, identities.mimir_gid)
        os.chown(controller_home, identities.mimir_uid, identities.mimir_gid)
        os.chown(executor_homes, 0, identities.worklink_gid)
        executor_homes.chmod(0o710)
        os.chown(snapshots, 0, identities.worklink_gid)
        snapshots.chmod(0o771)
        os.chown(uv_cache, 0, identities.worklink_gid)
        uv_cache.chmod(0o555)

        _git(sandbox, "init", "-q")
        _git(sandbox, "checkout", "-q", "-b", "feature/chainlink-1811")
        _git(sandbox, "config", "user.name", "seed")
        _git(sandbox, "config", "user.email", "seed@example.invalid")
        (sandbox / ".gitignore").write_text(".factory/\n", encoding="utf-8")
        (sandbox / "fix.txt").write_text("before\n", encoding="utf-8")
        (sandbox / "verify-remediation").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            f"test \"$(id -u)\" = {identities.worklink_uid}\n"
            "test \"$(cat fix.txt)\" = after\n"
            "test \"$(cat new.txt)\" = new\n",
            encoding="utf-8",
        )
        (sandbox / "verify-remediation").chmod(0o755)
        _git(sandbox, "add", ".gitignore", "fix.txt", "verify-remediation")
        _git(sandbox, "commit", "-q", "-m", "seed")
        _chown_tree(checkout, identities.worklink_uid, identities.worklink_gid)
        checkout.chmod(0o2770)
        sandbox.chmod(0o2770)

        (home / "worklink.yaml").write_text(
            "defaults:\n"
            "  test_command: sh verify-remediation\n"
            "backends:\n"
            "  opencode:\n"
            "    bash_allowlist:\n"
            "      - git *\n"
            "      - sh verify-remediation\n",
            encoding="utf-8",
        )
        os.chown(home / "worklink.yaml", identities.mimir_uid, identities.mimir_gid)
        launcher = boundary / "lib" / "node_modules" / "feature-factory" / "bin" / "factory.js"
        adapter = boundary / "lib" / "node_modules" / "opencode-feature-factory"
        launcher.parent.mkdir(parents=True)
        adapter.mkdir(parents=True)
        launcher.write_text("", encoding="utf-8")
        for package in (launcher.parent.parent, adapter):
            (package / "package.json").write_text(
                json.dumps({"version": "0.12.1"}), encoding="utf-8",
            )
        incident = record_failure(
            dispatch_failure_state_dir(home), issue_id=1811, attempt=1,
            exit_status=1, error="retained checkout requires remediation",
            log_path="run.log", run_id="chainlink-1811", work_path=str(sandbox),
        )
        record = FactoryRunRecord(
            run_id="chainlink-1811", issue_id=1811, attempt=1,
            repository="owner/mimir", base_ref="main",
            branch="feature/chainlink-1811", launcher=str(launcher),
            sandbox=str(sandbox), session="session-1",
            handle=LaunchHandle("local_subprocess", "99999999", 1),
            status=FactoryStatus(
                run_id="chainlink-1811", valid=True, sandbox_path=str(sandbox),
                status="needs-human", lock="stale", dead_lock=True,
                lock_session="session-1",
            ),
            observed_at=None, controller_phase="parked",
        )
        save_factory_record(home, record)
        _chown_tree(home, identities.mimir_uid, identities.mimir_gid)
        monkeypatch.setenv("MIMIR_HOME", str(home))
        monkeypatch.setenv("HOME", str(controller_home))
        monkeypatch.setenv("WORKLINK_REPO", str(controller_repo))
        monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
        monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
        case = SimpleNamespace(
            home=home,
            state_root=home / "state" / "pollers",
        )

        monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(retained_root))
        monkeypatch.setattr(provider_worker_client, "DEFAULT_EXECUTOR_SOCKET", socket_path)
        monkeypatch.setattr(worker_client, "DEFAULT_EXECUTOR_SOCKET", socket_path)
        monkeypatch.setattr(worker_exec, "HOME_ROOT", executor_homes)
        monkeypatch.setattr(worker_exec, "REPO_TEST_CHECKOUT_ROOT", snapshots)
        monkeypatch.setattr(worker_exec, "REPO_TEST_UV_CACHE", uv_cache)
        from mimir_cultivate import contained_checkout as provider_checkout
        from mimir_cultivate import contained_execution as provider_execution

        monkeypatch.setattr(provider_checkout, "REPO_TEST_CHECKOUT_ROOT", snapshots)
        monkeypatch.setattr(
            "mimir_cultivate.worklink.checkout._REPO_TEST_CHECKOUT_ROOT", snapshots,
        )
        monkeypatch.setattr(
            "mimir_cultivate.worklink.retained_scope._factory_session_lock_is_fresh",
            lambda _record: False,
        )
        monkeypatch.setattr(
            "mimir_cultivate.worklink.retained_scope._factory_session_lock_is_fresh",
            lambda _record: False,
        )
        read_fd, write_fd = os.pipe()
        with _ExecutorServer(socket_path, controller_uid=identities.mimir_uid) as executor:
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.setgroups([identities.worklink_gid])
                    os.setresgid(identities.mimir_gid, identities.mimir_gid, identities.mimir_gid)
                    os.setresuid(identities.mimir_uid, identities.mimir_uid, identities.mimir_uid)
                    init_logger(
                        home / "logs" / "events.jsonl",
                        session_id="remediation-root-e2e",
                    )
                    contained_execution.WorkerClient = lambda capability: worker_client.WorkerClient(
                        capability, socket_path=socket_path,
                    )
                    provider_execution.WorkerClient = contained_execution.WorkerClient
                    # The turn's authority is derived under the selected provider
                    # policy, as the runtime does and as the remediation_case
                    # fixture does for the other tests. Without it the core policy
                    # has no retained-scope hook ("provider unavailable").
                    selected = load_plugins(enable="cultivate").policy
                    with policy_scope(selected):
                        event, labels, auth = asyncio.run(_trusted_turn(case))
                    scope = auth.retained_factory_scope
                    assert scope is not None, auth.retained_factory_scope_refusal
                    turn = TurnContext(
                        turn_id="retained-remediation-root-e2e",
                        session_id=event.channel_id,
                        trigger=event.trigger,
                        channel_id=event.channel_id,
                        started_at=0.0,
                        auth_context=auth,
                        ifc_labels=labels,
                    )
                    backend = FileToolRouter(
                        default=WriteGuardBackend(home, ["state"], guard_outside_root=True),
                        routes=build_file_tool_routes([(str(retained_root), "retained")]),
                    )
                    with policy_scope(selected):
                        token = set_current_turn(turn)
                        try:
                            written = backend.write(str(sandbox / "new.txt"), "new\n")
                            edited = backend.edit(str(sandbox / "fix.txt"), "before", "after")
                        finally:
                            reset_current_turn(token)
                    assert written.error is None, written.error
                    assert edited.error is None, edited.error
                    tools = RepoGitTools(
                        retained_scope=scope, runner=retained_factory_git_runner,
                    )
                    status = tools.execute(GitStatus()).stdout
                    diff = tools.execute(GitDiff()).stdout
                    tested = asyncio.run(RepoProjectTests(retained_scope=scope).execute())
                    staged = tools.execute(GitStage(("fix.txt", "new.txt")))
                    committed = tools.execute(
                        GitCommit(("fix.txt", "new.txt"), "retained remediation")
                    )
                    result = {
                        "status": status,
                        "diff": diff,
                        "tested": tested.ok,
                        "test_code": tested.code,
                        "staged": staged.ok,
                        "committed": committed.ok,
                    }
                except BaseException as exc:
                    result = {"error": repr(exc), "traceback": traceback.format_exc()}
                os.write(write_fd, json.dumps(result).encode())
                os.close(write_fd)
                os._exit(0 if "error" not in result else 1)
            os.close(write_fd)
            result = _read_pipe_json(read_fd)
            os.close(read_fd)
            _, wait_status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(wait_status) == 0, json.dumps(result, indent=2)
            assert "error" not in result, json.dumps(result, indent=2)
            assert executor.connections >= 8
        assert "fix.txt" in result["status"] and "new.txt" in result["status"]
        assert "+after" in result["diff"]
        assert result["tested"] is True, result
        assert result["staged"] is True and result["committed"] is True
        assert (sandbox / "new.txt").stat().st_uid == identities.worklink_uid
        assert (sandbox / "fix.txt").stat().st_uid == identities.worklink_uid
        assert all(
            candidate.stat(follow_symlinks=False).st_uid == identities.worklink_uid
            for candidate in (sandbox / ".git", *sandbox.joinpath(".git").rglob("*"))
        )
        _assert_no_controller_git_processes(identities.mimir_uid, retained_root)
        assert _git(sandbox, "status", "--short") == ""
        assert _git(sandbox, "show", "-s", "--format=%s") == "retained remediation"
        assert _git(sandbox, "show", "-s", "--format=%an <%ae>") == (
            f"{DEFAULT_USER_NAME} <{DEFAULT_USER_EMAIL}>"
        )
        assert not any(snapshots.rglob("checkout"))
    finally:
        shutil.rmtree(boundary, ignore_errors=True)


@pytest.mark.asyncio
async def test_worklink_resume_tool_time_race_refuses_without_launch_or_fresh_attempt(
    remediation_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir_cultivate.registration import worklink_resume
    from mimir_cultivate.worklink import detached_dispatch

    case = remediation_case
    _event, _labels, auth = await _trusted_turn(case)
    scope = auth.retained_factory_scope
    assert scope is not None
    parked = replace(case.record, status=replace(case.record.status, status="needs-human"))
    save_factory_record(case.home, parked)
    launch = Mock(side_effect=AssertionError("stale recovery launched"))
    monkeypatch.setattr(detached_dispatch, "launch_detached_worklink", launch)
    before = sorted((case.home / "state/worklink").rglob("*"))

    state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["issues"]["1811"]["occurrence_id"] = "replacement-occurrence"
    state.write_text(json.dumps(payload), encoding="utf-8")
    output = await worklink_resume.coroutine(
        runtime=SimpleNamespace(context=auth),
    )

    assert "incident occurrence is not current" in output
    launch.assert_not_called()
    assert sorted((case.home / "state/worklink").rglob("*")) == before


@pytest.mark.asyncio
async def test_worklink_resume_replaced_tool_time_race_refuses_without_launch_or_fresh_attempt(
    remediation_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir_cultivate.registration import worklink_resume
    from mimir_cultivate.worklink import detached_dispatch

    case = remediation_case
    _event, _labels, auth = await _trusted_turn(case)
    scope = auth.retained_factory_scope
    assert scope is not None
    save_factory_record(
        case.home,
        replace(
            case.record,
            branch="replacement",
            status=replace(case.record.status, status="needs-human"),
        ),
    )
    launch = Mock(side_effect=AssertionError("replaced recovery launched"))
    monkeypatch.setattr(detached_dispatch, "launch_detached_worklink", launch)
    before = sorted((case.home / "state/worklink").rglob("*"))

    output = await worklink_resume.coroutine(
        runtime=SimpleNamespace(context=auth),
    )

    assert "retained factory target was replaced" in output
    launch.assert_not_called()
    assert sorted((case.home / "state/worklink").rglob("*")) == before


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
    selected = load_plugins(enable="cultivate").policy
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
            policy=selected,
        )
        assert decision.allowed is False
        if name == "worklink_resume":
            assert decision.reason.startswith("ifc_label_blocked:"), (name, decision.reason)
        else:
            assert decision.reason == "write_blocked_by_untrusted_ingest"
            assert decision.refusal_detail == ac._TAINTED_WRITE_REFUSAL
    assert not (case.sandbox / "blocked.txt").exists()


@pytest.mark.real_worklink_identities
def test_leaf_checkout_lets_the_worklink_uid_run_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaf build's worker uid runs real git in its own checkout.

    Production path end to end: the controller (``mimir``, in the ``worklink``
    group) issues a selected, worker-eligible leaf checkout, then launches
    through the root executor's path-addressed RPC, which drops to the
    ``worklink`` uid. Git must be able to stat and write that tree: an FD-only
    checkout behind a closed 0700 boundary fails here with "failed to stat".
    """
    identities = _system_identities()
    from mimir_cultivate.worklink import checkout as worklink_checkout
    from mimir_cultivate.worklink import worker_client, worker_exec
    from mimir_cultivate.worklink.backends.base import WorkSpec
    from mimir_cultivate.worklink.compute import LocalSubprocessComputeBackend

    workspace = Path("/tmp") / f"mimir-leaf-git-{uuid.uuid4()}"
    repo = workspace / "leaf-repo"
    worklink_root = workspace / ".worklink"
    executor_homes = workspace / "executor-homes"
    controller_home = workspace / "controller-home"
    socket_path = workspace / "executor.sock"
    try:
        repo.mkdir(parents=True)
        controller_home.mkdir()
        executor_homes.mkdir()
        workspace.chmod(0o755)
        os.chown(executor_homes, 0, identities.worklink_gid)
        executor_homes.chmod(0o710)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
             "commit", "-q", "--allow-empty", "-m", "base")
        # Model the provisioned base clone: per-command -c identity above is
        # not persisted for the controller to lend to the attempt checkout.
        _git(repo, "config", "--local", "user.name", "leaf-test-bot")
        _git(repo, "config", "--local", "user.email", "leaf-test-bot@example.com")
        _git(repo, "remote", "add", "origin", str(repo))
        _chown_tree(repo, identities.mimir_uid, identities.mimir_gid)
        os.chown(controller_home, identities.mimir_uid, identities.mimir_gid)
        os.chown(workspace, identities.mimir_uid, identities.mimir_gid)

        monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
        monkeypatch.setenv("HOME", str(controller_home))
        monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(worklink_root))
        monkeypatch.setattr(worker_exec, "HOME_ROOT", executor_homes)
        script = "\n".join((
            "set -eu",
            f'test "$(id -u)" = {identities.worklink_uid}',
            "git status --porcelain",
            "printf 'worker change\\n' > worker-file",
            "git add worker-file",
            "export GIT_AUTHOR_NAME=worker GIT_AUTHOR_EMAIL=w@example.com",
            "export GIT_COMMITTER_NAME=worker GIT_COMMITTER_EMAIL=w@example.com",
            "git commit -q -m 'worker commit'",
            "git log -1 --format=%s",
            'test "$(git config --get-all safe.directory)" = "$(pwd)"',
            f'if git -C "{worklink_root / repo.name / "1413-1"}" status > sibling-out 2>&1; then exit 20; fi',
            'grep -q "dubious ownership" sibling-out',
            "printf 'git-euid=%s\\n' \"$(id -u)\"",
        ))

        read_fd, write_fd = os.pipe()
        with _ExecutorServer(socket_path, controller_uid=identities.mimir_uid):
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.setgroups([identities.worklink_gid])
                    os.setresgid(identities.mimir_gid, identities.mimir_gid, identities.mimir_gid)
                    os.setresuid(identities.mimir_uid, identities.mimir_uid, identities.mimir_uid)
                    lease = worklink_checkout.create_isolated_checkout(
                        repo, issue_id=1412, attempt=1, base="main", worker_eligible=True,
                    )
                    assert not lease.worker_authorized and lease.authorization is None
                    assert lease.path == worklink_root / repo.name / "1412-1"
                    sibling = worklink_root / repo.name / "1413-1"
                    sibling.mkdir(mode=0o2770)
                    os.chown(sibling, identities.mimir_uid, identities.worklink_gid)
                    _git(sibling, "init", "-q")
                    compute = LocalSubprocessComputeBackend.for_path_checkout(
                        identities.worklink_uid,
                        worker_client=worker_client.WorkerClient.for_path_checkout(
                            lease.path, issue_id=1412, attempt=1,
                            run_uid=identities.worklink_uid, socket_path=socket_path,
                        ),
                    )
                    argv = ("/bin/sh", "-c", script)
                    spec = WorkSpec(
                        issue_id=1412, attempt=1, repo_url=str(repo), base_ref="main",
                        branch=lease.branch, prompt="", rules=None, test_command="",
                        backend="opencode", timeout_s=60, local_checkout=lease.path,
                        local_argv=argv,
                    )

                    async def build():
                        handle = await compute._launch_contained(spec, argv)
                        try:
                            return await compute.wait(handle, 60)
                        finally:
                            await compute.cleanup(handle)

                    result = asyncio.run(build())
                    committed = subprocess.run(
                        ["git", "-c", f"safe.directory={lease.path}", "-C", str(lease.path),
                         "log", "-1", "--format=%s"],
                        capture_output=True, text=True, check=True,
                    ).stdout.strip()
                    owner = (lease.path / "worker-file").stat().st_uid
                    payload = {"exit": result.exit_code, "stdout": result.stdout,
                               "stderr": result.stderr, "committed": committed, "owner": owner}
                except BaseException as exc:
                    payload = {"error": repr(exc), "traceback": traceback.format_exc()}
                os.write(write_fd, json.dumps(payload).encode())
                os._exit(0)
            os.close(write_fd)
            chunks = []
            while chunk := os.read(read_fd, 65536):
                chunks.append(chunk)
            os.close(read_fd)
            _, status = os.waitpid(pid, 0)
        payload = json.loads(b"".join(chunks))
        assert os.waitstatus_to_exitcode(status) == 0, payload
        assert "error" not in payload, payload
        assert payload["exit"] == 0, payload
        assert "worker commit" in payload["stdout"], payload
        assert f"git-euid={identities.worklink_uid}" in payload["stdout"], payload
        assert payload["committed"] == "worker commit"
        assert payload["owner"] == identities.worklink_uid != identities.mimir_uid
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
