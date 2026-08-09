from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import mimir.acp.agent as agent_module
from mimir.acp import sdk
from mimir.acp.agent import ActivePrompt, MimirAcpAgent
from mimir.acp.journal import JournalLease
from mimir.acp.updates import UpdateDispatcher
from mimir.tools.client_provider import MIMIR_HANDS_V1, PermissionDecision, PermissionEligibility, get_turn_capability_context, hands_edit
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
        self.call_provider = False
        self.cancel_resisted = asyncio.Event()
        self.cancel_release = asyncio.Event()

    async def run_turn(self, event: Any, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))
        call_index = len(self.calls) - 1
        turn_id = kwargs["turn_id"]
        self.subscriptions.append(self.bus._exact_turn_subscribers.get(turn_id))
        if self.replacement_subscription is not None:
            self.bus._exact_turn_subscribers[turn_id] = self.replacement_subscription
        self.entered.set()
        gate = self.call_gates[call_index] if self.call_gates is not None else self.gate
        if self.call_provider:
            context = get_turn_capability_context()
            assert context is not None
            try:
                await context.provider.call_tool("edit", {"path": "a", "oldText": "x", "newText": "y"})
            except asyncio.CancelledError:
                self.cancel_resisted.set()
                await self.cancel_release.wait()
                raise
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
    created = await agent.new_session("/workspace")
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
    session_id = (await agent.new_session("/one")).session_id
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
    with pytest.raises(sdk.RequestError) as unauthenticated:
        await agent.load_session("/replacement", session_id)
    assert unauthenticated.value.to_error_obj()["code"] == -32000
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
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


async def test_malformed_provider_declaration_creates_no_state(tmp_path: Path) -> None:
    agent, _, core = await _ready(tmp_path)

    with pytest.raises(sdk.RequestError) as raised:
        await agent.new_session(
            "/one",
            mcp_servers=[{"name": "one", "nested": {"enabled": True}}],
        )

    assert raised.value.to_error_obj()["code"] == -32602
    assert agent._environments == {}
    assert list(agent._store.root.glob("*.meta.json")) == []
    assert core.calls == []


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


async def test_providerless_public_turn_fails_closed_without_remote_authority(tmp_path: Path) -> None:
    bundle, core = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class ProviderlessClient(Client):
        def __init__(self) -> None:
            super().__init__()
            self.remote_calls: list[str] = []

        async def request_tool_permission(self, *args: Any, **kwargs: Any) -> Any:
            self.remote_calls.append("permission")

        async def message_mcp(self, *args: Any, **kwargs: Any) -> Any:
            self.remote_calls.append("provider")

    client = ProviderlessClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/workspace")).session_id
    observed: list[Any] = []

    async def providerless_turn(*args: Any, **kwargs: Any) -> None:
        context = get_turn_capability_context()
        observed.append(context.profile_policy if context is not None else object())
        await hands_edit.ainvoke({"path": "a", "old_text": "x", "new_text": "y"})

    core.run_turn = providerless_turn
    with pytest.raises(sdk.RequestError):
        await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="edit")])

    assert observed == [None]
    assert client.remote_calls == []
    assert agent._sessions[session_id].active_prompt is None


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


class McpClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.connects: list[str] = []
        self.disconnects: list[str] = []
        self.notifications: list[tuple[str, str, Any]] = []
        self.messages: list[tuple[str, str, Any]] = []

    async def connect_mcp(self, server_id: str) -> str:
        self.connects.append(server_id)
        return f"connection-{len(self.connects)}"

    async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
        self.messages.append((connection_id, method, params))
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "hands", "version": "1"}}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": tool.provider_name,
                        "description": tool.description,
                        "inputSchema": _thaw_schema(tool.input_schema),
                        "outputSchema": _thaw_schema(tool.result_schema),
                    }
                    for tool in MIMIR_HANDS_V1.tools
                ]
            }
        return {"changed": True}

    async def notify_mcp(self, connection_id: str, method: str, params: Any = None) -> None:
        self.notifications.append((connection_id, method, params))

    async def disconnect_mcp(self, connection_id: str) -> None:
        self.disconnects.append(connection_id)


def _thaw_schema(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw_schema(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_schema(item) for item in value]
    return value


def _hands(server_id: str) -> list[dict[str, Any]]:
    return [{"type": "acp", "name": "mimir-hands", "serverId": server_id}]


@pytest.mark.parametrize(
    "servers",
    [
        [{"type": "stdio", "name": "mimir-hands", "command": "hands"}],
        [{"type": "http", "name": "mimir-hands", "url": "https://example.test"}],
        [{"type": "sse", "name": "mimir-hands", "url": "https://example.test"}],
        [{"type": "acp", "name": "unknown", "serverId": "one"}],
        [{"type": "acp", "name": "mimir-hands", "serverId": ""}],
        [{"type": "acp", "name": "mimir-hands", "serverId": "one", "_meta": "bad"}],
        [{"type": "acp", "name": "mimir-hands", "serverId": "one", "extra": True}],
        _hands("one") + _hands("two"),
    ],
)
async def test_provider_admission_rejects_before_connect_or_state(tmp_path: Path, servers: Any) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)
    client = McpClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})

    with pytest.raises(sdk.RequestError) as raised:
        await agent.new_session("/workspace", mcp_servers=servers)

    assert raised.value.to_error_obj()["code"] == -32602
    assert client.connects == []
    assert agent._sessions == {}
    assert list(agent._store.root.glob("*.meta.json")) == []


async def test_provider_indexes_are_session_owned_and_load_uses_fresh_connection(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)
    client = McpClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    first = (await agent.new_session("/one", mcp_servers=_hands("server-a"))).session_id
    second = (await agent.new_session("/two", mcp_servers=_hands("server-b"))).session_id

    assert set(agent._connection.server_sessions) == {"server-a", "server-b"}
    assert set(agent._connection.connection_sessions) == {"connection-1", "connection-2"}
    assert agent._sessions[first].provider is not agent._sessions[second].provider

    await agent.load_session("/reloaded", first, mcp_servers=_hands("server-a"))

    assert client.connects == ["server-a", "server-b", "server-a"]
    assert agent._sessions[first].provider.connection_id == "connection-3"
    assert "connection-1" not in agent._connection.connection_sessions
    assert client.disconnects == ["connection-1"]


async def test_list_changed_revalidates_only_fresh_tools_list_serially(tmp_path: Path) -> None:
    class StrictClient(McpClient):
        def __init__(self) -> None:
            super().__init__()
            self.list_active = 0
            self.max_list_active = 0

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            if method == "initialize" and any(call[1] == "initialize" for call in self.messages):
                raise RuntimeError("duplicate initialize")
            if method == "tools/list":
                self.list_active += 1
                self.max_list_active = max(self.max_list_active, self.list_active)
                await asyncio.sleep(0.01)
                try:
                    return await super().message_mcp(connection_id, method, params)
                finally:
                    self.list_active -= 1
            return await super().message_mcp(connection_id, method, params)

    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)
    client = StrictClient()
    generation = agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server-a"))).session_id
    connection_id = agent._sessions[session_id].provider.connection_id

    await asyncio.gather(
        agent.on_mcp_notification(generation, connection_id, "notifications/tools/list_changed", None),
        agent.on_mcp_notification(generation, connection_id, "notifications/tools/list_changed", None),
    )
    await asyncio.gather(*agent._connections[generation].tasks)

    assert [method for _, method, _ in client.messages].count("initialize") == 1
    assert [method for _, method, _ in client.messages].count("tools/list") == 3
    assert client.max_list_active == 1
    assert agent._sessions[session_id].provider is not None


async def test_transport_teardown_is_generation_scoped_and_requires_load(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)
    old = McpClient()
    old_generation = agent.on_connect(old)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server-a"))).session_id
    successor = McpClient()
    successor_generation = agent.on_connect(successor)

    await agent.on_transport_closed(old_generation)

    assert agent._generation == successor_generation
    assert agent._connection.closed is False
    assert session_id not in agent._environments


async def test_active_prompt_permission_uses_admitted_snapshot_and_is_once_scoped() -> None:
    class Publisher:
        def __init__(self) -> None:
            self.updates: list[Any] = []

        async def publish_live(self, update: Any, **kwargs: Any) -> None:
            self.updates.append(update)

    class Peer:
        def __init__(self) -> None:
            self.snapshots: list[Any] = []

        async def request_tool_permission(self, session_id: str, snapshot: Any) -> Any:
            self.snapshots.append((session_id, snapshot))
            return SimpleNamespace(decision="allow_once", error=None)

    lease = JournalLease("00000000-0000-0000-0000-000000000001", 1, 1)
    dispatcher = UpdateDispatcher(Publisher(), lease, 1)
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "tool-1", "tool_name": "hands_edit", "args": {"path": "a", "token": "secret"}})
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "tool-1", "tool_name": "hands_edit", "args": {"path": "changed", "token": "later"}})
    await dispatcher.drain()
    peer = Peer()
    owner = SimpleNamespace(_boundary_lock=asyncio.Lock())
    session = SimpleNamespace(provider=SimpleNamespace(peer=peer, agent=owner), generation=1, prompt_epoch=1, record=SimpleNamespace(session_id="session-1"), active_prompt=None)
    forwarder = asyncio.create_task(asyncio.sleep(3600))
    active = ActivePrompt(session, 1, 1, None, None, forwarder, dispatcher, lease)
    session.active_prompt = active

    decision = await active.request_permission(PermissionEligibility("tool-1", "ignored", "ignored", {"path": "a", "token": "secret"}))
    lease.close()
    stale = await active.request_permission(PermissionEligibility("tool-1", "ignored", "ignored", {"path": "a", "token": "secret"}))

    assert decision is PermissionDecision.ALLOW_ONCE
    assert stale is PermissionDecision.REJECT_ONCE
    assert peer.snapshots[0][1].title == "hands_edit"
    assert peer.snapshots[0][1].raw_input == {"path": "a", "token": "[redacted]"}
    forwarder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await forwarder
    await dispatcher.close()


async def test_explicit_cancel_drains_accepted_tool_and_terminalizes_before_response(
    tmp_path: Path,
) -> None:
    agent, client, core = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    core.gate = asyncio.Event()
    prompt = asyncio.create_task(
        agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="cancel")])
    )
    await core.entered.wait()
    turn_id = core.calls[0][1]["turn_id"]
    agent._bundle.turn_event_bus.publish(
        {
            "turn_id": turn_id,
            "channel_id": agent._sessions[session_id].record.thread_id,
            "seq": 1,
            "ts": "now",
            "type": "tool_call",
            "phase": "start",
            "id": "accepted-tool",
            "tool_name": "hands_edit",
            "args": {"path": "a", "token": "secret"},
        }
    )
    dispatcher = agent._active_prompts[session_id].dispatcher
    await core.subscriptions[0].join()
    await dispatcher.drain()

    await agent.cancel(session_id)
    response = await prompt

    assert response.stop_reason == "cancelled"
    assert _types(client) == [
        "user_message_chunk",
        "tool_call",
        "tool_call_update",
    ]
    assert client.updates[1].raw_input == {"path": "a", "token": "[redacted]"}
    assert client.updates[2].tool_call_id == "accepted-tool"
    assert client.updates[2].status == "failed"
    assert client.updates[2].raw_output == {"error": "Tool execution cancelled"}
    await agent.cancel(session_id)
    assert agent._audit_events[-1] == {
        "event": "acp_cancel_noop",
        "session_id": session_id,
    }


async def test_journal_boundary_linearizes_prepared_delivery_sent_and_close(tmp_path: Path) -> None:
    agent, _, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    record = agent._sessions[session_id].record
    journal = agent._journals.open(record)

    class GatedClient(Client):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def session_update(self, session_id: str, update: Any) -> None:
            self.entered.set()
            await self.release.wait()
            await super().session_update(session_id, update)

    client = GatedClient()
    lease = JournalLease("00000000-0000-4000-8000-000000000001", 1, 1)
    update = sdk.ToolCallStart(
        sessionUpdate="tool_call", toolCallId="tool", title="read",
        kind="other", status="pending", rawInput={"path": "a"},
    )
    publishing = asyncio.create_task(
        journal.publish_live(update, client, turn_id=lease.turn_id, lease=lease)
    )
    await client.entered.wait()
    records = [json.loads(line) for line in record.journal_path.read_text().splitlines()]
    assert [item["kind"] for item in records] == ["prepared"]

    close_attempted = asyncio.Event()

    async def close_boundary() -> bool:
        close_attempted.set()
        return await lease.close_boundary(journal.lock)

    closing = asyncio.create_task(close_boundary())
    await close_attempted.wait()
    assert closing.done() is False
    client.release.set()
    await publishing
    assert await closing is True
    records = [json.loads(line) for line in record.journal_path.read_text().splitlines()]
    assert [item["kind"] for item in records] == ["prepared", "sent"]
    assert await journal.publish_live(update, client, turn_id=lease.turn_id, lease=lease) is None
    assert len(client.updates) == 1


async def test_cancel_timeout_dirties_execution_and_requires_fresh_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/one")).session_id
    state = agent._sessions[session_id]
    resisted = asyncio.Event()
    release = asyncio.Event()

    started = asyncio.Event()

    async def resistant_model() -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            resisted.set()
            await release.wait()

    async def pending_handler() -> None:
        await asyncio.Future()

    model = asyncio.create_task(resistant_model())
    handler = asyncio.create_task(pending_handler())
    forwarder = asyncio.create_task(pending_handler())
    await started.wait()
    lease = JournalLease("00000000-0000-4000-8000-000000000002", state.generation, 1)
    publisher = SimpleNamespace(_journal=SimpleNamespace(lock=asyncio.Lock()))
    dispatcher = UpdateDispatcher(publisher, lease, 1)
    active = ActivePrompt(state, state.generation, 1, handler, model, forwarder, dispatcher, lease)
    state.active_prompt = active
    agent._active_prompts[session_id] = active
    monkeypatch.setattr(agent_module, "ACP_PROMPT_CANCEL_GRACE_SECONDS", 0.01)

    await agent._cancel_active(active, transport=False)
    await resisted.wait()
    assert state.dirty is True
    assert agent._sessions[session_id] is state
    assert session_id not in agent._environments
    assert agent._execution_keys[session_id] == 1
    with pytest.raises(sdk.RequestError):
        await agent.prompt(session_id, [])

    release.set()
    await model
    for task in (handler, forwarder):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_idle_and_repeated_cancel_are_structured_owned_noops(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent, _, _ = await _ready(tmp_path)
    with caplog.at_level("INFO", logger="mimir.acp.agent"):
        await agent.cancel("missing")
        await agent.cancel("missing")
    assert [record.message for record in caplog.records] == ["acp_cancel_noop", "acp_cancel_noop"]
    assert all(record.acp_audit["session_id"] == "missing" for record in caplog.records)
    assert agent._audit_events == [
        {"event": "acp_cancel_noop", "session_id": "missing"},
        {"event": "acp_cancel_noop", "session_id": "missing"},
    ]


async def test_provider_connect_and_discovery_failures_leave_no_durable_session(
    tmp_path: Path,
) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class FailingClient(McpClient):
        def __init__(self, fail_connect: bool) -> None:
            super().__init__()
            self.fail_connect = fail_connect

        async def connect_mcp(self, server_id: str) -> str:
            if self.fail_connect:
                raise RuntimeError("connect failed")
            return await super().connect_mcp(server_id)

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            if method == "tools/list":
                return {"tools": []}
            return await super().message_mcp(connection_id, method, params)

    for fail_connect in (True, False):
        client = FailingClient(fail_connect)
        agent.on_connect(client)
        await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
        with pytest.raises(sdk.RequestError):
            await agent.new_session("/one", mcp_servers=_hands(f"server-{fail_connect}"))
        assert agent._sessions == {}
        assert agent._connection.server_sessions == {}
        assert agent._connection.connection_sessions == {}
        assert list(agent._store.root.iterdir()) == []
        if not fail_connect:
            assert client.disconnects == ["connection-1"]


async def test_failed_load_readmission_preserves_prior_provider_binding(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class ReloadClient(McpClient):
        fail_discovery = False

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            if self.fail_discovery and method == "tools/list":
                return {"tools": []}
            return await super().message_mcp(connection_id, method, params)

    client = ReloadClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server"))).session_id
    prior = agent._sessions[session_id]
    prior_provider = prior.provider
    client.fail_discovery = True

    with pytest.raises(sdk.RequestError):
        await agent.load_session("/two", session_id, mcp_servers=_hands("server"))

    assert agent._sessions[session_id] is prior
    assert agent._connection.server_sessions["server"] is prior
    assert prior_provider is not None and prior_provider.closed is False
    assert agent._connection.connection_sessions[prior_provider.connection_id] is prior
    assert client.disconnects == ["connection-2"]


async def test_real_resistant_provider_cancel_requires_authenticated_load_with_fresh_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, core = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class ResistantClient(McpClient):
        def __init__(self) -> None:
            super().__init__()
            self.call_started = asyncio.Event()
            self.call_release = asyncio.Event()

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            if method == "tools/call":
                self.call_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await self.call_release.wait()
                    raise
            return await super().message_mcp(connection_id, method, params)

    client = ResistantClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server"))).session_id
    core.call_provider = True
    monkeypatch.setattr(agent_module, "ACP_PROMPT_CANCEL_GRACE_SECONDS", 0.01)
    prompt = asyncio.create_task(
        agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="resist")])
    )
    await client.call_started.wait()

    await agent.cancel(session_id)

    state = agent._sessions[session_id]
    assert state.dirty is True
    assert state.provider is None
    assert session_id not in agent._environments
    with pytest.raises(sdk.RequestError):
        await agent.prompt(session_id, [])
    client.call_release.set()
    core.cancel_release.set()
    assert (await prompt).stop_reason == "cancelled"
    with pytest.raises(sdk.RequestError):
        await agent.authenticate("mimir-web-key", **{"mimir.webKey": "stale"})
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    await agent.load_session("/rebuilt", session_id, mcp_servers=_hands("server"))
    rebuilt = agent._sessions[session_id]
    assert rebuilt.dirty is False
    assert rebuilt.execution_session_key > state.execution_session_key
    assert rebuilt.provider is not None
    assert rebuilt.provider.connection_id == "connection-2"
    assert session_id in agent._environments


async def test_failed_replay_restores_all_prior_provider_indexes(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class ReplayFailClient(McpClient):
        fail_replay = False

        async def session_update(self, session_id: str, update: Any) -> None:
            if self.fail_replay:
                raise RuntimeError("send failed")
            await super().session_update(session_id, update)

    client = ReplayFailClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server"))).session_id
    await agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="persist")])
    prior = agent._sessions[session_id]
    prior_provider = prior.provider
    client.fail_replay = True

    with pytest.raises(sdk.RequestError):
        await agent.load_session("/two", session_id, mcp_servers=_hands("server"))

    assert agent._sessions[session_id] is prior
    assert agent._connection.bound_sessions == {session_id}
    assert agent._connection.server_sessions == {"server": prior}
    assert prior_provider is not None
    assert agent._connection.connection_sessions == {prior_provider.connection_id: prior}
    assert agent._connection.used_connection_ids == {"connection-1", "connection-2"}


async def test_discovery_requires_exact_unique_rows_and_retains_used_ids(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class DuplicateClient(McpClient):
        async def connect_mcp(self, server_id: str) -> str:
            self.connects.append(server_id)
            return "fixed-connection"

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            result = await super().message_mcp(connection_id, method, params)
            if method == "tools/list":
                result = {"tools": [result["tools"][0], result["tools"][0], result["tools"][2]]}
            return result

    client = DuplicateClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    for _ in range(2):
        with pytest.raises(sdk.RequestError):
            await agent.new_session("/one", mcp_servers=_hands("server"))
        assert agent._connection.bound_sessions == set()
        assert agent._connection.server_sessions == {}
        assert agent._connection.connection_sessions == {}
    assert agent._connection.used_connection_ids == {"fixed-connection"}


async def test_journal_send_and_fsync_failures_preserve_prepared_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = await _ready(tmp_path)
    first_id = (await agent.new_session("/send")).session_id
    first_record = agent._sessions[first_id].record
    first_journal = agent._journals.open(first_record)

    class SendFailure(Client):
        async def session_update(self, session_id: str, update: Any) -> None:
            raise RuntimeError("send failed")

    lease = JournalLease("00000000-0000-4000-8000-000000000010", 1, 1)
    update = sdk.ToolCallStart(
        sessionUpdate="tool_call", toolCallId="one", title="read",
        kind="other", status="pending", rawInput={"path": "a"},
    )
    with pytest.raises(RuntimeError, match="send failed"):
        await first_journal.publish_live(update, SendFailure(), turn_id=lease.turn_id, lease=lease)
    records = [json.loads(line) for line in first_record.journal_path.read_text().splitlines()]
    assert [item["kind"] for item in records] == ["prepared"]

    second_id = (await agent.new_session("/fsync")).session_id
    second_record = agent._sessions[second_id].record
    second_journal = agent._journals.open(second_record)
    monkeypatch.setattr(second_journal, "_append_durable", lambda payload: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(sdk.RequestError):
        await second_journal.publish_live(update, Client(), turn_id=lease.turn_id, lease=JournalLease(lease.turn_id, 1, 1))
    metadata = json.loads(second_record.metadata_path.read_text())
    assert metadata["replayability"] == "io_failed"


async def test_journal_multi_event_cancel_terminals_are_durable_and_ordered(tmp_path: Path) -> None:
    agent, client, _ = await _ready(tmp_path)
    session_id = (await agent.new_session("/ordered")).session_id
    record = agent._sessions[session_id].record
    journal = agent._journals.open(record, client)
    lease = JournalLease("00000000-0000-4000-8000-000000000011", 1, 1)
    publisher = agent_module._TurnPublisher(journal, client, lease)
    dispatcher = UpdateDispatcher(publisher, lease, 1)
    for tool_id in ("one", "two"):
        dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": tool_id, "tool_name": "hands_edit", "args": {"path": tool_id}})
    await dispatcher.drain()
    await lease.close_boundary(journal.lock)
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "late", "tool_name": "hands_edit", "args": {"path": "late"}})
    await dispatcher.terminalize_cancelled()
    await dispatcher.close()

    records = [json.loads(line) for line in record.journal_path.read_text().splitlines()]
    prepared = [item for item in records if item["kind"] == "prepared"]
    assert [item["update"]["toolCallId"] for item in prepared] == ["one", "two", "one", "two"]
    assert [item["update"]["status"] for item in prepared[-2:]] == ["failed", "failed"]
    assert [item["sequence"] for item in records] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert all(item["update"]["rawOutput"] == {"error": "Tool execution cancelled"} for item in prepared[-2:])


async def test_cancel_boundary_prevents_post_boundary_model_registration(tmp_path: Path) -> None:
    bundle, core = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class FirstSendGate(McpClient):
        def __init__(self) -> None:
            super().__init__()
            self.send_entered = asyncio.Event()
            self.send_release = asyncio.Event()
            self.gated = False

        async def session_update(self, session_id: str, update: Any) -> None:
            if not self.gated:
                self.gated = True
                self.send_entered.set()
                await self.send_release.wait()
            await super().session_update(session_id, update)

    client = FirstSendGate()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one")).session_id
    prompt = asyncio.create_task(
        agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="cancel")])
    )
    await client.send_entered.wait()
    cancelling = asyncio.create_task(agent.cancel(session_id))
    await asyncio.sleep(0)
    client.send_release.set()

    await cancelling
    assert (await prompt).stop_reason == "cancelled"
    assert core.calls == []


async def test_cancel_boundary_prevents_permission_and_mcp_registration(tmp_path: Path) -> None:
    bundle, core = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)

    class TrackingClient(McpClient):
        def __init__(self) -> None:
            super().__init__()
            self.permission_requests = 0
            self.tool_requests = 0

        async def request_tool_permission(self, session_id: str, snapshot: Any) -> Any:
            self.permission_requests += 1
            await asyncio.Future()

        async def message_mcp(self, connection_id: str, method: str, params: Any = None) -> Any:
            if method == "tools/call":
                self.tool_requests += 1
                await asyncio.Future()
            return await super().message_mcp(connection_id, method, params)

    client = TrackingClient()
    agent.on_connect(client)
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
    session_id = (await agent.new_session("/one", mcp_servers=_hands("server"))).session_id
    core.gate = asyncio.Event()
    prompt = asyncio.create_task(
        agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="race")])
    )
    await core.entered.wait()
    active = agent._active_prompts[session_id]
    active.dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "tool", "tool_name": "hands_edit", "args": {"path": "a"}})
    await active.dispatcher.drain()
    provider = agent._sessions[session_id].provider
    assert provider is not None

    await agent._boundary_lock.acquire()
    cancelling = asyncio.create_task(agent.cancel(session_id))
    await asyncio.sleep(0)
    permission = asyncio.create_task(
        active.request_permission(PermissionEligibility("tool", "ignored", "ignored", {"path": "a"}))
    )
    mcp = asyncio.create_task(provider.call_tool("edit", {"path": "a", "oldText": "x", "newText": "y"}))
    await asyncio.sleep(0)
    agent._boundary_lock.release()

    await cancelling
    assert await permission is PermissionDecision.REJECT_ONCE
    with pytest.raises(RuntimeError, match="closed"):
        await mcp
    assert (await prompt).stop_reason == "cancelled"
    assert client.permission_requests == 0
    assert client.tool_requests == 0


@pytest.mark.asyncio
async def test_integrated_hands_edit_permission_wire_and_provider_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise once-only permission and terminal updates over the public wire."""
    from langchain.agents.middleware import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langgraph.runtime import Runtime

    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir.tools.client_provider import hands_edit

    class WireTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def receive(self) -> dict[str, Any] | None:
            return await self.incoming.get()

        async def send(self, message: dict[str, Any]) -> None:
            sdk.validate_jsonrpc_envelope(message)
            await self.outgoing.put(message)

        async def close(self) -> None:
            return None

    bundle, core = _bundle(tmp_path)
    agent = MimirAcpAgent(bundle)
    transport = WireTransport()
    holder: dict[str, sdk.AcpPeer] = {}

    async def route(method: str, params: Any, is_notification: bool) -> Any:
        assert method == sdk.MCP_MESSAGE_METHOD
        return await holder["peer"].route_mcp(params, is_notification)

    connection = sdk.Connection(
        route, transport, listening=False,
        state_store=sdk.StrictMessageStateStore(),
    )
    runner = asyncio.create_task(connection.main_loop())
    peer = sdk.AcpPeer(connection, agent)
    holder["peer"] = peer
    peer.peer_generation = agent.on_connect(peer)

    async def next_outgoing() -> dict[str, Any]:
        return await asyncio.wait_for(transport.outgoing.get(), 3)

    owned_tasks: list[asyncio.Task[Any]] = []
    try:
        # Authentication itself refuses a real resolver-backed non-admin before
        # any session/provider authority can be established.
        identities_path = tmp_path / "state" / "identities.yaml"
        identities = yaml.safe_load(identities_path.read_text(encoding="utf-8"))
        identities["people"].append({
            "canonical": "viewer", "display_name": "Viewer",
            "aliases": [hash_web_key("viewer-secret")],
            "access": {"roles": ["user"], "is_service": False},
        })
        identities_path.write_text(yaml.safe_dump(identities), encoding="utf-8")
        bundle.core.identity_resolver.reload()
        with pytest.raises(sdk.RequestError):
            await agent.authenticate(
                "mimir-web-key", **{"mimir.webKey": "viewer-secret"}
            )
        assert agent._connection.auth_context is None

        await agent.authenticate("mimir-web-key", **{"mimir.webKey": "secret"})
        creating = asyncio.create_task(
            agent.new_session("/untrusted-cwd", mcp_servers=_hands("hands-server"))
        )
        owned_tasks.append(creating)
        assert await next_outgoing() == {
            "jsonrpc": "2.0", "id": 0, "method": "mcp/connect",
            "params": {"serverId": "hands-server"},
        }
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 0, "result": {"connectionId": "opaque-1"},
        })
        assert await next_outgoing() == {
            "jsonrpc": "2.0", "id": 1, "method": "mcp/message",
            "params": {
                "connectionId": "opaque-1", "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "mimir", "version": "0.7.4"},
                },
            },
        }
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "hands", "version": "1"},
            },
        })
        assert await next_outgoing() == {
            "jsonrpc": "2.0", "method": "mcp/message",
            "params": {"connectionId": "opaque-1", "method": "notifications/initialized"},
        }
        assert await next_outgoing() == {
            "jsonrpc": "2.0", "id": 2, "method": "mcp/message",
            "params": {"connectionId": "opaque-1", "method": "tools/list", "params": {}},
        }
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "tools": [
                    {
                        "name": tool.provider_name,
                        "description": tool.description,
                        "inputSchema": _thaw_schema(tool.input_schema),
                        "outputSchema": _thaw_schema(tool.result_schema),
                    }
                    for tool in MIMIR_HANDS_V1.tools
                ]
            },
        })
        session_id = (await creating).session_id

        turns = iter([
            [("edit-1", "notes-1.txt", "old-1", "new-1")],
            [("edit-2", "notes-2.txt", "old-2", "new-2")],
            [("edit-3", "notes-3.txt", "old-3", "new-3")],
        ])

        async def integrated_turn(event: Any, **kwargs: Any) -> None:
            # The request uses exactly the continuation context installed by the
            # authenticated ACP agent; the replacement core must not manufacture
            # or strengthen authority.
            bound_auth = event.continuation_auth_context
            assert bound_auth is not None
            assert bound_auth.canonical_principal == "operator"
            assert "admin" in bound_auth.roles
            assert bound_auth.enforcement_enabled

            # Match CoreAgent.run_turn's bound-session IFC initialization.  The
            # replacement classifies request context but derives every authority
            # field exclusively from the authenticated continuation carrier.
            from mimir.agent import _initialize_ifc_labels
            from mimir.models import InformationFlowState

            labels = _initialize_ifc_labels(
                event,
                event.attachment_names,
                resolver=bundle.core.identity_resolver,
            )
            auth = dataclasses.replace(
                bound_auth,
                ifc_labels=labels,
                ifc_state=InformationFlowState(labels=labels),
                saga_session_id=kwargs["saga_session_id"],
            )
            assert auth.principal == bound_auth.principal
            assert auth.canonical_principal == bound_auth.canonical_principal
            assert auth.roles == bound_auth.roles
            assert auth.enforcement_enabled == bound_auth.enforcement_enabled
            turn_id = kwargs["turn_id"]
            active = agent._active_prompts[session_id]
            queue = bundle.turn_event_bus._exact_turn_subscribers[turn_id]
            seq = 0
            for tool_id, path, old_text, new_text in next(turns):
                arguments = {"path": path, "old_text": old_text, "new_text": new_text}
                seq += 1
                bundle.turn_event_bus.publish({
                    "turn_id": turn_id, "channel_id": event.channel_id, "seq": seq,
                    "ts": "now", "type": "tool_call", "phase": "start",
                    "id": tool_id, "tool_name": "hands_edit", "args": arguments,
                })
                await queue.join()
                await active.dispatcher.drain()
                request = ToolCallRequest(
                    tool_call={
                        "name": "hands_edit", "args": arguments,
                        "id": tool_id, "type": "tool_call",
                    },
                    tool=None, state=None, runtime=Runtime(context=auth),
                )

                async def handler(call: ToolCallRequest) -> ToolMessage:
                    result = await hands_edit.ainvoke(call.tool_call["args"])
                    return ToolMessage(
                        content=json.dumps(result), tool_call_id=tool_id,
                        name="hands_edit",
                    )

                result = await BudgetGateMiddleware().awrap_tool_call(request, handler)
                seq += 1
                # Publish the actual middleware/handler ToolMessage through the
                # same exact-turn event path used by the real core.
                bundle.turn_event_bus.publish({
                    "turn_id": turn_id, "channel_id": event.channel_id, "seq": seq,
                    "ts": "now", "type": "tool_call", "phase": "end",
                    "id": tool_id, "tool_name": "hands_edit", "args": arguments,
                })
                seq += 1
                bundle.turn_event_bus.publish({
                    "turn_id": turn_id, "channel_id": event.channel_id, "seq": seq,
                    "ts": "now", "type": "tool_result", "phase": "end",
                    "id": result.tool_call_id, "tool_name": result.name or "hands_edit",
                    "content": json.loads(str(result.content)) if result.status == "success"
                    else {"error": str(result.content)},
                    "status": result.status, "is_error": result.status == "error",
                })
                await queue.join()
                await active.dispatcher.drain()

        monkeypatch.setattr(core, "run_turn", integrated_turn)

        def permission_request(outer_id: int, tool_id: str, path: str, old: str, new: str) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0", "id": outer_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": tool_id, "title": "hands_edit", "kind": "other",
                        "status": "pending", "rawInput": {
                            "path": path, "old_text": old, "new_text": new,
                        },
                    },
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
                    ],
                },
            }

        def provider_request(outer_id: int, path: str, old: str, new: str, token: str) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0", "id": outer_id, "method": "mcp/message",
                "params": {
                    "connectionId": "opaque-1", "method": "tools/call",
                    "params": {
                        "name": "edit",
                        "arguments": {"path": path, "oldText": old, "newText": new},
                        "_meta": {"progressToken": token},
                    },
                },
            }

        prompting = asyncio.create_task(
            agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="edit twice")])
        )
        owned_tasks.append(prompting)
        user_update = await next_outgoing()
        assert user_update["method"] == "session/update"
        assert user_update["params"]["update"]["sessionUpdate"] == "user_message_chunk"
        start_1 = await next_outgoing()
        start_1_update = start_1["params"]["update"]
        assert start_1_update["_meta"] == {"mimir.sequence": 1}
        assert {key: value for key, value in start_1_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call", "toolCallId": "edit-1",
            "title": "hands_edit", "kind": "other", "status": "pending",
            "rawInput": {"path": "notes-1.txt", "old_text": "old-1", "new_text": "new-1"},
        }
        assert await next_outgoing() == permission_request(
            3, "edit-1", "notes-1.txt", "old-1", "new-1"
        )
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 3,
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
        })
        provider_frame = await next_outgoing()
        progress_token = provider_frame["params"]["params"]["_meta"]["progressToken"]
        assert isinstance(progress_token, str) and len(progress_token) >= 32
        assert provider_frame == provider_request(
            4, "notes-1.txt", "old-1", "new-1", progress_token
        )
        for token, progress in ((progress_token, 1), ("foreign", 2)):
            await transport.incoming.put({
                "jsonrpc": "2.0", "method": "mcp/message", "params": {
                    "connectionId": "opaque-1", "method": "notifications/progress",
                    "params": {"progressToken": token, "progress": progress},
                },
            })
        await transport.incoming.put({
            "jsonrpc": "2.0", "method": "mcp/message", "params": {
                "connectionId": "opaque-1", "method": "notifications/message",
                "params": {"level": "info", "logger": "hands", "data": {"token": "secret", "message": "working"}},
            },
        })
        for _ in range(20):
            if len(agent._audit_events) >= 3:
                break
            await asyncio.sleep(0.01)
        assert agent._audit_events[-3]["status"] == "accepted"
        assert agent._audit_events[-2]["status"] == "ignored"
        assert agent._audit_events[-1]["data"] == {"token": "[redacted]", "message": "working"}
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 4, "result": {"changed": True},
        })
        progress_1 = await next_outgoing()
        await transport.incoming.put({
            "jsonrpc": "2.0", "method": "mcp/message", "params": {
                "connectionId": "opaque-1", "method": "notifications/progress",
                "params": {"progressToken": progress_token, "progress": 3},
            },
        })
        for _ in range(20):
            if len(agent._audit_events) >= 4:
                break
            await asyncio.sleep(0.01)
        assert agent._audit_events[-1]["status"] == "ignored"
        progress_1_update = progress_1["params"]["update"]
        assert progress_1_update["_meta"] == {"mimir.sequence": 2}
        assert {key: value for key, value in progress_1_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call_update", "toolCallId": "edit-1",
            "status": "in_progress",
            "rawInput": {"path": "notes-1.txt", "old_text": "old-1", "new_text": "new-1"},
        }
        terminal_1 = await next_outgoing()
        terminal_1_update = terminal_1["params"]["update"]
        assert terminal_1_update["_meta"] == {"mimir.sequence": 3}
        assert {key: value for key, value in terminal_1_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call_update", "toolCallId": "edit-1",
            "status": "completed", "rawOutput": {"changed": True},
        }
        assert (await asyncio.wait_for(prompting, 3)).stop_reason == "end_turn"

        prompting_2 = asyncio.create_task(
            agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="edit again")])
        )
        owned_tasks.append(prompting_2)
        assert (await next_outgoing())["params"]["update"]["sessionUpdate"] == "user_message_chunk"
        start_2 = await next_outgoing()
        start_2_update = start_2["params"]["update"]
        assert start_2_update["_meta"] == {"mimir.sequence": 5}
        assert {key: value for key, value in start_2_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call", "toolCallId": "edit-2",
            "title": "hands_edit", "kind": "other", "status": "pending",
            "rawInput": {"path": "notes-2.txt", "old_text": "old-2", "new_text": "new-2"},
        }
        assert await next_outgoing() == permission_request(
            5, "edit-2", "notes-2.txt", "old-2", "new-2"
        )
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 5,
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
        })
        provider_frame_2 = await next_outgoing()
        progress_token_2 = provider_frame_2["params"]["params"]["_meta"]["progressToken"]
        assert progress_token_2 != progress_token
        assert provider_frame_2 == provider_request(
            6, "notes-2.txt", "old-2", "new-2", progress_token_2
        )
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 6, "result": {"changed": True},
        })
        progress_2 = await next_outgoing()
        progress_2_update = progress_2["params"]["update"]
        assert progress_2_update["_meta"] == {"mimir.sequence": 6}
        assert {key: value for key, value in progress_2_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call_update", "toolCallId": "edit-2",
            "status": "in_progress",
            "rawInput": {"path": "notes-2.txt", "old_text": "old-2", "new_text": "new-2"},
        }
        terminal_2 = await next_outgoing()
        terminal_2_update = terminal_2["params"]["update"]
        assert terminal_2_update["_meta"] == {"mimir.sequence": 7}
        assert {key: value for key, value in terminal_2_update.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call_update", "toolCallId": "edit-2",
            "status": "completed", "rawOutput": {"changed": True},
        }
        assert (await asyncio.wait_for(prompting_2, 3)).stop_reason == "end_turn"

        rejecting = asyncio.create_task(
            agent.prompt(session_id, [sdk.TextContentBlock(type="text", text="reject it")])
        )
        owned_tasks.append(rejecting)
        assert (await next_outgoing())["params"]["update"]["sessionUpdate"] == "user_message_chunk"
        rejected_start = (await next_outgoing())["params"]["update"]
        assert rejected_start["_meta"] == {"mimir.sequence": 9}
        assert {key: value for key, value in rejected_start.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call", "toolCallId": "edit-3",
            "title": "hands_edit", "kind": "other", "status": "pending",
            "rawInput": {"path": "notes-3.txt", "old_text": "old-3", "new_text": "new-3"},
        }
        assert await next_outgoing() == permission_request(
            7, "edit-3", "notes-3.txt", "old-3", "new-3"
        )
        await transport.incoming.put({
            "jsonrpc": "2.0", "id": 7,
            "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
        })
        rejected_progress = (await next_outgoing())["params"]["update"]
        assert rejected_progress["_meta"] == {"mimir.sequence": 10}
        assert {key: value for key, value in rejected_progress.items() if key != "_meta"} == {
            "sessionUpdate": "tool_call_update", "toolCallId": "edit-3",
            "status": "in_progress",
            "rawInput": {"path": "notes-3.txt", "old_text": "old-3", "new_text": "new-3"},
        }
        rejected_terminal = await next_outgoing()
        rejected_update = rejected_terminal["params"]["update"]
        assert rejected_update["_meta"] == {"mimir.sequence": 11}
        rejected_body = {
            key: value for key, value in rejected_update.items() if key != "_meta"
        }
        assert rejected_body["sessionUpdate"] == "tool_call_update"
        assert rejected_body["toolCallId"] == "edit-3"
        assert rejected_body["status"] == "failed"
        assert "error" in rejected_body["rawOutput"]
        assert (await asyncio.wait_for(rejecting, 3)).stop_reason == "end_turn"
        # A reject consumed only its permission outer ID: there is no tools/call
        # frame, and no stale allow_once decision was retained from either call.
        assert transport.outgoing.empty()

    finally:
        await transport.incoming.put(None)
        await asyncio.wait_for(runner, 3)
        await agent.on_transport_closed(peer.peer_generation)
        await connection.close()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
