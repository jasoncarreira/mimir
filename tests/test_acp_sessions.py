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

    async def run_turn(self, event: Any, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))
        turn_id = kwargs["turn_id"]
        common = {"turn_id": turn_id, "channel_id": event.channel_id, "seq": 1, "ts": "now"}
        self.bus.publish({**common, "type": "tool_call", "phase": "start", "id": "tool-1", "tool_name": "lookup"})
        if self.cancel:
            raise asyncio.CancelledError
        self.bus.publish({**common, "type": "tool_call", "phase": "end", "id": "tool-1", "tool_name": "lookup", "args": {"token": "hidden", "query": "x"}})
        self.bus.publish({**common, "type": "tool_result", "phase": "end", "id": "tool-1", "tool_name": "lookup", "content": {"answer": 1}, "status": "ok"})
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
    assert client.updates[3].raw_input == {"token": "[redacted]", "query": "x"}
    event, kwargs = core.calls[0]
    assert event.content == 'hello\n[resource_link]{"name":"guide","type":"resource_link","uri":"file:///guide"}'
    assert event.author == event.author_id == "operator"
    assert event.source_id == kwargs["turn_id"]
    assert kwargs["session_id"] == kwargs["saga_session_id"] == f"acp:{session_id}"
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
    assert agent._bundle.turn_event_bus._exact_turn_subscribers == {}


async def test_additional_directories_are_rejected_before_creation(tmp_path: Path) -> None:
    agent, _, _ = await _ready(tmp_path)

    with pytest.raises(sdk.RequestError) as raised:
        await agent.new_session("/one", additional_directories=["/two"])

    assert raised.value.to_error_obj()["code"] == -32602
    assert list(agent._store.root.glob("*.meta.json")) == []
