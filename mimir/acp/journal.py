from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

from mimir.acp.sdk import RequestError, SessionNotification
from mimir.acp.session_store import SessionRecord, SessionStore

MAX_JOURNAL_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class JournalLease:
    turn_id: str
    generation: int
    epoch: int
    closed: bool = False
    _accepted: int = 0
    _terminalized: bool = False

    def accept(self) -> bool:
        if self.closed:
            return False
        self._accepted += 1
        return True

    def close(self) -> bool:
        if self.closed:
            return False
        self.closed = True
        return True

    @property
    def accepted(self) -> int:
        return self._accepted

_UPDATE_ADAPTER = TypeAdapter(SessionNotification.model_fields["update"].annotation)


class SessionJournal:
    def __init__(self, store: SessionStore, record: SessionRecord, client: Any | None = None) -> None:
        self.store = store
        self.record = record
        self.current_client = client
        self.lock = asyncio.Lock()
        self.journal_enabled = record.replayability == "replayable"
        self._reported = False
        self._fatal = False
        try:
            prepared, _ = self._read_validated()
        except BaseException as exc:
            self.journal_enabled = False
            self._fatal = True
            self.store.try_mark(record.session_id, record.owner_principal, "io_failed")
            self._report_once("ACP journal validation failed; replay disabled")
            raise RequestError(-32603, "Session replay unavailable: io_failed") from exc
        self.next_sequence = len(prepared)

    def bind_client(self, client: Any) -> None:
        self.current_client = client

    async def publish_live(
        self,
        update: Any,
        client: Any | None = None,
        *,
        turn_id: str | None = None,
        lease: JournalLease | None = None,
        accepted: bool = False,
    ) -> Any:
        async with self.lock:
            if lease is not None and lease.closed and not accepted:
                return None
            return await self._publish_locked(update, client or self.current_client, turn_id)

    async def close_turn(
        self,
        lease: JournalLease,
        terminal_updates: list[Any],
        client: Any | None = None,
    ) -> list[Any]:
        async with self.lock:
            lease.close()
            if lease._terminalized:
                return []
            lease._terminalized = True
            published = []
            for update in terminal_updates:
                published.append(
                    await self._publish_locked(
                        update, client or self.current_client, lease.turn_id
                    )
                )
            return published

    async def _publish_locked(self, update: Any, client: Any, turn_id: str | None) -> Any:
        if self._fatal or client is None:
            raise RequestError(-32603, "Internal error")
        sequence = self.next_sequence
        self.next_sequence += 1
        update = _with_sequence(update, sequence)
        prepared = _line({"kind": "prepared", "sequence": sequence, "turn_id": turn_id or str(__import__("uuid").uuid4()), "update": update.model_dump(mode="json", by_alias=True, exclude_none=True)})
        sent = _line({"kind": "sent", "sequence": sequence})
        if self.journal_enabled:
            try:
                current_size = self.record.journal_path.stat().st_size
                if current_size + len(prepared) + len(sent) > MAX_JOURNAL_BYTES:
                    self.store.mark(self.record.session_id, self.record.owner_principal, "overflowed")
                    self.journal_enabled = False
                    self._report_once("ACP journal capacity exceeded; replay disabled")
                else:
                    self._append_durable(prepared)
            except BaseException as exc:
                self.journal_enabled = False
                self._fatal = True
                self.store.try_mark(self.record.session_id, self.record.owner_principal, "io_failed")
                self._report_once("ACP journal write failed; replay disabled")
                raise RequestError(-32603, "Internal error") from exc
        await client.session_update(self.record.session_id, update)
        if self.journal_enabled:
            try:
                self._append_durable(sent)
            except BaseException:
                self.journal_enabled = False
                self._fatal = True
                self.store.try_mark(self.record.session_id, self.record.owner_principal, "io_failed")
                self._report_once("ACP journal write failed after delivery; replay disabled")
        return update

    async def send_replay(self, client: Any | None = None) -> None:
        async with self.lock:
            client = client or self.current_client
            if client is None:
                raise RequestError(-32603, "Internal error")
            try:
                prepared, _ = self._read_validated()
            except BaseException as exc:
                self.journal_enabled = False
                self.store.try_mark(self.record.session_id, self.record.owner_principal, "io_failed")
                self._report_once("ACP journal replay failed; replay disabled")
                raise RequestError(-32603, "Session replay unavailable: io_failed") from exc
            for item in prepared:
                update = _UPDATE_ADAPTER.validate_python(item["update"])
                await client.session_update(self.record.session_id, update)

    def _read_validated(self) -> tuple[list[dict[str, Any]], set[int]]:
        self.store._validate_file(self.record.journal_path)
        prepared: list[dict[str, Any]] = []
        sent: set[int] = set()
        with self.record.journal_path.open("rb") as stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    raise ValueError("partial journal record")
                item = json.loads(raw, object_pairs_hook=_unique_object)
                if not isinstance(item, dict):
                    raise ValueError("invalid journal record")
                if item.get("kind") == "prepared":
                    sequence = item.get("sequence")
                    if set(item) != {"kind", "sequence", "turn_id", "update"} or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != len(prepared) or not _canonical_uuid(item["turn_id"]) or not isinstance(item["update"], dict):
                        raise ValueError("invalid prepared record")
                    if item["update"].get("_meta") != {"mimir.sequence": sequence}:
                        raise ValueError("invalid prepared metadata")
                    _UPDATE_ADAPTER.validate_python(item["update"])
                    prepared.append(item)
                elif item.get("kind") == "sent":
                    sequence = item.get("sequence")
                    if set(item) != {"kind", "sequence"} or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0 or sequence >= len(prepared) or sequence in sent:
                        raise ValueError("invalid sent record")
                    sent.add(sequence)
                else:
                    raise ValueError("invalid journal record")
        return prepared, sent

    def _append_durable(self, body: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.record.journal_path, flags)
        try:
            info = os.fstat(fd)
            if not __import__("stat").S_ISREG(info.st_mode) or __import__("stat").S_IMODE(info.st_mode) != 0o600 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
                raise OSError("unsafe ACP session file")
            written = 0
            while written < len(body):
                count = os.write(fd, body[written:])
                if count <= 0:
                    raise OSError("short ACP journal write")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)

    def _report_once(self, message: str) -> None:
        if not self._reported:
            print(message, file=sys.stderr)
            self._reported = True


class JournalCache:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._sessions: dict[str, SessionJournal] = {}

    def open(self, record: SessionRecord, client: Any | None = None) -> SessionJournal:
        journal = self._sessions.get(record.session_id)
        if journal is None:
            journal = SessionJournal(self.store, record, client)
            self._sessions[record.session_id] = journal
        elif client is not None:
            journal.bind_client(client)
        return journal

    def discard(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def _with_sequence(update: Any, sequence: int) -> Any:
    return update.model_copy(update={"field_meta": {"mimir.sequence": sequence}})


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = __import__("uuid").UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal key")
        result[key] = value
    return result


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


SessionSequencer = SessionJournal
