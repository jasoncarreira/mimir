from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from mimir.acp import sdk
from mimir.acp.agent import MimirAcpAgent
from mimir.channel_registry import ChannelRegistry
from mimir.identities import IdentityResolver, hash_web_key
from mimir.turn_event_bus import TurnEventBus


def _resolver(home: Path, *, canonical: str = "operator", key: str = "secret") -> IdentityResolver:
    state = home / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(
        yaml.safe_dump(
            {
                "people": [
                    {
                        "canonical": canonical,
                        "display_name": canonical.title(),
                        "aliases": [hash_web_key(key)],
                        "access": {"roles": ["admin"], "is_service": False},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    resolver = IdentityResolver(home)
    resolver.reload()
    return resolver


class Client:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append(update)


class CoreAgent:
    def __init__(self, bus: TurnEventBus, channels: ChannelRegistry) -> None:
        self.bus = bus
        self.channels = channels
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.cancel = False
        self.fail = False
        self.write_todos = False
        self.pressure = 0
        self.gate: asyncio.Event | None = None
        self.call_gates: list[asyncio.Event] | None = None
        self.fail_calls: set[int] = set()
        self.cancel_calls: set[int] = set()
        self.entered = asyncio.Event()
        self.subscriptions: list[Any] = []
        self.replacement_subscription: asyncio.Queue[dict[str, Any]] | None = None

    async def run_turn(self, event: Any, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))
        call_index = len(self.calls) - 1
        turn_id = kwargs["turn_id"]
        self.subscriptions.append(self.bus._exact_turn_subscribers.get(turn_id))
        if self.replacement_subscription is not None:
            self.bus._exact_turn_subscribers[turn_id] = self.replacement_subscription
        self.entered.set()
        gate = self.call_gates[call_index] if self.call_gates is not None else self.gate
        if gate is not None:
            await gate.wait()
        common = {"turn_id": turn_id, "channel_id": event.channel_id, "seq": 1, "ts": "now"}
        for index in range(self.pressure):
            self.bus.publish({**common, "type": "tool_call", "phase": "start", "id": f"pressure-{index}", "tool_name": "lookup"})
        tool_name = "write_todos" if self.write_todos else "lookup"
        tool_id = "canonical-tool-id"
        self.bus.publish({**common, "type": "tool_call", "phase": "start", "id": tool_id, "tool_name": tool_name})
        if self.cancel or call_index in self.cancel_calls:
            raise asyncio.CancelledError
        if self.fail or call_index in self.fail_calls:
            raise RuntimeError("private failure detail")
        args = ({"todos": [{"content": "ship", "status": "pending"}]} if self.write_todos else {"token": "hidden", "query": "x"})
        self.bus.publish({**common, "type": "tool_call", "phase": "end", "id": tool_id, "tool_name": tool_name, "args": args})
        self.bus.publish({**common, "type": "tool_result", "phase": "end", "id": tool_id, "tool_name": tool_name, "content": {"answer": 1}, "status": "ok"})
        result = await self.channels.send(event.channel_id, "answer")
        assert result.sent


def _bundle(home: Path) -> tuple[Any, CoreAgent]:
    resolver = _resolver(home)
    bus = TurnEventBus()
    channels = ChannelRegistry()
    core_agent = CoreAgent(bus, channels)
    bundle = SimpleNamespace(
        core=SimpleNamespace(identity_resolver=resolver),
        config=SimpleNamespace(home=home, acp_journal_ttl_days=7),
        adapters=SimpleNamespace(channels=channels),
        turn_event_bus=bus,
        agent=core_agent,
    )
    return bundle, core_agent


async def _ready(home: Path) -> tuple[MimirAcpAgent, Client, CoreAgent]:
    bundle, core = _bundle(home)
    agent = MimirAcpAgent(bundle)
    client = Client()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    return agent, client, core


def _types(client: Client) -> list[str]:
    return [item.session_update for item in client.updates]


async def test_new_prompt_runs_bound_core_and_preserves_update_order(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    created = await agent.new_session("/workspace", mcp_servers=[{"name": "one"}])
    session_id = created.session_id
    response = await agent.prompt(
        session_id,
        [
            sdk.TextContentBlock(type="text", text="hello"),
            sdk.ResourceContentBlock(type="resource_link", name="guide", uri="file:///guide"),
        ],
    )

    assert response.stop_reason == "end_turn"
    assert _types(client) == [
        "user_message_chunk",
        "user_message_chunk",
        "tool_call",
        "tool_call_update",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert client.updates[2].tool_call_id == "canonical-tool-id"
    assert client.updates[3].tool_call_id == "canonical-tool-id"
    assert client.updates[3].raw_input == {"token": "[redacted]", "query": "x"}
    first_chunk = client.updates[0].model_dump(mode="json", by_alias=True, exclude_none=True)
    assert first_chunk["content"] == {"type": "text", "text": "hello"}
    assert "messageId" not in first_chunk
    second_chunk = client.updates[1].model_dump(mode="json", by_alias=True, exclude_none=True)
    assert second_chunk["content"] == {"type": "resource_link", "name": "guide", "uri": "file:///guide"}
    assert "messageId" not in second_chunk
    event, kwargs = core.calls[0]
    assert event.content == 'hello\n[resource_link]{"name":"guide","type":"resource_link","uri":"file:///guide"}'
    assert event.author == event.author_id == "operator"
    assert event.source_id == kwargs["turn_id"]
    assert kwargs["session_id"] == kwargs["saga_session_id"] == f"acp:{session_id}"
    assert core.subscriptions[0] is not None
    assert agent._bundle.turn_event_bus._exact_turn_subscribers == {}


async def test_prompt_validates_all_blocks_before_any_update(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/workspace")).session_id

    with pytest.raises(sdk.RequestError) as raised:
        await agent.prompt(
            session_id,
            [sdk.TextContentBlock(type="text", text="ok"), sdk.ImageContentBlock(type="image", data="eA==", mimeType="image/png")],
        )

    assert raised.value.to_error_obj()["code"] == -32602
    assert client.updates == []
    assert core.calls == []


async def test_load_replays_at_least_once_without_mutating_journal(tmp_path: Path) -> None:
    agent, client, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one", mcp_servers=[{"old": True}])).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])
    journal = agent._store.paths(session_id)[0]
    before = journal.read_bytes()
    client.updates.clear()

    await agent.load_session("/two", session_id, mcp_servers=None)
    first = [update.model_dump(mode="json", by_alias=True, exclude_none=True) for update in client.updates]
    client.updates.clear()
    await agent.load_session("/three", session_id, mcp_servers=[])
    second = [update.model_dump(mode="json", by_alias=True, exclude_none=True) for update in client.updates]

    assert first == second
    assert journal.read_bytes() == before
    assert agent._environments[session_id][1].cwd == "/three"
    assert agent._environments[session_id][1].mcp_servers == []


async def test_nonreplayable_load_has_no_replay_prefix(tmp_path: Path) -> None:
    agent, client, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])
    agent._store.mark(session_id, "operator", "overflowed")
    client.updates.clear()

    with pytest.raises(sdk.RequestError) as raised:
        await agent.load_session("/two", session_id)

    assert raised.value.to_error_obj() == {
        "code": -32603,
        "message": "Session replay unavailable: overflowed",
        "data": None,
    }
    assert client.updates == []


async def test_cancelled_bound_turn_terminalizes_open_tools(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    core.cancel = True

    with pytest.raises(sdk.RequestError) as raised:
        await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])

    assert raised.value.to_error_obj() == sdk.internal_error().to_error_obj()
    assert _types(client) == ["user_message_chunk", "tool_call", "tool_call_update"]
    assert client.updates[-1].status == "failed"
    assert session_id not in agent._active_prompts
    assert agent._bundle.turn_event_bus._exact_turn_subscribers == {}

    core.cancel = False
    response = await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="after cancel")])
    assert response.stop_reason == "end_turn"
    assert session_id not in agent._active_prompts


async def test_additional_directories_are_rejected_before_creation(tmp_path: Path) -> None:
    agent, _, _ = await _ready(tmp_path)

    with pytest.raises(sdk.RequestError) as raised:
        await agent.new_session("/one", additional_directories=["/two"])

    assert raised.value.to_error_obj()["code"] == -32602
    assert list(agent._store.root.glob("*.meta.json")) == []


async def test_one_active_prompt_per_session_and_guard_releases(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    core.gate = asyncio.Event()
    first = asyncio.create_task(agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="first")]))
    await core.entered.wait()

    with pytest.raises(sdk.RequestError) as raised:
        await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="second")])
    assert raised.value.to_error_obj() == sdk.internal_error().to_error_obj()
    assert agent._active_prompts.get(session_id) is not None

    core.gate.set()
    await first
    assert session_id not in agent._active_prompts
    assert [update.content.text for update in client.updates if update.session_update == "user_message_chunk"] == ["first"]


async def test_connection_replacement_old_cleanup_preserves_successor_ownership(tmp_path: Path) -> None:
    agent, _, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    old_gate = asyncio.Event()
    successor_gate = asyncio.Event()
    core.call_gates = [old_gate, successor_gate]
    core.fail_calls = {0}
    running = asyncio.create_task(agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="first")]))
    await core.entered.wait()

    replacement = Client()
    agent.on_connect(replacement)
    assert session_id not in agent._active_prompts
    assert agent._environments == {}
    await agent.load_session("/replacement", session_id)
    successor = asyncio.create_task(agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="successor")]))
    while len(core.calls) < 2:
        await asyncio.sleep(0)
    successor_token = agent._active_prompts[session_id]

    old_gate.set()
    with pytest.raises(sdk.RequestError):
        await running
    assert agent._active_prompts.get(session_id) is successor_token

    successor_gate.set()
    response = await successor
    assert response.stop_reason == "end_turn"
    assert session_id not in agent._active_prompts


async def test_normal_core_failure_terminalizes_and_redacts(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    core.fail = True

    with pytest.raises(sdk.RequestError) as raised:
        await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])

    assert raised.value.to_error_obj() == sdk.internal_error().to_error_obj()
    assert _types(client) == ["user_message_chunk", "tool_call", "tool_call_update"]
    assert client.updates[1].tool_call_id == client.updates[2].tool_call_id == "canonical-tool-id"
    assert client.updates[2].status == "failed"
    assert session_id not in agent._active_prompts

    core.fail = False
    response = await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="after failure")])
    assert response.stop_reason == "end_turn"
    assert session_id not in agent._active_prompts


async def test_write_todos_emits_complete_replacement_plan(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    core.write_todos = True

    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="plan")])

    plans = [update for update in client.updates if update.session_update == "plan"]
    assert len(plans) == 1
    assert [(entry.content, entry.status, entry.priority) for entry in plans[0].entries] == [("ship", "pending", "medium")]


@pytest.mark.parametrize("reason", ["expired", "deleted", "io_failed"])
async def test_unavailable_load_reasons_have_no_prefix(tmp_path: Path, reason: str) -> None:
    agent, client, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])
    agent._store.mark(session_id, "operator", reason)
    client.updates.clear()

    with pytest.raises(sdk.RequestError) as raised:
        await agent.load_session("/two", session_id)

    assert raised.value.to_error_obj()["message"] == f"Session replay unavailable: {reason}"
    assert client.updates == []


async def test_sweep_runs_at_start_of_each_authenticated_stateful_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, _ = await _ready(tmp_path)
    calls: list[int] = []
    original = agent._store.sweep

    def sweep(days: int) -> None:
        calls.append(days)
        original(days)

    monkeypatch.setattr(agent._store, "sweep", sweep)
    session_id = (await agent.new_session("/one")).session_id
    await agent.load_session("/two", session_id)
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])

    assert calls == [7, 7, 7]


async def test_environment_is_deep_copied_replaced_cleared_and_not_persisted(tmp_path: Path) -> None:
    agent, _, core = await _ready(tmp_path)
    servers = [{"name": "one", "nested": {"enabled": True}}]
    session_id = (await agent.new_session("/one", mcp_servers=servers)).session_id
    servers[0]["nested"]["enabled"] = False
    environment = agent._environments[session_id][1]
    assert environment.mcp_servers == [{"name": "one", "nested": {"enabled": True}}]
    metadata = agent._store.paths(session_id)[1].read_text(encoding="utf-8")
    assert "/one" not in metadata and "mcp" not in metadata.lower()
    assert core.calls == []

    await agent.load_session("/two", session_id, mcp_servers=None)
    assert agent._environments[session_id][1].cwd == "/two"
    assert agent._environments[session_id][1].mcp_servers is None
    agent.on_connect(Client())
    assert agent._environments == {}


async def test_repeated_load_preserves_metadata_mtime_bytes_and_sequence(tmp_path: Path) -> None:
    agent, client, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])
    journal_path, metadata_path = agent._store.paths(session_id)
    journal = agent._journals._sessions[session_id]
    before = (journal_path.read_bytes(), metadata_path.read_bytes(), journal_path.stat().st_mtime_ns, journal.next_sequence)
    client.updates.clear()

    await agent.load_session("/two", session_id)
    await agent.load_session("/three", session_id)

    after = (journal_path.read_bytes(), metadata_path.read_bytes(), journal_path.stat().st_mtime_ns, journal.next_sequence)
    assert after == before


async def test_load_replay_excludes_live_prompt_publication(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="old")])
    replay_count = len(client.updates)
    client.updates.clear()
    original = client.session_update
    replay_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked(session: str, update: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            replay_started.set()
            await release.wait()
        await original(session, update)

    client.session_update = blocked
    loading = asyncio.create_task(agent.load_session("/two", session_id))
    await replay_started.wait()
    core.entered = asyncio.Event()
    prompting = asyncio.create_task(agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="live")]))
    await asyncio.sleep(0)
    assert not core.entered.is_set()
    release.set()
    await loading
    await prompting
    assert [item.content.text for item in client.updates[:replay_count] if item.session_update == "user_message_chunk"] == ["old"]
    assert next(item.content.text for item in client.updates[replay_count:] if item.session_update == "user_message_chunk") == "live"


async def test_exact_updates_survive_presentation_pressure(tmp_path: Path) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    channel = f"acp:{session_id}"
    presentation = agent._bundle.turn_event_bus.subscribe(channel)
    core.pressure = 300

    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])

    assert presentation.qsize() == 256
    mapped_ids = [update.tool_call_id for update in client.updates if update.session_update in {"tool_call", "tool_call_update"}]
    assert mapped_ids == [*(f"pressure-{index}" for index in range(300)), *(["canonical-tool-id"] * 3)]
    agent._bundle.turn_event_bus.unsubscribe(channel, presentation)


async def test_finally_unsubscribes_only_its_exact_queue_identity(tmp_path: Path) -> None:
    agent, _, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    replacement: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    core.replacement_subscription = replacement
    core.fail = True

    with pytest.raises(sdk.RequestError):
        await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="hello")])

    turn_id = core.calls[0][1]["turn_id"]
    assert core.subscriptions[0] is not replacement
    assert agent._bundle.turn_event_bus._exact_turn_subscribers[turn_id] is replacement
    agent._bundle.turn_event_bus.unsubscribe_exact_turn(turn_id, replacement)


async def test_overflowed_session_accepts_later_live_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.acp.journal as journal_module

    monkeypatch.setattr(journal_module, "MAX_JOURNAL_BYTES", 1)
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id

    first = await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="first")])
    second = await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="second")])

    assert first.stop_reason == second.stop_reason == "end_turn"
    assert len(core.calls) == 2
    assert [update.content.text for update in client.updates if update.session_update == "user_message_chunk"] == ["first", "second"]
    sequences = [update.field_meta["mimir.sequence"] for update in client.updates]
    assert sequences == list(range(len(sequences)))
    assert not agent._store.paths(session_id)[0].exists()
    client.updates.clear()
    with pytest.raises(sdk.RequestError, match="Session replay unavailable: overflowed"):
        await agent.load_session("/two", session_id)
    assert client.updates == []


async def test_nonreplayable_marker_survives_deleted_journal_and_reconstruction(tmp_path: Path) -> None:
    agent, _, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    agent._store.mark(session_id, "operator", "overflowed")
    agent._store.paths(session_id)[0].unlink()

    reconstructed, client, _ = await _ready(tmp_path)
    with pytest.raises(sdk.RequestError) as raised:
        await reconstructed.load_session("/two", session_id)

    assert raised.value.to_error_obj()["message"] == "Session replay unavailable: overflowed"
    assert client.updates == []
