from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from mimir.acp.sdk import AgentPlanUpdate, PlanEntry, RequestError, ToolCallProgress, ToolCallStart

_SENSITIVE = {"authorization", "cookie", "password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "private_key"}
_VALID_TODO_STATUS = {"pending", "in_progress", "completed"}


class UpdateDispatcher:
    def __init__(self, publisher: Any) -> None:
        self.publisher = publisher
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._open_tools: dict[str, str] = {}
        self._tool_args: dict[str, Any] = {}

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def enqueue(self, event: Mapping[str, Any]) -> None:
        self._ensure_worker()
        self.queue.put_nowait(_allowed_event(event))

    submit = enqueue

    async def consume(self, source: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            event = await source.get()
            try:
                self.enqueue(event)
            finally:
                source.task_done()

    async def drain(self) -> None:
        self._ensure_worker()
        await self.queue.join()
        if self._failure is not None:
            raise RequestError(-32603, "Internal error") from self._failure

    async def terminalize_failure(self, error: BaseException | None = None) -> None:
        self._ensure_worker()
        await self.queue.join()
        if self._failure is None:
            self._failure = error or RuntimeError("ACP turn failed")
        for tool_id, name in list(self._open_tools.items()):
            self.queue.put_nowait({"type": "_terminal", "phase": "end", "id": tool_id, "tool_name": name})
        await self.queue.join()
        raise RequestError(-32603, "Internal error") from self._failure

    async def close(self) -> None:
        if self._worker is None:
            return
        self.queue.put_nowait(None)
        await self.queue.join()
        await self._worker
        self._worker = None

    def invalidate(self) -> None:
        if self._failure is None:
            self._failure = RuntimeError("ACP connection was replaced")

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                if self._failure is None or event.get("type") == "_terminal":
                    for update in self._map(event):
                        try:
                            await self.publisher.publish_live(update)
                        except BaseException as exc:
                            if self._failure is None:
                                self._failure = exc
                            break
            finally:
                self.queue.task_done()

    def _map(self, event: dict[str, Any]) -> list[Any]:
        kind = event.get("type")
        phase = event.get("phase")
        tool_id = event.get("id")
        name = str(event.get("tool_name") or "unknown")
        if kind in {"turn", "reasoning", "model", "outbound_message", "injected_input"}:
            return []
        if kind == "_terminal" and isinstance(tool_id, str):
            if tool_id not in self._open_tools:
                return []
            self._open_tools.pop(tool_id, None)
            self._tool_args.pop(tool_id, None)
            return [ToolCallProgress(sessionUpdate="tool_call_update", toolCallId=tool_id, status="failed", rawOutput={"error": "Tool execution failed"})]
        if kind == "tool_call":
            if name == "send_message":
                return []
            if not isinstance(tool_id, str) or not tool_id:
                return []
            if phase == "start":
                if tool_id in self._open_tools:
                    return []
                self._open_tools[tool_id] = name
                return [ToolCallStart(sessionUpdate="tool_call", toolCallId=tool_id, title=name, kind="other", status="pending")]
            if phase == "end":
                output: list[Any] = []
                if tool_id not in self._open_tools:
                    self._open_tools[tool_id] = name
                    output.append(ToolCallStart(sessionUpdate="tool_call", toolCallId=tool_id, title=name, kind="other", status="pending"))
                args = _strict_json(event.get("args"))
                self._tool_args[tool_id] = args
                output.append(ToolCallProgress(sessionUpdate="tool_call_update", toolCallId=tool_id, status="in_progress", rawInput=args))
                return output
            return []
        if kind == "tool_result":
            if name == "send_message":
                return []
            if not isinstance(tool_id, str) or not tool_id or phase != "end":
                return []
            output = []
            if tool_id not in self._open_tools:
                self._open_tools[tool_id] = name
                output.append(ToolCallStart(sessionUpdate="tool_call", toolCallId=tool_id, title=name, kind="other", status="pending"))
            failed = event.get("status") not in {None, "ok", "completed", "success"} or bool(event.get("is_error"))
            content = _strict_json(event.get("content"))
            output.append(ToolCallProgress(sessionUpdate="tool_call_update", toolCallId=tool_id, status="failed" if failed else "completed", rawOutput=content))
            self._open_tools.pop(tool_id, None)
            args = self._tool_args.pop(tool_id, None)
            if name == "write_todos" and not failed:
                plan = _todos_plan(args)
                if plan is not None:
                    output.append(plan)
            return output
        return []


def _allowed_event(event: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"type", "phase", "id", "tool_name", "args", "content", "status", "is_error"}
    return {key: event[key] for key in keys if key in event}


def _strict_json(value: Any, key: str = "") -> Any:
    normalized_key = "".join(character for character in key.lower() if character.isalnum())
    if normalized_key in {"authorization", "cookie", "password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey", "accesstoken", "refreshtoken"}:
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if __import__("math").isfinite(value) else "[redacted]"
    if isinstance(value, Mapping):
        return {str(k): _strict_json(v, str(k)) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_strict_json(item) for item in value]
    return "[redacted]"


def _todos_plan(args: Any) -> AgentPlanUpdate | None:
    if not isinstance(args, dict) or set(args) != {"todos"} or not isinstance(args["todos"], list):
        return None
    entries = []
    for todo in args["todos"]:
        if not isinstance(todo, dict) or set(todo) != {"content", "status"}:
            return None
        content = todo.get("content")
        status = todo.get("status")
        if not isinstance(content, str) or not content.strip() or status not in _VALID_TODO_STATUS:
            return None
        entries.append(PlanEntry(content=content, status=status, priority="medium"))
    if not entries:
        return None
    return AgentPlanUpdate(sessionUpdate="plan", entries=entries)


ACPUpdateDispatcher = UpdateDispatcher
