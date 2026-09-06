from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from pydantic import PrivateAttr

from mimir.access_control import (
    CapabilityTier,
    OperationCatalog,
    OperationDecision,
    ServicePrincipal,
    ToolRegistry,
    build_trigger_service_principal,
    builtin_trigger_service_principal,
)
from mimir.models import AuthContext
from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir.tools.service_tool_surface import ServiceToolSurfaceMiddleware


def auth(service: ServicePrincipal | None = None, **changes: Any) -> AuthContext:
    values = dict(
        principal=service.canonical if service else "user:admin",
        canonical_principal=service.canonical if service else "user:admin",
        roles=("service",) if service else ("admin",), event_ingress=None,
        trigger=service.trigger if service else "user_message", channel_id="test",
        interactivity=None, is_service=service is not None,
        service_authority=service, enforcement_enabled=False,
    )
    return AuthContext(**(values | changes))


def service_context(kind: str) -> AuthContext:
    capabilities = ("read_file", "memory_store")
    if kind == "code_execution":
        capabilities += (
            "shell_exec", "bash_async", "bash_jobs_list", "bash_job_output",
            "write_file", "edit_file", "send_message", "spawn_open_code", "worklink_run",
        )
    return auth(build_trigger_service_principal(
        canonical=f"poller:{kind}", trigger="poller",
        profile="research" if kind == "research" else "custom",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE if kind == "research"
        else CapabilityTier.CODE_EXECUTION,
        capabilities=capabilities, creation_path="test",
    ))


def tools(*names: str) -> list[StructuredTool]:
    return [StructuredTool.from_function(
        lambda: "executed", name=name, description=f"Test {name}",
    ) for name in names]


INVENTORY = (
    "web_search", "fetch_url", "memory_query", "memory_get", "write_todos",
    "read_file", "memory_store", "shell_exec", "bash_async", "bash_jobs_list",
    "bash_job_output", "write_file", "edit_file", "send_message", "saga_feedback",
    "saga_mark_contributions", "saga_end_session", "saga_record_skill_learning",
    "saga_forget", "rebuild_index", "spawn_open_code", "worklink_run",
    "hands_read", "hands_edit", "hands_shell", "hands_python", "novel_tool",
)
OPEN = list(INVENTORY[:5])
CODE_EXECUTION = OPEN + [
    "read_file", "memory_store", "shell_exec", "bash_async", "bash_jobs_list",
    "bash_job_output", "write_file", "edit_file", "send_message", "spawn_open_code",
    "worklink_run",
]


class RecordingModel(BaseChatModel):
    """Exercise real binding and ToolNode dispatch, without a provider."""

    call_name: str = "bash_exec"
    _bound: list[str] = PrivateAttr(default_factory=list)
    _seen: list[tuple[list[str], list[Any]]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "service-surface-test"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> RecordingModel:
        self._bound = [tool.name for tool in tools]
        return self

    def _generate(self, messages: list[Any], **kwargs: Any) -> ChatResult:
        self._seen.append((list(self._bound), list(messages)))
        response = AIMessage(content="finished") if any(
            isinstance(message, ToolMessage) for message in messages
        ) else AIMessage(content="", tool_calls=[{
            "name": self.call_name, "args": {}, "id": "hallucinated",
            "type": "tool_call",
        }])
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages: list[Any], **kwargs: Any) -> ChatResult:
        return self._generate(messages, **kwargs)


@pytest.mark.parametrize("kind", ["research", "code_execution", "admin"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_graph_surface_and_unknown_tool_listing(kind, asynchronous):
    model = RecordingModel()
    graph = create_agent(
        model, tools=tools(*INVENTORY), system_prompt="Original instructions.",
        middleware=[ServiceToolSurfaceMiddleware()], context_schema=AuthContext,
    )
    context = auth() if kind == "admin" else service_context(kind)
    inputs = {"messages": [{"role": "user", "content": "Do the task"}]}
    result = (await graph.ainvoke(inputs, context=context) if asynchronous
              else graph.invoke(inputs, context=context))
    expected = list(INVENTORY) if kind == "admin" else OPEN + ["read_file", "memory_store"]
    if kind == "code_execution":
        expected = CODE_EXECUTION
    assert len(model._seen) == 2
    for bound, messages in model._seen:
        assert bound == expected
        prompt = messages[0].text
        assert prompt.startswith("Original instructions.")
        if kind == "admin":
            assert prompt == "Original instructions."
        else:
            assert f"Available tools: {', '.join(expected)}." in prompt
            unavailable = (
                "bash_exec, execute." if kind == "code_execution" else
                "shell_exec, bash_exec, bash_async, bash_jobs_list, bash_job_output, "
                "execute, write_file, edit_file, send_message."
            )
            assert f"Unavailable common shell/background/file-write/reply tools: {unavailable}" in prompt
            # Providers may flatten text blocks with no inserted separator.
            flattened = "".join(block["text"] for block in messages[0].content)
            assert flattened.startswith(
                "Original instructions.\n\nService tool availability for this turn:\n"
            )
    error = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert error.status == "error"
    assert error.tool_call_id == "hallucinated"
    if kind == "admin":
        assert "hands_shell" in error.content
    else:
        assert error.content == (
            "Error: bash_exec is not a valid tool. Available tools: "
            + ", ".join(expected) + "."
        )


@pytest.mark.usefixtures("middleware_event_logger")
@pytest.mark.parametrize("kind", ["research", "code_execution"])
@pytest.mark.parametrize("enforce", [False, True], ids=["shadow", "enforced"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_graph_surface_before_gate_preserves_invalid_name_listing(
    monkeypatch, kind, enforce, asynchronous,
):
    registry = ToolRegistry()
    monkeypatch.setattr("mimir.tools.budget_gate.get_tool_registry", lambda: registry)
    model = RecordingModel()
    graph = create_agent(
        model, tools=tools(*INVENTORY), system_prompt="Original instructions.",
        middleware=[ServiceToolSurfaceMiddleware(), BudgetGateMiddleware()],
        context_schema=AuthContext,
    )
    context = replace(service_context(kind), enforcement_enabled=enforce)
    inputs = {"messages": [{"role": "user", "content": "Do the task"}]}
    result = (await graph.ainvoke(inputs, context=context) if asynchronous
              else graph.invoke(inputs, context=context))
    expected = CODE_EXECUTION if kind == "code_execution" else OPEN + ["read_file", "memory_store"]
    assert len(model._seen) == 2
    for bound, messages in model._seen:
        assert bound == expected
        assert f"Available tools: {', '.join(bound)}." in messages[0].text
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(errors) == 1
    assert errors[0].name == "bash_exec"
    assert errors[0].tool_call_id == "hallucinated"
    assert errors[0].status == "error"
    # If BudgetGate is outermost, enforcement refuses this unregistered tool
    # before the surface middleware can supply the filtered recovery list.
    assert errors[0].content == (
        "Error: bash_exec is not a valid tool. Available tools: "
        + ", ".join(expected) + "."
    )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_per_request_filtering_preserves_requests_and_prompt_blocks(asynchronous):
    middleware = ServiceToolSurfaceMiddleware()
    system = SystemMessage(content=[{"type": "text", "text": "cached instructions"}],
                           additional_kwargs={"keep": "metadata"})
    inventory = tools(*INVENTORY)
    contexts = [service_context("research"), auth(), service_context("code_execution")]

    async def run(context):
        request = ModelRequest(model=RecordingModel(), messages=[], tools=inventory,
                               system_message=system, runtime=Runtime(context=context))
        captured = []

        def handler(filtered):
            captured.append(filtered)
            return ModelResponse(result=[AIMessage(content="ok")])

        async def ahandler(filtered):
            await asyncio.sleep(0)
            return handler(filtered)

        response = (await middleware.awrap_model_call(request, ahandler) if asynchronous
                    else middleware.wrap_model_call(request, handler))
        filtered = captured[0]
        assert request.tools is inventory and request.system_message is system
        assert len(system.content) == 1
        if not context.is_service:
            assert filtered is request
            assert isinstance(response, ModelResponse)
        else:
            assert filtered.system_message.content[0] == system.content[0]
            assert filtered.system_message.additional_kwargs == system.additional_kwargs
            assert response.command.update["service_tool_surface_names"] == [t.name for t in filtered.tools]
        return [t.name for t in filtered.tools]

    results = await asyncio.gather(*(run(context) for context in contexts))
    assert results == [OPEN + ["read_file", "memory_store"], list(INVENTORY),
                       CODE_EXECUTION]


@pytest.mark.parametrize("context", [
    auth(is_service=True),
    auth(ServicePrincipal(canonical="forged", trigger="poller"), event_ingress="http"),
    SimpleNamespace(is_service=True, roles=("admin",)),
    {"is_service": True, "roles": ["admin"]},
])
def test_untrusted_service_fails_closed(context):
    request = ModelRequest(model=RecordingModel(), messages=[], tools=tools(*INVENTORY),
                           runtime=Runtime(context=context))
    captured = []
    ServiceToolSurfaceMiddleware().wrap_model_call(
        request, lambda filtered: captured.append(filtered) or ModelResponse(result=[]),
    )
    assert captured[0].tools == []
    assert "Available tools: (none)." in captured[0].system_message.text


@pytest.mark.parametrize("enforce", [False, True])
def test_synthesis_uses_catalog_and_exact_resource_requirements(tmp_path, monkeypatch, enforce):
    service = builtin_trigger_service_principal("session-boundary", tmp_path)
    catalog = OperationCatalog()
    catalog.register_operation("explicit_open", OperationDecision.OPEN)
    monkeypatch.setattr("mimir.tools.service_tool_surface.get_operation_catalog", lambda: catalog)
    candidates = [*INVENTORY, "explicit_open", "explicit_grant"]
    service = replace(service, capabilities=(*service.capabilities, "explicit_grant"))

    def surface(principal):
        request = ModelRequest(
            model=RecordingModel(), messages=[], tools=tools(*candidates),
            runtime=Runtime(context=auth(principal, enforcement_enabled=enforce)),
        )
        return [t.name for t in ServiceToolSurfaceMiddleware()._filter_request(request).tools]

    visible = surface(service)
    assert all(name in visible for name in (
        *OPEN, "read_file", "memory_store", "shell_exec", "bash_jobs_list",
        "bash_job_output", "write_file", "edit_file", "rebuild_index",
        "saga_feedback", "saga_mark_contributions", "saga_end_session",
        "saga_record_skill_learning", "explicit_open", "explicit_grant",
    ))
    assert all(name not in visible for name in ("bash_async", "send_message", "saga_forget", "hands_shell"))
    stripped = surface(replace(service, readable_domains=(), sink_destinations=()))
    assert stripped == [*OPEN, "explicit_open", "explicit_grant"]
    # OPEN memory reads do not require an explicit service grant. Non-open
    # resource operations require both the grant and declared domain/sink.
    granted = replace(service, capabilities=("send_message",), sink_destinations=("message",))
    assert "send_message" in surface(granted)
    assert "send_message" not in surface(replace(granted, sink_destinations=()))


def test_dictionary_tool_schemas_and_empty_synthesis_prompt():
    inventory = [
        {"type": "function", "function": {"name": "web_search"}},
        {"name": "memory_query"}, {"type": "provider_specific"},
        {"name": "write_file"},
    ]
    request = ModelRequest(model=RecordingModel(), messages=[], tools=inventory,
                           runtime=Runtime(context=service_context("research")))
    filtered = ServiceToolSurfaceMiddleware()._filter_request(request)
    assert filtered.tools == inventory[:2]
    assert "Available tools: web_search, memory_query." in filtered.system_message.text


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_same_graph_recomputes_surface_between_invocations(asynchronous):
    model = RecordingModel()
    graph = create_agent(model, tools=tools(*INVENTORY),
                         middleware=[ServiceToolSurfaceMiddleware()])
    contexts = [auth(), service_context("research"), service_context("code_execution")]
    for context in contexts:
        inputs = {"messages": [{"role": "user", "content": "run"}]}
        if asynchronous:
            await graph.ainvoke(inputs, context=context)
        else:
            graph.invoke(inputs, context=context)
    assert [names for names, _ in model._seen[::2]] == [
        list(INVENTORY), OPEN + ["read_file", "memory_store"],
        CODE_EXECUTION,
    ]


@pytest.mark.parametrize("kind", ["research", "code_execution"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_deepagents_builtin_write_visibility(kind, asynchronous):
    from deepagents import create_deep_agent
    from deepagents.backends import StateBackend

    model = RecordingModel()
    graph = create_deep_agent(
        model=model, tools=[], backend=StateBackend(),
        middleware=[ServiceToolSurfaceMiddleware()], context_schema=AuthContext,
    )
    inputs = {"messages": [{"role": "user", "content": "Do the task"}]}
    context = service_context(kind)
    result = (await graph.ainvoke(inputs, context=context) if asynchronous
              else graph.invoke(inputs, context=context))
    assert len(model._seen) == 2
    for bound, messages in model._seen:
        assert "read_file" in bound
        assert ("write_file" in bound) == (kind == "code_execution")
        assert ("edit_file" in bound) == (kind == "code_execution")
        assert f"Available tools: {', '.join(bound)}." in messages[0].text
    error = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert error.content == (
        "Error: bash_exec is not a valid tool. Available tools: "
        + ", ".join(model._seen[0][0]) + "."
    )


def test_current_request_inventory_is_not_widened():
    middleware = ServiceToolSurfaceMiddleware()
    context = service_context("code_execution")
    for inventory in (tools("web_search", "spawn_open_code"), tools("memory_get"), []):
        request = ModelRequest(model=RecordingModel(), messages=[], tools=inventory,
                               runtime=Runtime(context=context))
        filtered = middleware._filter_request(request)
        assert filtered.tools == inventory
        expected = ", ".join(tool.name for tool in inventory) or "(none)"
        assert f"Available tools: {expected}." in filtered.system_message.text


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_registered_hidden_tool_still_reaches_handler(asynchronous):
    middleware = ServiceToolSurfaceMiddleware()
    tool = tools("memory_store")[0]
    request = ToolCallRequest(
        tool_call={"name": tool.name, "args": {}, "id": "direct", "type": "tool_call"},
        tool=tool, state={"service_tool_surface_names": []},
        runtime=Runtime(context=service_context("research")),
    )
    called = []

    def handler(actual):
        called.append(actual)
        return ToolMessage(content="gate response", tool_call_id="direct")

    async def ahandler(actual):
        return handler(actual)

    response = (await middleware.awrap_tool_call(request, ahandler) if asynchronous
                else middleware.wrap_tool_call(request, handler))
    assert called == [request]
    assert response.content == "gate response"


@pytest.mark.usefixtures("middleware_event_logger")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True], ids=["shadow", "enforced"])
async def test_real_budget_gate_shadow_decision_and_enforced_hidden_tool_denial(monkeypatch, asynchronous, enforce):
    # Capture real policy decisions without replacing authorization. Visibility
    # does not alter shadow compatibility; enforcement must stop direct calls.
    registry = ToolRegistry()
    monkeypatch.setattr("mimir.tools.budget_gate.get_tool_registry", lambda: registry)
    decisions = []
    authorize_tool = registry.authorize_tool

    def capture_authorization(*args, **kwargs):
        decision = authorize_tool(*args, **kwargs)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(registry, "authorize_tool", capture_authorization)
    context = auth(ServicePrincipal(canonical="poller:limited", trigger="poller"),
                   enforcement_enabled=enforce)
    tool = tools("memory_store")[0]
    surface = ServiceToolSurfaceMiddleware()
    model_request = ModelRequest(model=RecordingModel(), messages=[], tools=[tool],
                                 runtime=Runtime(context=context))
    assert surface._filter_request(model_request).tools == []
    request = ToolCallRequest(
        tool_call={"name": tool.name, "args": {}, "id": "direct", "type": "tool_call"},
        tool=tool, state={"service_tool_surface_names": []}, runtime=Runtime(context=context),
    )
    gate = BudgetGateMiddleware()
    executed = []

    def execute(actual):
        executed.append(actual)
        return ToolMessage(content="executed", tool_call_id="direct", name=tool.name)

    async def aexecute(actual):
        return execute(actual)

    async def agate(actual):
        return await gate.awrap_tool_call(actual, aexecute)

    result = (await surface.awrap_tool_call(request, agate) if asynchronous else
              surface.wrap_tool_call(request, lambda actual: gate.wrap_tool_call(actual, execute)))
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.tool_name == "memory_store"
    assert decision.decision == OperationDecision.ADMIN_REQUIRED
    assert decision.reason == "admin_required"
    assert decision.would_block is True
    assert decision.allowed is (not enforce)
    assert decision.is_shadow_decision is (not enforce)
    assert decision.enforcement_enabled is enforce
    if enforce:
        assert executed == []
        assert result.status == "error"
    else:
        assert len(executed) == 1
        assert executed[0].tool_call["name"] == "memory_store"
        assert result.status == "success"
        assert result.content == "executed"
    assert result.tool_call_id == "direct"
