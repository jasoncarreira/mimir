from __future__ import annotations

import json

from deepagents.middleware.subagents import _build_task_tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain.tools import ToolRuntime
import pytest

from mimir._deepagents_subagent_auth import (
    _AuthContextRunnable,
    _create_auth_context_runnable,
    _wrap_task_tool,
    install_subagent_auth_context_patch,
)
from mimir.access_control import builtin_trigger_service_principal, create_auth_context
from mimir.models import AgentEvent, AuthContext, InformationFlowLabels
from mimir.subagents import build_mimir_subagents
from mimir.tools.budget_gate import _authorize_tool_call


class _AuthorizingSubagent(Runnable):
    def __init__(self) -> None:
        self.contexts: list[AuthContext | None] = []

    def _result(self, context: AuthContext | None) -> dict:
        self.contexts.append(context)
        authorization, denial = _authorize_tool_call("add_schedule", context)
        return {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {"allowed": authorization.allowed, "denial": denial}
                    )
                )
            ]
        }

    def invoke(self, input, config=None, *, context=None, **kwargs):
        return self._result(context)

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        return self._result(context)


def _auth_context(*, roles: tuple[str, ...], enforce: bool) -> AuthContext:
    return AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=roles,
        event_ingress="bridge",
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=enforce,
        ifc_labels=InformationFlowLabels(),
    )


def _auth_task_tool(runnable: Runnable):
    return _wrap_task_tool(
        _build_task_tool(
            [
                {
                    "name": "general-purpose",
                    "description": "authorization test child",
                    "runnable": _AuthContextRunnable(runnable),
                }
            ]
        )
    )


def _task_runtime(context) -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [HumanMessage(content="parent prompt")]},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-auth",
        store=None,
    )


def test_declarative_subagent_graph_uses_auth_context_schema() -> None:
    sentinel = RunnableLambda(lambda state: state)
    seen_kwargs = {}

    def create_agent(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return sentinel

    wrapped = _create_auth_context_runnable(create_agent, "model", tools=[])

    assert isinstance(wrapped, _AuthContextRunnable)
    assert seen_kwargs["context_schema"] is AuthContext


def test_build_mimir_subagents_registers_explicit_general_purpose() -> None:
    specs = build_mimir_subagents()

    assert [spec["name"] for spec in specs] == ["general-purpose"]


def test_every_subagent_runs_tool_calls_through_budget_gate() -> None:
    specs = build_mimir_subagents()

    assert all(
        any(
            middleware.__class__.__name__ == "BudgetGateMiddleware"
            for middleware in spec["middleware"]
        )
        for spec in specs
    )


def test_task_subagent_authorizes_with_exact_parent_admin_carrier() -> None:
    child = _AuthorizingSubagent()
    tool = _auth_task_tool(child)
    parent_auth = _auth_context(roles=("admin",), enforce=True)

    result = tool.func(
        description="run an admin operation",
        subagent_type="general-purpose",
        runtime=_task_runtime(parent_auth),
    )

    payload = json.loads(result.update["messages"][0].content)
    assert child.contexts == [parent_auth]
    assert child.contexts[0] is parent_auth
    assert payload == {"allowed": True, "denial": None}


def test_heartbeat_task_subagent_cannot_exceed_parent_capabilities(tmp_path) -> None:
    authority = builtin_trigger_service_principal("heartbeat", tmp_path)
    parent_auth = create_auth_context(
        AgentEvent(
            trigger="scheduled_tick",
            channel_id="heartbeat:test",
            service_principal="heartbeat",
            service_authority=authority,
        ),
        enforce=True,
        ifc_labels=InformationFlowLabels(),
    )
    child = _AuthorizingSubagent()

    result = _auth_task_tool(child).func(
        description="attempt an authority-widening schedule mutation",
        subagent_type="general-purpose",
        runtime=_task_runtime(parent_auth),
    )

    payload = json.loads(result.update["messages"][0].content)
    assert child.contexts == [parent_auth]
    assert child.contexts[0] is parent_auth
    assert payload["allowed"] is False
    assert "requires an admin identity" in payload["denial"]


@pytest.mark.asyncio
async def test_atask_subagent_denies_non_admin_parent_under_enforcement() -> None:
    child = _AuthorizingSubagent()
    tool = _auth_task_tool(child)
    parent_auth = _auth_context(roles=("user",), enforce=True)

    result = await tool.coroutine(
        description="run an admin operation",
        subagent_type="general-purpose",
        runtime=_task_runtime(parent_auth),
    )

    payload = json.loads(result.update["messages"][0].content)
    assert child.contexts[0] is parent_auth
    assert payload["allowed"] is False
    assert "requires an admin identity" in payload["denial"]


def test_task_subagent_uses_frozen_parent_enforcement_not_live_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _AuthorizingSubagent()
    tool = _auth_task_tool(child)
    parent_auth = _auth_context(roles=("user",), enforce=False)
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")

    result = tool.func(
        description="run an admin operation",
        subagent_type="general-purpose",
        runtime=_task_runtime(parent_auth),
    )

    payload = json.loads(result.update["messages"][0].content)
    assert child.contexts[0] is parent_auth
    assert payload == {"allowed": True, "denial": None}


def test_task_subagent_rejects_model_supplied_auth_context_lookalike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForgedContext:
        roles = ("admin",)
        enforcement_enabled = False

    child = _AuthorizingSubagent()
    tool = _auth_task_tool(child)
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")

    result = tool.func(
        description="run an admin operation",
        subagent_type="general-purpose",
        runtime=_task_runtime(ForgedContext()),
    )

    payload = json.loads(result.update["messages"][0].content)
    assert child.contexts == [None]
    assert payload["allowed"] is False
    assert "missing_auth_context" in payload["denial"]


@pytest.mark.parametrize(
    ("roles", "expected_allowed"),
    [(('admin',), True), (('user',), False)],
)
@pytest.mark.asyncio
async def test_real_task_subagent_gate_uses_propagated_parent_carrier(
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
    expected_allowed: bool,
) -> None:
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    from mimir.tools import budget_gate
    from mimir.tools.budget_gate import BudgetGateMiddleware

    class _ToolCallingFakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

    executions: list[str] = []

    @tool
    def add_schedule(name: str) -> str:
        """Record a scheduler operation without touching production state."""
        executions.append(name)
        return "executed"

    parent_auth = _auth_context(roles=roles, enforce=True)
    decisions = []
    child_invocations: list[str] = []
    original_authorize = budget_gate._authorize_tool_call
    original_invoke = _AuthContextRunnable.invoke
    original_ainvoke = _AuthContextRunnable.ainvoke

    def capture_authorization(tool_name, *args, **kwargs):
        authorization = original_authorize(tool_name, *args, **kwargs)
        if tool_name == "add_schedule":
            decisions.append((authorization[0], args[0] if args else None))
        return authorization

    def capture_invoke(self, *args, **kwargs):
        child_invocations.append("sync")
        return original_invoke(self, *args, **kwargs)

    async def capture_ainvoke(self, *args, **kwargs):
        child_invocations.append("async")
        return await original_ainvoke(self, *args, **kwargs)

    monkeypatch.setattr(budget_gate, "_authorize_tool_call", capture_authorization)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(_AuthContextRunnable, "invoke", capture_invoke)
    monkeypatch.setattr(_AuthContextRunnable, "ainvoke", capture_ainvoke)
    model = _ToolCallingFakeModel(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": "task",
            "args": {
                "description": "add the nightly schedule",
                "subagent_type": "general-purpose",
            },
            "id": "tc-task", "type": "tool_call",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "add_schedule",
            "args": {"name": "nightly"},
            "id": "tc-add-schedule", "type": "tool_call",
        }]),
        AIMessage(content="child done"),
        AIMessage(content="parent done"),
    ]))

    install_subagent_auth_context_patch()
    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt="parent test",
        subagents=[{
            "name": "general-purpose",
            "description": "authorization test child",
            "system_prompt": "Call add_schedule once.",
            "tools": [add_schedule],
            "middleware": [BudgetGateMiddleware()],
        }],
        context_schema=AuthContext,
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="delegate schedule creation")]},
        context=parent_auth,
    )

    assert len(decisions) == 1
    assert child_invocations
    decision, observed_context = decisions[0]
    assert observed_context is parent_auth
    assert decision.allowed is expected_allowed
    assert executions == (["nightly"] if expected_allowed else [])
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "tc-task"
        for message in result["messages"]
    )
