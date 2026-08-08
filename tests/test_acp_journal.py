from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from mimir.acp.journal import SessionJournal
from mimir.acp.sdk import TextContentBlock, UserMessageChunk
from mimir.acp.session_store import SessionStore


class Client:
    def __init__(self) -> None:
        self.updates = []

    async def session_update(self, session_id, update) -> None:
        self.updates.append((session_id, update))


def update(text: str = "hello") -> UserMessageChunk:
    return UserMessageChunk(sessionUpdate="user_message_chunk", content=TextContentBlock(type="text", text=text))


def test_owner_paths_modes_and_foreign_are_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    assert oct(store.root.stat().st_mode & 0o777) == "0o700"
    assert oct(record.journal_path.stat().st_mode & 0o777) == "0o600"
    assert oct(record.metadata_path.stat().st_mode & 0o777) == "0o600"
    assert store.load_owned(record.session_id, "owner") == record
    with pytest.raises(Exception, match="Invalid session"):
        store.load_owned(record.session_id, "foreign")
    with pytest.raises(Exception, match="Invalid session"):
        store.load_owned("../bad", "owner")


@pytest.mark.asyncio
async def test_publish_and_replay_are_prepared_send_sent_and_invariant(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    await journal.publish_live(update())
    rows = [json.loads(line) for line in record.journal_path.read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["prepared", "sent"]
    assert rows[0]["sequence"] == 0
    assert rows[0]["update"]["_meta"] == {"mimir.sequence": 0}
    before = (record.journal_path.read_bytes(), record.journal_path.stat().st_mtime_ns, journal.next_sequence)
    replay = Client()
    await journal.send_replay(replay)
    await journal.send_replay(replay)
    after = (record.journal_path.read_bytes(), record.journal_path.stat().st_mtime_ns, journal.next_sequence)
    assert before == after
    assert len(replay.updates) == 2


@pytest.mark.asyncio
async def test_prepared_without_sent_replays_at_least_once(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    payload = update().model_copy(update={"field_meta": {"mimir.sequence": 0}}).model_dump(mode="json", by_alias=True, exclude_none=True)
    record.journal_path.write_text(json.dumps({"kind": "prepared", "sequence": 0, "turn_id": "00000000-0000-4000-8000-000000000000", "update": payload}, separators=(",", ":")) + "\n")
    os.chmod(record.journal_path, 0o600)
    client = Client()
    journal = SessionJournal(store, record, client)
    await journal.send_replay()
    assert client.updates[0][1].field_meta == {"mimir.sequence": 0}


def test_delete_marks_before_unlink_and_retries(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    store.delete_owned_session(record.session_id, "owner")
    assert not record.journal_path.exists()
    metadata = json.loads(record.metadata_path.read_text())
    assert (metadata["lifecycle"], metadata["replayability"]) == ("deleted", "deleted")
    store.delete_owned_session(record.session_id, "owner")
