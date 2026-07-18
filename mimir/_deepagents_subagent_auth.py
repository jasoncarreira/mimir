"""Carry Mimir's run-scoped authorization context into DeepAgents children."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

from langchain_core.runnables import Runnable, RunnableConfig

from .models import AuthContext


_subagent_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "mimir_subagent_auth_context", default=None
)
_PATCH_MARKER = "_mimir_auth_context_patched"


class _AuthContextRunnable(Runnable):
    """Invoke a child graph with the exact carrier captured by its task call."""

    def __init__(self, runnable: Runnable) -> None:
        self._runnable = runnable

    def invoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._runnable.invoke(
            input,
            config,
            context=_subagent_auth_context.get(),
            **kwargs,
        )

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._runnable.ainvoke(
            input,
            config,
            context=_subagent_auth_context.get(),
            **kwargs,
        )


def _wrap_task_tool(task_tool: Any) -> Any:
    """Capture the validated parent carrier around task/atask execution."""
    original_func = task_tool.func
    original_coroutine = task_tool.coroutine

    @wraps(original_func)
    def task(*task_args: Any, **task_kwargs: Any) -> Any:
        runtime = task_kwargs.get("runtime")
        carrier = getattr(runtime, "context", None)
        token = _subagent_auth_context.set(
            carrier if isinstance(carrier, AuthContext) else None
        )
        try:
            return original_func(*task_args, **task_kwargs)
        finally:
            _subagent_auth_context.reset(token)

    @wraps(original_coroutine)
    async def atask(*task_args: Any, **task_kwargs: Any) -> Any:
        runtime = task_kwargs.get("runtime")
        carrier = getattr(runtime, "context", None)
        token = _subagent_auth_context.set(
            carrier if isinstance(carrier, AuthContext) else None
        )
        try:
            return await original_coroutine(*task_args, **task_kwargs)
        finally:
            _subagent_auth_context.reset(token)

    task_tool.func = task
    task_tool.coroutine = atask
    return task_tool


def _create_auth_context_runnable(
    create_agent: Any, *args: Any, **kwargs: Any
) -> _AuthContextRunnable:
    """Build a child graph with the same immutable context schema as its parent."""
    kwargs["context_schema"] = AuthContext
    return _AuthContextRunnable(create_agent(*args, **kwargs))


def install_subagent_auth_context_patch() -> None:
    """Patch the pinned DeepAgents child boundary until it supports context.

    DeepAgents constructs declarative subagents through its module-local
    ``create_agent`` reference, then invokes them from the ``task`` tool without
    forwarding ``ToolRuntime.context``. The adapter gives child graphs the same
    context schema as Mimir's parent graph and supplies only an actual
    ``AuthContext`` from that runtime. A model argument or lookalike object can
    therefore never become the child carrier.
    """
    from deepagents.middleware import subagents as deepagents_subagents

    if getattr(deepagents_subagents, _PATCH_MARKER, False):
        return

    original_create_agent = deepagents_subagents.create_agent
    original_build_task_tool = deepagents_subagents._build_task_tool

    @wraps(original_create_agent)
    def create_agent_with_auth_context(*args: Any, **kwargs: Any) -> Runnable:
        return _create_auth_context_runnable(original_create_agent, *args, **kwargs)

    @wraps(original_build_task_tool)
    def build_task_tool_with_auth_context(*args: Any, **kwargs: Any) -> Any:
        return _wrap_task_tool(original_build_task_tool(*args, **kwargs))

    deepagents_subagents.create_agent = create_agent_with_auth_context
    deepagents_subagents._build_task_tool = build_task_tool_with_auth_context
    setattr(deepagents_subagents, _PATCH_MARKER, True)
