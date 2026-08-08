from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import mimir.acp.journal as journal_module
from mimir.acp.journal import SessionJournal, _line, _with_sequence
from mimir.acp.sdk import RequestError, TextContentBlock, UserMessageChunk
from mimir.acp.session_store import SessionStore

TURN_ID = "00000000-0000-4000-8000-000000000000"


class Client:
    def __init__(self, actions: list[str] | None = None) -> None:
        self.updates = []
        self.actions = actions
        self.error: BaseException | None = None

    async def session_update(self, session_id, update) -> None:
        if self.actions is not None:
            self.actions.append("send")
        if self.error is not None:
            raise self.error
        self.updates.append((session_id, update))


class BlockingClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def session_update(self, session_id, update) -> None:
        self.entered.set()
        await self.release.wait()
        await super().session_update(session_id, update)


def update(text: str = "hello") -> UserMessageChunk:
    return UserMessageChunk(sessionUpdate="user_message_chunk", content=TextContentBlock(type="text", text=text))


def prepared_row(sequence: int = 0) -> bytes:
    payload = _with_sequence(update(), sequence).model_dump(mode="json", by_alias=True, exclude_none=True)
    return _line({"kind": "prepared", "sequence": sequence, "turn_id": TURN_ID, "update": payload})


def test_owner_paths_modes_and_foreign_are_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    assert oct(store.root.stat().st_mode & 0o777) == "0o700"
    assert oct(record.journal_path.stat().st_mode & 0o777) == "0o600"
    assert oct(record.metadata_path.stat().st_mode & 0o777) == "0o600"
    assert store.load_owned(record.session_id, "owner") == record
    for candidate, owner in [(record.session_id, "foreign"), ("../bad", "owner"), (record.session_id.upper(), "owner")]:
        with pytest.raises(RequestError, match="Invalid session"):
            store.load_owned(candidate, owner)


def test_unsafe_root_and_journal_mode_are_rejected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    os.chmod(record.journal_path, 0o644)
    with pytest.raises(RequestError, match="io_failed"):
        store.load_owned(record.session_id, "owner")
    other = tmp_path / "other"
    unsafe = other / ".mimir" / "acp"
    unsafe.mkdir(parents=True)
    os.chmod(unsafe, 0o755)
    with pytest.raises(OSError, match="unsafe ACP session directory"):
        SessionStore(other)


@pytest.mark.parametrize("target", ["journal", "metadata"])
def test_symlink_and_unsafe_mode_are_rejected(tmp_path: Path, target: str) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    path = record.journal_path if target == "journal" else record.metadata_path
    if target == "journal":
        path.unlink()
        path.symlink_to(record.metadata_path)
        with pytest.raises(RequestError, match="io_failed"):
            store.load_owned(record.session_id, "owner")
    else:
        os.chmod(path, 0o644)
        with pytest.raises(RequestError, match="Invalid session"):
            store.load_owned(record.session_id, "owner")


def test_nonregular_journal_is_rejected_without_output(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    record.journal_path.unlink()
    record.journal_path.mkdir()
    with pytest.raises(RequestError, match="io_failed"):
        store.load_owned(record.session_id, "owner")


@pytest.mark.asyncio
async def test_publish_orders_prepared_fsync_send_sent_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    actions: list[str] = []
    journal = SessionJournal(store, record, Client(actions))
    original_fsync = journal_module.os.fsync

    def fsync(fd: int) -> None:
        actions.append("fsync")
        original_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fsync)
    await journal.publish_live(update(), turn_id=TURN_ID)
    assert actions == ["fsync", "send", "fsync"]


@pytest.mark.asyncio
async def test_prepared_failure_sends_nothing_and_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    monkeypatch.setattr(journal, "_append_durable", lambda body: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(RequestError, match="Internal error"):
        await journal.publish_live(update())
    with pytest.raises(RequestError, match="Internal error"):
        await journal.publish_live(update())
    assert client.updates == []
    assert record.journal_path.read_bytes() == b""
    assert json.loads(record.metadata_path.read_text())["replayability"] == "io_failed"
    assert capsys.readouterr().err.count("replay disabled") == 1


@pytest.mark.asyncio
async def test_send_failure_leaves_prepared_replayable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    client.error = RuntimeError("transport")
    journal = SessionJournal(store, record, client)
    with pytest.raises(RuntimeError, match="transport"):
        await journal.publish_live(update(), turn_id=TURN_ID)
    assert [json.loads(line)["kind"] for line in record.journal_path.read_text().splitlines()] == ["prepared"]
    assert json.loads(record.metadata_path.read_text())["replayability"] == "replayable"


@pytest.mark.asyncio
async def test_sent_failure_preserves_send_and_prevents_following_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    original = journal._append_durable
    calls = 0

    def fail_sent(body: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sent")
        original(body)

    monkeypatch.setattr(journal, "_append_durable", fail_sent)
    await journal.publish_live(update())
    with pytest.raises(RequestError, match="Internal error"):
        await journal.publish_live(update("later"))
    assert len(client.updates) == 1
    assert json.loads(record.metadata_path.read_text())["replayability"] == "io_failed"


@pytest.mark.asyncio
async def test_capacity_equality_then_overflow_writes_no_pair_and_continues_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    first = _with_sequence(update(), 0)
    pair_size = len(_line({"kind": "prepared", "sequence": 0, "turn_id": TURN_ID, "update": first.model_dump(mode="json", by_alias=True, exclude_none=True)})) + len(_line({"kind": "sent", "sequence": 0}))
    monkeypatch.setattr(journal_module, "MAX_JOURNAL_BYTES", pair_size)
    await journal.publish_live(update(), turn_id=TURN_ID)
    before = record.journal_path.read_bytes()
    await journal.publish_live(update("overflow"), turn_id=TURN_ID)
    await journal.publish_live(update("continued"), turn_id=TURN_ID)
    assert record.journal_path.read_bytes() == before
    assert len(client.updates) == 3
    assert [item[1].field_meta["mimir.sequence"] for item in client.updates] == [0, 1, 2]
    assert json.loads(record.metadata_path.read_text())["replayability"] == "overflowed"
    assert capsys.readouterr().err.count("capacity exceeded") == 1


@pytest.mark.asyncio
async def test_overflow_marker_failure_writes_and_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    monkeypatch.setattr(journal_module, "MAX_JOURNAL_BYTES", 0)
    monkeypatch.setattr(store, "mark", lambda *args: (_ for _ in ()).throw(OSError("marker")))
    with pytest.raises(RequestError, match="Internal error"):
        await journal.publish_live(update())
    assert record.journal_path.read_bytes() == b""
    assert client.updates == []


@pytest.mark.asyncio
async def test_prepared_without_sent_replays_every_time_without_mutation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    record.journal_path.write_bytes(prepared_row())
    client = Client()
    journal = SessionJournal(store, record, client)
    before = (record.journal_path.read_bytes(), record.journal_path.stat().st_mtime_ns, record.metadata_path.read_bytes(), record.metadata_path.stat().st_mtime_ns, journal.next_sequence)
    await journal.send_replay()
    await journal.send_replay()
    after = (record.journal_path.read_bytes(), record.journal_path.stat().st_mtime_ns, record.metadata_path.read_bytes(), record.metadata_path.stat().st_mtime_ns, journal.next_sequence)
    assert before == after
    assert len(client.updates) == 2
    assert client.updates[0][1].field_meta == {"mimir.sequence": 0}


@pytest.mark.asyncio
async def test_replay_and_live_are_excluded_by_one_lock(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    record.journal_path.write_bytes(prepared_row())
    replay_client = BlockingClient()
    journal = SessionJournal(store, record, replay_client)
    replay_task = asyncio.create_task(journal.send_replay())
    await replay_client.entered.wait()
    live_client = Client()
    live_task = asyncio.create_task(journal.publish_live(update("live"), live_client))
    await asyncio.sleep(0)
    assert live_client.updates == []
    replay_client.release.set()
    await replay_task
    await live_task
    assert len(live_client.updates) == 1


@pytest.mark.parametrize("body", [b"not-json\n", b'{"kind":"sent","sequence":0}\n', b'{"kind":"prepared","sequence":1,"turn_id":"00000000-0000-4000-8000-000000000000","update":{}}\n'])
def test_corruption_marks_io_failed_before_any_replay(tmp_path: Path, body: bytes) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    record.journal_path.write_bytes(body)
    client = Client()
    with pytest.raises(RequestError, match="io_failed"):
        SessionJournal(store, record, client)
    assert client.updates == []
    assert json.loads(record.metadata_path.read_text())["replayability"] == "io_failed"


def test_ttl_marker_before_unlink_failure_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    mtime = record.journal_path.stat().st_mtime_ns
    store.sweep_expired(1, now_ns=mtime + 86_400_000_000_000)
    assert record.journal_path.exists()
    original = store.mark
    monkeypatch.setattr(store, "mark", lambda *args: (_ for _ in ()).throw(OSError("marker")))
    store.sweep_expired(1, now_ns=mtime + 86_400_000_000_001)
    assert record.journal_path.exists()
    monkeypatch.setattr(store, "mark", original)
    store.sweep_expired(1, now_ns=mtime + 86_400_000_000_001)
    assert not record.journal_path.exists()
    assert json.loads(record.metadata_path.read_text())["replayability"] == "expired"


def test_delete_marker_before_unlink_failure_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    original = store.mark
    monkeypatch.setattr(store, "mark", lambda *args: (_ for _ in ()).throw(OSError("marker")))
    with pytest.raises(OSError, match="marker"):
        store.delete_owned_session(record.session_id, "owner")
    assert record.journal_path.exists()
    monkeypatch.setattr(store, "mark", original)
    store.delete_owned_session(record.session_id, "owner")
    assert not record.journal_path.exists()
    assert json.loads(record.metadata_path.read_text())["replayability"] == "deleted"
    store.delete_owned_session(record.session_id, "owner")


@pytest.mark.asyncio
async def test_replay_send_failure_after_prefix_is_invariant_and_later_retries_all(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    record.journal_path.write_bytes(prepared_row(0) + prepared_row(1))

    class PrefixFailClient(Client):
        async def session_update(self, session_id, update) -> None:
            if len(self.updates) == 1:
                raise RuntimeError("transport")
            await super().session_update(session_id, update)

    client = PrefixFailClient()
    journal = SessionJournal(store, record, client)
    before = (
        record.journal_path.read_bytes(),
        record.journal_path.stat().st_mtime_ns,
        record.metadata_path.read_bytes(),
        record.metadata_path.stat().st_mtime_ns,
        journal.next_sequence,
    )
    with pytest.raises(RuntimeError, match="transport"):
        await journal.send_replay()
    after_failure = (
        record.journal_path.read_bytes(),
        record.journal_path.stat().st_mtime_ns,
        record.metadata_path.read_bytes(),
        record.metadata_path.stat().st_mtime_ns,
        journal.next_sequence,
    )
    assert after_failure == before
    assert [item[1].field_meta["mimir.sequence"] for item in client.updates] == [0]
    retry = Client()
    await journal.send_replay(retry)
    assert [item[1].field_meta["mimir.sequence"] for item in retry.updates] == [0, 1]
    assert (
        record.journal_path.read_bytes(),
        record.journal_path.stat().st_mtime_ns,
        record.metadata_path.read_bytes(),
        record.metadata_path.stat().st_mtime_ns,
        journal.next_sequence,
    ) == before


@pytest.mark.asyncio
async def test_sent_failure_and_marker_failure_preserve_observed_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    client = Client()
    journal = SessionJournal(store, record, client)
    original = journal._append_durable
    calls = 0

    def fail_sent(body: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sent")
        original(body)

    monkeypatch.setattr(journal, "_append_durable", fail_sent)
    monkeypatch.setattr(store, "try_mark", lambda *args: False)
    delivered = await journal.publish_live(update())
    assert delivered is client.updates[0][1]
    assert len(client.updates) == 1
    assert json.loads(record.metadata_path.read_text())["replayability"] == "replayable"
    with pytest.raises(RequestError, match="Internal error"):
        await journal.publish_live(update("later"))
    assert len(client.updates) == 1


@pytest.mark.parametrize("operation,failure", [("ttl", "unlink"), ("ttl", "fsync"), ("delete", "unlink"), ("delete", "fsync")])
def test_post_marker_cleanup_failure_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str) -> None:
    store = SessionStore(tmp_path)
    record = store.create_session("owner")
    original_unlink = Path.unlink
    original_fsync = store._fsync_parent
    cleanup_calls = 0

    if failure == "unlink":
        def unlink(path: Path, *args, **kwargs) -> None:
            nonlocal cleanup_calls
            if path == record.journal_path and cleanup_calls == 0:
                cleanup_calls += 1
                raise OSError("unlink")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
    else:
        def fsync_parent() -> None:
            nonlocal cleanup_calls
            if cleanup_calls == 0:
                cleanup_calls += 1
                raise OSError("parent fsync")
            original_fsync()

        monkeypatch.setattr(store, "_fsync_parent", fsync_parent)

    if operation == "ttl":
        now_ns = record.journal_path.stat().st_mtime_ns + 86_400_000_000_001
        store.sweep_expired(1, now_ns=now_ns)
        metadata = json.loads(record.metadata_path.read_text())
        assert metadata["replayability"] == "expired"
        store.sweep_expired(1, now_ns=now_ns)
    else:
        with pytest.raises(OSError):
            store.delete_owned_session(record.session_id, "owner")
        metadata = json.loads(record.metadata_path.read_text())
        assert metadata["replayability"] == "deleted"
        store.delete_owned_session(record.session_id, "owner")

    assert cleanup_calls == 1
    assert not record.journal_path.exists()
