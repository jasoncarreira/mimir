from __future__ import annotations

import asyncio
import io
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import mimir.acp.agent as agent_module
from mimir.acp import sdk
from mimir.acp.agent import MimirAcpAgent
from mimir.acp.stdio import _DrainProtocol, _ReservedFrameTransport
from mimir.identities import IdentityResolver, hash_web_key
from mimir.identities_populator import issue_web_key, revoke_web_key


def _resolver(
    home: Path,
    *,
    raw_key: str = "admin-secret",
    canonical: str = "operator",
    display_name: str | None = "Mimir Operator",
    roles: list[str] | None = None,
    is_service: bool = False,
) -> IdentityResolver:
    state = home / "state"
    state.mkdir(exist_ok=True)
    document = {
        "people": [
            {
                "canonical": canonical,
                "display_name": display_name,
                "aliases": [hash_web_key(raw_key)],
                "access": {
                    "roles": ["admin"] if roles is None else roles,
                    "is_service": is_service,
                },
            }
        ]
    }
    (state / "identities.yaml").write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )
    resolver = IdentityResolver(home)
    resolver.reload()
    return resolver


def _agent(resolver: IdentityResolver) -> MimirAcpAgent:
    bundle = SimpleNamespace(core=SimpleNamespace(identity_resolver=resolver))
    return MimirAcpAgent(bundle)


def test_acp_agent_audience_provider_reuses_runtime_identity_resolver(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)

    agent = _agent(resolver)

    assert agent._identity_resolver is resolver
    assert agent._audience_provider.identity_resolver is resolver


async def _agent_with_session(tmp_path: Path) -> tuple[MimirAcpAgent, str, int]:
    agent = _agent(_resolver(tmp_path))
    generation = agent.on_connect(SimpleNamespace())
    await agent.authenticate(
        "mimir-web-key",
        **{"mimir.webKey": "admin-secret"},
    )
    session_id = (await agent.new_session("/workspace")).session_id
    return agent, session_id, generation


def _dump(response: Any) -> dict[str, Any]:
    return response.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_unset=True,
    )


def _error(error: sdk.RequestError) -> dict[str, Any]:
    return error.to_error_obj()


async def _run_requests(
    agent: MimirAcpAgent,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reader = asyncio.StreamReader()
    for request in requests:
        reader.feed_data((json.dumps(request) + "\n").encode())
    output = io.BytesIO()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(output, protocol)
    writer = asyncio.StreamWriter(
        transport,
        protocol,
        None,
        asyncio.get_running_loop(),
    )
    runner = asyncio.create_task(
        sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)
    )
    expected_responses = sum("id" in request for request in requests)
    async with asyncio.timeout(1):
        while len(output.getvalue().splitlines()) < expected_responses:
            await asyncio.sleep(0)
    reader.feed_eof()
    await runner
    return [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.mark.parametrize("requested_version", [0, 1, 2, 65535])
async def test_initialize_is_exact_stable_v1(
    tmp_path: Path,
    requested_version: int,
) -> None:
    agent = _agent(_resolver(tmp_path))

    response = await agent.initialize(
        requested_version,
        client_capabilities=sdk.ClientCapabilities(),
        client_info=sdk.Implementation(
            name="asserted-client",
            title="Asserted Client",
            version="9.9.9",
        ),
        vendorCapability=True,
    )

    assert _dump(response) == {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {
                "image": False,
                "audio": False,
                "embeddedContext": False,
            },
            "mcpCapabilities": {"http": False, "sse": False, "acp": True},
        },
        "authMethods": [
            {
                "id": "mimir-web-key",
                "name": "Mimir web key",
                "description": 'Pass the web key in _meta["mimir.webKey"].',
            }
        ],
        "agentInfo": {
            "name": "mimir",
            "title": "Mimir",
            "version": version("mimir-agent"),
        },
    }


async def test_authenticate_uses_resolved_identity_and_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "never-store-this"
    resolver = _resolver(tmp_path, raw_key=raw_key)
    agent = _agent(resolver)
    captured: dict[str, Any] = {}
    original_factory = agent_module.create_auth_context

    def factory_spy(event: Any, passed_resolver: Any, **kwargs: Any) -> Any:
        captured.update(event=event, resolver=passed_resolver, kwargs=kwargs)
        return original_factory(event, passed_resolver, **kwargs)

    monkeypatch.setattr(agent_module, "create_auth_context", factory_spy)

    response = await agent.authenticate(
        "mimir-web-key",
        **{
            "mimir.webKey": raw_key,
            "principal": "asserted-root",
            "canonicalPrincipal": "asserted-root",
            "roles": ["admin"],
            "is_service": True,
        },
    )

    assert response is not None
    assert _dump(response) == {}
    event = captured["event"]
    assert event.trigger == "user_message"
    assert event.channel_id == "acp:stdio"
    assert event.content == ""
    assert event.author == "operator"
    assert event.author_display == "Mimir Operator"
    assert event.author_id is None
    assert event.source_id is None
    assert event.source == "acp"
    assert event.extra == {
        "channel_visibility": "private",
        "bridge_instance": "acp-stdio",
    }
    assert captured["resolver"] is resolver
    assert captured["kwargs"]["enforce"] is True
    assert captured["kwargs"]["event_ingress"] is None
    assert captured["kwargs"]["audience_provider"] is agent._audience_provider
    assert captured["kwargs"]["cross_platform_pull"] is True
    assert agent._auth_context is not None
    assert agent._auth_context.principal == "operator"
    assert agent._auth_context.canonical_principal == "operator"
    assert agent._auth_context.roles == ("admin",)
    assert agent._auth_context.trigger == "user_message"
    assert agent._auth_context.event_ingress is None
    assert agent._auth_context.enforcement_enabled is True
    assert agent._auth_context.is_service is False
    assert raw_key not in repr(agent.__dict__)
    assert raw_key not in (tmp_path / "state" / "identities.yaml").read_text()


async def test_authenticate_accepts_key_issued_after_agent_construction(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text("people: []\n", encoding="utf-8")
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    agent = _agent(resolver)

    raw_key = issue_web_key(tmp_path, "operator", roles=["admin"])

    response = await agent.authenticate(
        "mimir-web-key", **{"mimir.webKey": raw_key}
    )
    assert response is not None
    assert agent._auth_context is not None
    assert agent._auth_context.canonical_principal == "operator"


async def test_authenticate_rejects_key_revoked_after_agent_construction(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)
    agent = _agent(resolver)
    await agent.authenticate(
        "mimir-web-key", **{"mimir.webKey": "admin-secret"}
    )

    assert revoke_web_key(tmp_path, "operator") is True

    with pytest.raises(sdk.RequestError) as raised:
        await agent.authenticate(
            "mimir-web-key", **{"mimir.webKey": "admin-secret"}
        )
    assert _error(raised.value) == _error(sdk.auth_required_error())
    assert agent._auth_context is None


async def test_authenticate_fails_closed_once_for_corrupt_identity_reload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver(tmp_path)
    agent = _agent(resolver)
    identities_path = tmp_path / "state" / "identities.yaml"
    identities_path.write_text("people:\n  - canonical: 'broken\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="mimir.identities"):
        for _ in range(2):
            with pytest.raises(sdk.RequestError) as raised:
                await agent.authenticate(
                    "mimir-web-key", **{"mimir.webKey": "admin-secret"}
                )
            assert _error(raised.value) == _error(sdk.auth_required_error())

    assert sum("parse failed" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize(
    ("method_id", "provided_key", "roles", "is_service"),
    [
        ("other-method", "credential", ["admin"], False),
        ("mimir-web-key", None, ["admin"], False),
        ("mimir-web-key", "", ["admin"], False),
        ("mimir-web-key", "unknown", ["admin"], False),
        ("mimir-web-key", "credential", [], False),
        ("mimir-web-key", "credential", ["user"], False),
        ("mimir-web-key", "credential", ["admin"], True),
    ],
)
async def test_authentication_failures_are_context_free_and_generic(
    tmp_path: Path,
    method_id: str,
    provided_key: str | None,
    roles: list[str],
    is_service: bool,
) -> None:
    resolver = _resolver(
        tmp_path,
        raw_key="credential",
        roles=roles,
        is_service=is_service,
    )
    agent = _agent(resolver)
    if roles == ["admin"] and not is_service:
        await agent.authenticate(
            "mimir-web-key",
            **{"mimir.webKey": "credential"},
        )
    kwargs = {} if provided_key is None else {"mimir.webKey": provided_key}

    with pytest.raises(sdk.RequestError) as raised:
        await agent.authenticate(method_id, **kwargs)

    assert _error(raised.value) == {
        "code": -32000,
        "message": "Authentication required",
        "data": {"methodId": "mimir-web-key"},
    }
    assert agent._auth_context is None


async def test_resolver_and_factory_failures_do_not_escape_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _resolver(tmp_path)
    agent = _agent(resolver)

    def resolver_failure(raw_key: str) -> Any:
        raise RuntimeError(f"resolver leaked {raw_key}")

    monkeypatch.setattr(resolver, "resolve_web_key", resolver_failure)
    with pytest.raises(sdk.RequestError) as resolver_error:
        await agent.authenticate(
            "mimir-web-key",
            **{"mimir.webKey": "resolver-secret"},
        )
    assert _error(resolver_error.value) == _error(sdk.auth_required_error())
    assert "resolver-secret" not in str(resolver_error.value)
    assert agent._auth_context is None

    resolver = _resolver(tmp_path, raw_key="factory-secret")
    agent = _agent(resolver)

    def factory_failure(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("factory identity detail")

    monkeypatch.setattr(agent_module, "create_auth_context", factory_failure)
    with pytest.raises(sdk.RequestError) as factory_error:
        await agent.authenticate(
            "mimir-web-key",
            **{"mimir.webKey": "factory-secret"},
        )
    assert _error(factory_error.value) == _error(sdk.auth_required_error())
    assert "identity detail" not in str(factory_error.value)
    assert agent._auth_context is None


async def test_factory_output_is_revalidated_before_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(_resolver(tmp_path))
    forged = SimpleNamespace(
        principal="operator",
        canonical_principal="asserted-root",
        roles=("admin",),
        is_service=False,
    )
    monkeypatch.setattr(
        agent_module,
        "create_auth_context",
        lambda *args, **kwargs: forged,
    )

    with pytest.raises(sdk.RequestError) as raised:
        await agent.authenticate(
            "mimir-web-key",
            **{"mimir.webKey": "admin-secret"},
        )

    assert _error(raised.value) == _error(sdk.auth_required_error())
    assert agent._auth_context is None


@pytest.mark.parametrize(
    "handler",
    [
        lambda agent: agent.new_session("/tmp", mcp_servers=object()),
        lambda agent: agent.load_session("/tmp", "session-id", mcp_servers=object()),
        lambda agent: agent.prompt("session-id", []),
    ],
)
async def test_stateful_methods_require_authentication_and_connection(
    tmp_path: Path,
    handler: Any,
) -> None:
    agent = _agent(_resolver(tmp_path))

    with pytest.raises(sdk.RequestError) as pre_auth:
        await handler(agent)
    assert _error(pre_auth.value) == _error(sdk.auth_required_error())

    await agent.authenticate(
        "mimir-web-key",
        **{"mimir.webKey": "admin-secret"},
    )

    with pytest.raises(sdk.RequestError) as post_auth:
        await handler(agent)
    assert _error(post_auth.value) == _error(sdk.internal_error())


async def test_cancel_from_unauthenticated_peer_leaves_existing_session_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent, session_id, _ = await _agent_with_session(tmp_path)
    state = agent._sessions[session_id]
    active = SimpleNamespace(cancelling=False)
    state.active_prompt = active
    calls: list[Any] = []

    async def cancel_active(candidate: Any, *, transport: bool) -> bool:
        calls.append((candidate, transport))
        return True

    monkeypatch.setattr(agent, "_cancel_active", cancel_active)
    agent.on_connect(SimpleNamespace())

    with caplog.at_level("INFO", logger="mimir.acp.agent"):
        assert await agent.cancel(session_id) is None

    assert calls == []
    assert agent._sessions[session_id] is state
    assert state.active_prompt is active
    assert active.cancelling is False
    assert caplog.records[-1].acp_audit == {
        "event": "acp_cancel_noop",
        "session_id": session_id,
        "reason": "unauthenticated",
    }


async def test_cancel_from_demoted_connection_leaves_successor_prompt_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent, session_id, first_generation = await _agent_with_session(tmp_path)
    agent.on_connect(SimpleNamespace())
    await agent.authenticate(
        "mimir-web-key",
        **{"mimir.webKey": "admin-secret"},
    )
    await agent.load_session("/successor", session_id)
    state = agent._sessions[session_id]
    running_prompt = asyncio.create_task(asyncio.Event().wait())
    active = SimpleNamespace(cancelling=False, model_task=running_prompt)
    state.active_prompt = active
    calls: list[Any] = []

    async def cancel_active(candidate: Any, *, transport: bool) -> bool:
        calls.append((candidate, transport))
        return True

    monkeypatch.setattr(agent, "_cancel_active", cancel_active)
    token = agent._connection_generation.set(first_generation)
    try:
        with caplog.at_level("INFO", logger="mimir.acp.agent"):
            assert await agent.cancel(session_id) is None
    finally:
        agent._connection_generation.reset(token)
        for task in agent._retirement_tasks:
            task.cancel()
        await asyncio.gather(*agent._retirement_tasks, return_exceptions=True)

    assert calls == []
    assert agent._sessions[session_id] is state
    assert state.active_prompt is active
    assert active.cancelling is False
    assert running_prompt.done() is False
    assert caplog.records[-1].acp_audit == {
        "event": "acp_cancel_noop",
        "session_id": session_id,
        "reason": "not_current_connection",
    }
    running_prompt.cancel()
    await asyncio.gather(running_prompt, return_exceptions=True)


async def test_cancel_rejects_session_from_another_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent, session_id, generation = await _agent_with_session(tmp_path)
    state = agent._sessions[session_id]
    state.generation = generation + 1
    active = SimpleNamespace(cancelling=False)
    state.active_prompt = active
    calls: list[Any] = []

    async def cancel_active(candidate: Any, *, transport: bool) -> bool:
        calls.append((candidate, transport))
        return True

    monkeypatch.setattr(agent, "_cancel_active", cancel_active)
    with caplog.at_level("INFO", logger="mimir.acp.agent"):
        assert await agent.cancel(session_id) is None

    assert calls == []
    assert state.active_prompt is active
    assert active.cancelling is False
    assert caplog.records[-1].acp_audit["reason"] == "session_generation_mismatch"


async def test_cancel_rejects_session_owned_by_another_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent, session_id, _ = await _agent_with_session(tmp_path)
    state = agent._sessions[session_id]
    state.record = SimpleNamespace(
        session_id=session_id,
        owner_principal="another-operator",
    )
    active = SimpleNamespace(cancelling=False)
    state.active_prompt = active
    calls: list[Any] = []

    async def cancel_active(candidate: Any, *, transport: bool) -> bool:
        calls.append((candidate, transport))
        return True

    monkeypatch.setattr(agent, "_cancel_active", cancel_active)
    with caplog.at_level("INFO", logger="mimir.acp.agent"):
        assert await agent.cancel(session_id) is None

    assert calls == []
    assert state.active_prompt is active
    assert active.cancelling is False
    assert caplog.records[-1].acp_audit["reason"] == "session_owner_mismatch"


async def test_unknown_extension_request_and_notification_behavior(
    tmp_path: Path,
) -> None:
    agent = _agent(_resolver(tmp_path))

    with pytest.raises(sdk.RequestError) as raised:
        await agent.ext_method("example", {"authority": "asserted"})
    assert _error(raised.value) == {
        "code": -32601,
        "message": "Method not found",
        "data": {"method": "_example"},
    }
    assert await agent.ext_notification("notice", {"value": 1}) is None


async def test_actual_router_uses_only_meta_key_and_emits_no_notification_response(
    tmp_path: Path,
) -> None:
    agent = _agent(_resolver(tmp_path, raw_key="wire-secret"))

    responses = await _run_requests(
        agent,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "authenticate",
                "params": {
                    "methodId": "mimir-web-key",
                    "mimir.webKey": "not-meta",
                    "_meta": {
                        "mimir.webKey": "wire-secret",
                        "principal": "asserted-root",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/tmp", "mcpServers": []},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "_example",
                "params": {"value": 7},
            },
            {
                "jsonrpc": "2.0",
                "method": "_notice",
                "params": {"value": 8},
            },
        ],
    )

    by_id = {response["id"]: response for response in responses}
    assert by_id[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert by_id[2]["jsonrpc"] == "2.0"
    assert set(by_id[2]["result"]) == {"sessionId"}
    assert by_id[3] == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {
            "code": -32601,
            "message": "Method not found",
            "data": {"method": "_example"},
        },
    }
    assert agent._auth_context is None


def test_agent_exposes_no_out_of_scope_handlers(tmp_path: Path) -> None:
    agent = _agent(_resolver(tmp_path))
    forbidden = {
        "list_sessions",
        "fork_session",
        "resume_session",
        "close_session",
        "set_session_mode",
        "set_config_option",
        "session_update",
        "request_permission",
        "read_text_file",
        "write_text_file",
        "create_terminal",
        "terminal_output",
        "wait_for_terminal_exit",
        "kill_terminal",
        "release_terminal",
    }

    assert all(not hasattr(agent, name) for name in forbidden)


async def test_candidate_activates_only_after_admin_authentication(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    agent = _agent(resolver)
    first = SimpleNamespace()
    first_generation = agent.on_connect(first)
    with pytest.raises(sdk.RequestError) as unauthenticated_first:
        await agent.new_session("/one")
    assert _error(unauthenticated_first.value) == _error(sdk.auth_required_error())
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "admin-secret"})
    assert agent._connections[first_generation].principal == "operator"
    assert agent._connection is agent._connections[first_generation]

    second = SimpleNamespace()
    second_generation = agent.on_connect(second)
    assert agent._connection is agent._connections[first_generation]
    assert agent._generation == first_generation
    with pytest.raises(sdk.RequestError) as invalid_candidate:
        await agent.authenticate("mimir-web-key", **{"mimir.webKey": "invalid"})
    assert _error(invalid_candidate.value) == _error(sdk.auth_required_error())
    assert agent._connection is agent._connections[first_generation]
    assert agent._auth_context is not None
    assert agent._auth_context.canonical_principal == "operator"
    assert agent._connections[second_generation].auth_context is None

    await agent.on_transport_closed(second_generation)
    assert agent._connection is agent._connections[first_generation]
    assert agent._generation == first_generation
    assert agent._auth_context is not None

    replacement_generation = agent.on_connect(SimpleNamespace())
    await agent.authenticate("mimir-web-key", **{"mimir.webKey": "admin-secret"})
    assert agent._connection is agent._connections[replacement_generation]
    assert agent._generation == replacement_generation


async def test_overlapping_connections_keep_authentication_and_dispatch_physical(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path, raw_key="first-secret")
    identities_path = tmp_path / "state" / "identities.yaml"
    identities = yaml.safe_load(identities_path.read_text(encoding="utf-8"))
    identities["people"].append(
        {
            "canonical": "second-operator",
            "display_name": "Second Operator",
            "aliases": [hash_web_key("second-secret")],
            "access": {"roles": ["admin"], "is_service": False},
        }
    )
    identities_path.write_text(yaml.safe_dump(identities), encoding="utf-8")
    resolver.reload()
    agent = _agent(resolver)

    async def physical_connection(
        peer: object,
        ready: asyncio.Future[int],
        commands: asyncio.Queue[tuple[Any, tuple[Any, ...], dict[str, Any], asyncio.Future[Any]] | None],
    ) -> None:
        ready.set_result(agent.on_connect(peer))
        while (command := await commands.get()) is not None:
            operation, args, kwargs, result = command
            try:
                result.set_result(await operation(*args, **kwargs))
            except BaseException as exc:
                result.set_exception(exc)

    loop = asyncio.get_running_loop()
    first_ready: asyncio.Future[int] = loop.create_future()
    second_ready: asyncio.Future[int] = loop.create_future()
    first_commands: asyncio.Queue[Any] = asyncio.Queue()
    second_commands: asyncio.Queue[Any] = asyncio.Queue()
    first_task = asyncio.create_task(
        physical_connection(SimpleNamespace(), first_ready, first_commands)
    )
    second_task = asyncio.create_task(
        physical_connection(SimpleNamespace(), second_ready, second_commands)
    )
    first_generation, second_generation = await asyncio.gather(
        first_ready, second_ready
    )

    async def invoke(
        commands: asyncio.Queue[Any], operation: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = loop.create_future()
        await commands.put((operation, args, kwargs, result))
        return await result

    try:
        await invoke(
            first_commands,
            agent.authenticate,
            "mimir-web-key",
            **{"mimir.webKey": "first-secret"},
        )
        assert agent._connections[first_generation].principal == "operator"
        assert agent._connections[second_generation].principal is None
        assert agent._connection is agent._connections[first_generation]

        created = await invoke(first_commands, agent.new_session, "/active")
        assert created.session_id
        with pytest.raises(sdk.RequestError):
            await invoke(second_commands, agent.new_session, "/candidate")
        with pytest.raises(sdk.RequestError):
            await invoke(
                second_commands,
                agent.authenticate,
                "mimir-web-key",
                **{"mimir.webKey": "invalid"},
            )
        assert agent._connection is agent._connections[first_generation]
        assert agent._connections[first_generation].principal == "operator"
        assert agent._connections[second_generation].principal is None
    finally:
        await first_commands.put(None)
        await second_commands.put(None)
        await asyncio.gather(first_task, second_task)
