"""Per-request service tool visibility, not an execution authorization boundary.

Install after service tool providers but before BudgetGateMiddleware so unknown
names get the bound inventory, while registered calls still reach the gate.
The gate's own model-surface filter is ACP-only, not a service-turn modifier.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import NotRequired

from mimir.access_control import (
    OperationDecision,
    get_operation_catalog,
    get_trusted_service_from_auth_context,
    service_can_invoke_operation,
)
from mimir.models import AuthContext


class ServiceToolSurfaceState(AgentState):
    service_tool_surface_names: NotRequired[list[str]]


def _is_service(context: Any) -> bool:
    # A malformed service carrier must not take the interactive passthrough.
    return bool(
        context.get("is_service", False) if isinstance(context, dict)
        else getattr(context, "is_service", False)
    )


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name", "")
    return getattr(tool, "name", "")


class ServiceToolSurfaceMiddleware(AgentMiddleware):
    """Hide unavailable service operations in shadow and enforcement alike."""

    state_schema = ServiceToolSurfaceState

    def _filter_request(self, request: ModelRequest) -> ModelRequest:
        context = getattr(request.runtime, "context", None)
        if not _is_service(context):
            return request
        service = (
            get_trusted_service_from_auth_context(context)
            if isinstance(context, AuthContext) else None
        )
        catalog = get_operation_catalog()
        tools = [
            tool for tool in request.tools
            if service is not None and _tool_name(tool) and (
                catalog.get_decision(_tool_name(tool), context) == OperationDecision.OPEN
                or service_can_invoke_operation(service, _tool_name(tool))
            )
        ]
        names = [_tool_name(tool) for tool in tools]
        common = (
            "shell_exec", "bash_exec", "bash_async", "bash_jobs_list",
            "bash_job_output", "execute", "write_file", "edit_file", "send_message",
        )
        unavailable = [name for name in common if name not in names]
        note = (
            "Service tool availability for this turn:\n"
            f"Available tools: {', '.join(names) or '(none)'}.\n"
            "Unavailable common shell/background/file-write/reply tools: "
            f"{', '.join(unavailable) or '(none)'}.\n"
            "Use only available tool names; do not invent aliases or retry unavailable "
            "tools. Availability is not permission for arbitrary arguments: resource, "
            "destination, command and information-flow checks still apply."
        )
        original = request.system_message
        if original is None:
            system = SystemMessage(content=note)
        else:
            content = original.content
            blocks = ([{"type": "text", "text": content}] if isinstance(content, str)
                      else list(content))
            system = original.model_copy(update={
                "content": [*blocks, {"type": "text", "text": note}],
            })
        return request.override(tools=tools, system_message=system)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        filtered = self._filter_request(request)
        response = handler(filtered)
        if filtered is request:
            return response
        return ExtendedModelResponse(response, Command(update={
            "service_tool_surface_names": [_tool_name(tool) for tool in filtered.tools],
        }))

    async def awrap_model_call(
        self, request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        filtered = self._filter_request(request)
        response = await handler(filtered)
        if filtered is request:
            return response
        return ExtendedModelResponse(response, Command(update={
            "service_tool_surface_names": [_tool_name(tool) for tool in filtered.tools],
        }))

    def _invalid_tool(self, request: ToolCallRequest) -> ToolMessage | None:
        if request.tool is not None or not _is_service(
            getattr(request.runtime, "context", None),
        ):
            return None
        # This is an observational snapshot, never authority. No global inventory
        # or instance cache: concurrent invocations have independent graph state.
        names = (request.state or {}).get("service_tool_surface_names", [])
        return ToolMessage(
            content=(f"Error: {request.tool_call['name']} is not a valid tool. "
                     f"Available tools: {', '.join(names) or '(none)'}."),
            name=request.tool_call["name"], tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        invalid = self._invalid_tool(request)
        return invalid if invalid is not None else handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        invalid = self._invalid_tool(request)
        return invalid if invalid is not None else await handler(request)
