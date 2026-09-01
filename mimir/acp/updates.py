from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from mimir.acp.journal import JournalLease
from mimir.acp.sdk import AgentPlanUpdate, PermissionSnapshot, PlanEntry, RequestError, ToolCallProgress, ToolCallStart
from mimir.turn_event_redaction import scrub_value

_SENSITIVE = {"authorization", "cookie", "password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "private_key"}
_VALID_TODO_STATUS = {"pending", "in_progress", "completed"}
MAX_UPDATE_ITEMS = 128
MAX_UPDATE_BYTES = 8 * 1024 * 1024
UPDATE_CLOSE_TIMEOUT = 2.0


class UpdateDispatcher:
    def __init__(
        self,
        publisher: Any,
        lease: JournalLease | None = None,
        epoch: int = 0,
    ) -> None:
        self.publisher = publisher
        self.lease = lease
        self.epoch = epoch
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=MAX_UPDATE_ITEMS)
        self._queued_bytes = 0
        self._queued_sizes: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        try:
            self._producer = asyncio.current_task()
        except RuntimeError:
            self._producer = None
        self._failure: BaseException | None = None
        self._overflowed = False
        self._publication_failed = False
        self._open_tools: dict[str, str] = {}
        self._tool_args: dict[str, Any] = {}
        self._snapshots: dict[str, PermissionSnapshot] = {}
        self._terminalized_cancelled = False

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    def enqueue(self, event: Mapping[str, Any]) -> None:
        self._ensure_worker()
        allowed = _allowed_event(event)
        size = _event_bytes(allowed)
        if self.queue.full() or self._queued_bytes + size > MAX_UPDATE_BYTES:
            error = RuntimeError("ACP update queue limit exceeded")
            if self._failure is None:
                self._failure = error
            raise RequestError(-32603, "Internal error") from error
        self.queue.put_nowait(allowed)
        self._queued_sizes.put_nowait(size)
        self._queued_bytes += size

    async def submit(self, event: Mapping[str, Any]) -> None:
        if self._overflowed:
            await asyncio.sleep(0)
            return
        try:
            self.enqueue(event)
        except RequestError:
            self._overflowed = True
            current = asyncio.current_task()
            if self._producer is None or self._producer is current:
                raise
            self._producer.cancel()
        await asyncio.sleep(0)

    def permission_snapshot(self, tool_call_id: str) -> PermissionSnapshot | None:
        return self._snapshots.get(tool_call_id)


    async def drain(self) -> None:
        self._ensure_worker()
        await self.queue.join()
        if self._failure is not None:
            raise RequestError(-32603, "Internal error") from self._failure

    async def terminalize_failure(self, error: BaseException | None = None) -> None:
        self._ensure_worker()
        if self._failure is None:
            self._failure = error or RuntimeError("ACP turn failed")
        await self.queue.join()
        for tool_id, name in list(self._open_tools.items()):
            self.enqueue({"type": "_terminal", "phase": "end", "id": tool_id, "tool_name": name})
        await self.queue.join()
        raise RequestError(-32603, "Internal error") from self._failure

    async def terminalize_cancelled(self) -> None:
        if self._terminalized_cancelled:
            return
        self._terminalized_cancelled = True
        self._ensure_worker()
        await self.queue.join()
        updates = [
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId=tool_id,
                status="failed",
                rawOutput={"error": "Tool execution cancelled"},
            )
            for tool_id in self._open_tools
        ]
        self._open_tools.clear()
        self._tool_args.clear()
        self._snapshots.clear()
        if hasattr(self.publisher, "close_turn"):
            await self.publisher.close_turn(updates)
        elif updates:
            for update in updates:
                await self.publisher.publish_live(update)

    async def terminalize_abandoned(self) -> None:
        # A transport can disappear while the worker is blocked delivering an
        # update. Bound that write before appending terminal records directly.
        await self.close()
        updates = [
            ToolCallProgress(
                sessionUpdate="tool_call_update",
                toolCallId=tool_id,
                status="failed",
                rawOutput={"error": "Client disconnected during tool execution"},
            )
            for tool_id in self._open_tools
        ]
        self._open_tools.clear()
        self._tool_args.clear()
        self._snapshots.clear()
        if hasattr(self.publisher, "close_abandoned_turn"):
            await self.publisher.close_abandoned_turn(updates)
        elif hasattr(self.publisher, "close_turn"):
            await self.publisher.close_turn(updates)
        elif updates:
            for update in updates:
                await self.publisher.publish_live(update)

    async def close(self) -> None:
        if self._worker is None:
            return
        worker = self._worker
        try:
            await asyncio.wait_for(self._close_gracefully(worker), UPDATE_CLOSE_TIMEOUT)
        except TimeoutError as exc:
            if self._failure is None:
                self._failure = exc
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            while not self.queue.empty():
                self.queue.get_nowait()
                size = self._queued_sizes.get_nowait()
                self._queued_sizes.task_done()
                self._queued_bytes -= size
                self.queue.task_done()
        self._worker = None

    async def _close_gracefully(self, worker: asyncio.Task[None]) -> None:
        await self.queue.put(None)
        self._queued_sizes.put_nowait(0)
        await self.queue.join()
        await worker

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
                terminal = event.get("type") == "_terminal"
                accepted = True
                if self.lease is not None and not terminal:
                    accept_event = getattr(self.publisher, "accept_event", None)
                    if accept_event is not None:
                        accepted = await accept_event()
                    else:
                        accepted = self.lease.accept()
                if accepted and (self._failure is None or terminal):
                    updates = self._map(event)
                    if not self._publication_failed:
                        for update in updates:
                            try:
                                if self.lease is None or terminal:
                                    await self.publisher.publish_live(update)
                                else:
                                    await self.publisher.publish_live(update, accepted=True)
                            except BaseException as exc:
                                if self._failure is None:
                                    self._failure = exc
                                self._publication_failed = True
                                break
            finally:
                size = self._queued_sizes.get_nowait()
                self._queued_sizes.task_done()
                self._queued_bytes -= size
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
                raw_input = _client_json(event.get("args")) if "args" in event else {}
                self._tool_args[tool_id] = raw_input
                self._snapshots[tool_id] = PermissionSnapshot(
                    tool_call_id=tool_id,
                    title=name,
                    kind="other",
                    raw_input=_freeze_json(raw_input),
                )
                return [ToolCallStart(sessionUpdate="tool_call", toolCallId=tool_id, title=name, kind="other", status="pending", rawInput=raw_input)]
            if phase == "end":
                output: list[Any] = []
                if tool_id not in self._open_tools:
                    self._open_tools[tool_id] = name
                    output.append(ToolCallStart(sessionUpdate="tool_call", toolCallId=tool_id, title=name, kind="other", status="pending"))
                args = _client_json(event.get("args"))
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
            content = _client_json(event.get("content"))
            output.append(ToolCallProgress(sessionUpdate="tool_call_update", toolCallId=tool_id, status="failed" if failed else "completed", rawOutput=content))
            self._open_tools.pop(tool_id, None)
            self._snapshots.pop(tool_id, None)
            args = self._tool_args.pop(tool_id, None)
            if name == "write_todos" and not failed:
                plan = _todos_plan(args)
                if plan is not None:
                    output.append(plan)
            return output
        return []


def _event_bytes(event: Mapping[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _allowed_event(event: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"type", "phase", "id", "tool_name", "args", "content", "status", "is_error"}
    return _strict_json({key: event[key] for key in keys if key in event})


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


def _client_json(value: Any) -> Any:
    return scrub_value(_strict_json(value))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return __import__("types").MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


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
