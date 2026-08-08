from __future__ import annotations

import asyncio

from acp import PROTOCOL_VERSION, RequestError, run_agent
from acp.interfaces import Agent, Client
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanUpdate,
    AudioContentBlock,
    AuthenticateRequest,
    AuthenticateResponse,
    AuthMethodAgent,
    CancelNotification,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionRequest,
    NewSessionResponse,
    PromptCapabilities,
    PromptRequest,
    PlanEntry,
    PromptResponse,
    ResourceContentBlock,
    SessionNotification,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)


if PROTOCOL_VERSION != 1:
    raise ImportError(f"unsupported ACP protocol version: {PROTOCOL_VERSION}")


AUTH_METHOD_ID = "mimir-web-key"


def auth_required_error() -> RequestError:
    return RequestError.auth_required({"methodId": AUTH_METHOD_ID})


def method_not_found_error(method: str) -> RequestError:
    return RequestError.method_not_found(method)


def invalid_params_error() -> RequestError:
    return RequestError.invalid_params()


def internal_error() -> RequestError:
    return RequestError.internal_error()


async def run_stdio_agent(
    agent: Agent,
    *,
    request_reader: asyncio.StreamReader,
    response_writer: asyncio.StreamWriter,
) -> None:
    await run_agent(
        agent,
        input_stream=response_writer,
        output_stream=request_reader,
        use_unstable_protocol=False,
    )


__all__ = [
    "AUTH_METHOD_ID",
    "Agent",
    "AgentCapabilities",
    "AgentMessageChunk",
    "AgentPlanUpdate",
    "AudioContentBlock",
    "AuthenticateRequest",
    "AuthenticateResponse",
    "AuthMethodAgent",
    "CancelNotification",
    "Client",
    "ClientCapabilities",
    "EmbeddedResourceContentBlock",
    "ImageContentBlock",
    "Implementation",
    "InitializeRequest",
    "InitializeResponse",
    "LoadSessionRequest",
    "LoadSessionResponse",
    "McpCapabilities",
    "NewSessionRequest",
    "NewSessionResponse",
    "PROTOCOL_VERSION",
    "PromptCapabilities",
    "PromptRequest",
    "PlanEntry",
    "PromptResponse",
    "RequestError",
    "ResourceContentBlock",
    "SessionNotification",
    "TextContentBlock",
    "ToolCallProgress",
    "ToolCallStart",
    "UserMessageChunk",
    "auth_required_error",
    "internal_error",
    "invalid_params_error",
    "method_not_found_error",
    "run_stdio_agent",
]
