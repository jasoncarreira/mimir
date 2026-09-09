from __future__ import annotations

import asyncio
import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from langchain_core.tools import ToolException

from mimir.acp import agent as agent_module
from mimir.acp.hands_contract import HANDS_PROVIDER_TO_WRAPPER, hands_v1_wire_descriptors
from mimir.access_control import (
    CLIENT_FILE_RESOURCE_POLICY,
    ClientFileResourcePolicy,
    OperationDecision,
    ToolFlowDirection,
    canonical_client_file_resource,
    client_file_resource_path,
    get_operation_catalog,
    get_sink_category,
    get_tool_flow_direction,
    get_tool_registry,
    SinkCategory,
)
from mimir.tools.client_provider import (
    HANDS_TOOLS,
    MIMIR_HANDS_V1,
    PROVIDER_PROFILES,
    PermissionEligibility,
    TurnCapabilityContext,
    get_provider_profile,
    hands_edit,
    hands_python,
    hands_read,
    hands_shell,
    hands_request_scope,
    client_authorized_host_execution_metadata,
    issue_client_authorized_host_execution,
    reset_turn_capability_context,
    set_turn_capability_context,
)
from mimir.models import AuthContext
from mimir.tools.registry import all_mimir_tools


class FakeProvider:
    def __init__(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append((name, arguments))
        return self.results[name]


class StockMcpPeer:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response

    async def message_mcp(
        self, connection_id: str, method: str, params: Any = None
    ) -> Mapping[str, Any]:
        assert connection_id == "hands-connection"
        assert method == "tools/call"
        return self.response


def _stock_provider(response: Mapping[str, Any]) -> Any:
    owner = SimpleNamespace(_boundary_lock=asyncio.Lock())
    state = SimpleNamespace(
        active_prompt=None,
        provider=None,
        record=SimpleNamespace(session_id="provider-contract-test"),
    )
    provider = agent_module._AcpProviderConnection(
        owner, StockMcpPeer(response), "hands-connection", state
    )
    active = SimpleNamespace(
        generation=1,
        epoch=1,
        progress_tokens={},
        mcp_handles=[],
        mcp_request_ids=set(),
        mcp_tasks=set(),
    )
    active._is_current = lambda: state.active_prompt is active
    state.active_prompt = active
    state.provider = provider
    return provider


def _context(
    provider: FakeProvider,
    *,
    profile_policy: Any = MIMIR_HANDS_V1,
) -> TurnCapabilityContext:
    return TurnCapabilityContext(
        permission_broker=None,
        provider=provider,
        profile_policy=profile_policy,
        connection_generation=1,
        prompt_epoch=1,
        acp_delivery=True,
        lease=SimpleNamespace(closed=False),
        cwd="/tmp",
    )




def _admin_auth() -> AuthContext:
    return AuthContext(
        principal="acp:admin",
        canonical_principal="admin",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
        enforcement_enabled=True,
    )


async def _invoke_authorized_read(
    provider: FakeProvider,
    path: object,
    *,
    auth_context: AuthContext | None = None,
    profile_policy: Any = MIMIR_HANDS_V1,
) -> dict[str, str]:
    token = set_turn_capability_context(
        _context(provider, profile_policy=profile_policy)
    )
    try:
        authorization = get_tool_registry().authorize_tool(
            "hands_read",
            auth_context,
            enforce=True,
            arguments={"path": path},
        )
        if not authorization.allowed:
            raise ToolException(authorization.reason or "hands_read denied")
        return await hands_read.ainvoke({"path": path})
    finally:
        reset_turn_capability_context(token)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


async def _invoke_wrapper(wrapper_name: str, provider: Any) -> dict[str, Any]:
    token = set_turn_capability_context(_context(provider))
    try:
        if wrapper_name == "hands_read":
            return await hands_read.ainvoke({"path": "notes.txt"})
        if wrapper_name == "hands_edit":
            return await hands_edit.ainvoke({
                "path": "notes.txt", "old_text": "old", "new_text": "new"
            })
        if wrapper_name == "hands_request_scope":
            return await hands_request_scope.ainvoke({"path": ""})
        if wrapper_name == "hands_shell":
            return await hands_shell.ainvoke({"command": "pwd"})
        return await hands_python.ainvoke({"code": "1 + 1"})
    finally:
        reset_turn_capability_context(token)


def test_registry_contains_only_immutable_mimir_hands_profile() -> None:
    assert tuple(PROVIDER_PROFILES) == ("mimir-hands",)
    assert get_provider_profile("mimir-hands") is MIMIR_HANDS_V1
    assert get_provider_profile("unknown") is None
    assert MIMIR_HANDS_V1.profile_id == "mimir.hands.v1"
    assert MIMIR_HANDS_V1.resource_policy is CLIENT_FILE_RESOURCE_POLICY
    with pytest.raises(TypeError):
        PROVIDER_PROFILES["other"] = MIMIR_HANDS_V1
    with pytest.raises(FrozenInstanceError):
        MIMIR_HANDS_V1.adapter = "client-selected"


@pytest.mark.parametrize(
    ("wrapper_name", "auth_changes", "context_changes"),
    [
        ("hands_read", {}, {}),
        ("shell_exec", {}, {}),
        ("hands_edit", {"roles": ("user",)}, {}),
        ("hands_edit", {"is_service": True}, {}),
        ("hands_edit", {}, {"acp_delivery": False}),
        ("hands_edit", {}, {"profile_policy": None}),
        ("hands_edit", {}, {"connection_generation": 2}),
        ("hands_edit", {}, {"prompt_epoch": 2}),
        ("hands_edit", {}, {"lease_closed": True}),
        ("hands_edit", {}, {"provider_closed": True}),
    ],
)
def test_client_authorized_host_execution_marker_exclusions_are_exhaustive(
    wrapper_name: str,
    auth_changes: dict[str, object],
    context_changes: dict[str, object],
) -> None:
    from mimir.acp.journal import JournalLease

    context_changes = dict(context_changes)
    provider = SimpleNamespace(closed=context_changes.pop("provider_closed", False))
    lease = JournalLease("turn", 1, 1)
    if context_changes.pop("lease_closed", False):
        lease.close()
    context = TurnCapabilityContext(
        permission_broker=SimpleNamespace(request_permission=lambda _: None),
        provider=provider,
        profile_policy=MIMIR_HANDS_V1,
        connection_generation=1,
        prompt_epoch=1,
        acp_delivery=True,
        lease=lease,
    )
    context = replace(context, **context_changes)
    auth = replace(_admin_auth(), **auth_changes)
    request = object()
    token = set_turn_capability_context(context)
    try:
        marker = issue_client_authorized_host_execution(
            request_identity=request,
            auth_context_identity=auth,
            wrapper_name=wrapper_name,
            tainted=False,
        )
    finally:
        reset_turn_capability_context(token)

    assert marker is None


def test_client_authorized_host_execution_metadata_is_live_and_fails_closed() -> None:
    from mimir.acp.journal import JournalLease

    class State:
        tainted: object = False

        def current(self, fallback: object) -> object:
            return fallback

        def permission_has_untrusted_active_ingest(self, fallback: object) -> object:
            if isinstance(self.tainted, Exception):
                raise self.tainted
            return self.tainted

    state = State()
    auth = replace(_admin_auth(), ifc_state=state)
    lease = JournalLease("turn", 1, 1)
    context = TurnCapabilityContext(
        permission_broker=SimpleNamespace(request_permission=lambda _: None),
        provider=SimpleNamespace(closed=False),
        profile_policy=MIMIR_HANDS_V1,
        connection_generation=1,
        prompt_epoch=1,
        acp_delivery=True,
        lease=lease,
    )
    token = set_turn_capability_context(context)
    try:
        marker = issue_client_authorized_host_execution(
            request_identity=object(),
            auth_context_identity=auth,
            wrapper_name="hands_edit",
            tainted=False,
        )
        assert client_authorized_host_execution_metadata(marker) == (
            "hands_edit", False,
        )
        state.tainted = True
        assert client_authorized_host_execution_metadata(marker) == (
            "hands_edit", True,
        )
        state.tainted = object()
        assert client_authorized_host_execution_metadata(marker) is None
        state.tainted = RuntimeError("unavailable live taint")
        assert client_authorized_host_execution_metadata(marker) is None
        state.tainted = False
        lease.close()
        assert client_authorized_host_execution_metadata(marker) is None
    finally:
        reset_turn_capability_context(token)


def test_profile_has_exact_provider_schemas_and_server_metadata() -> None:
    tools = {policy.provider_name: policy for policy in MIMIR_HANDS_V1.tools}
    expected = {
        "read": (
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
            "39b714704935190561ed407980480b9a4a0b346b97346e0bff71fb9ace820194",
            "c9fd9a503f520ca7c958067d2db0bc2b1eef6fe5bbae6c7b9356cb921593c49a",
        ),
        "edit": (
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["path", "oldText", "newText"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"changed": {"type": "boolean"}},
                "required": ["changed"],
                "additionalProperties": False,
            },
            "44f2c54b1a8fc6eaebbff775cba08b64d9d6bb81f752e76e2c3181fe941773bb",
            "edf6d1c05ea9a0eb0767bed2015520eb6f3acc9fd0c7615d0ccfc332001820ce",
        ),
        "shell": (
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exitCode": {"type": "integer"},
                },
                "required": ["stdout", "stderr", "exitCode"],
                "additionalProperties": False,
            },
            "efcd767e81a864d776ee5d1d5757f469a0a9719f26d1cc0b3bae611597585186",
            "0cc998ce157fd5a7389d5ea6a2e6a86d20e70d0e1b4db06d8b91e8f17ec51907",
        ),
        "python": (
            {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "value": {"type": "string"},
                    "exception": {"type": "string"},
                    "timedOut": {"type": "boolean"},
                    "kernel": {
                        "type": "string",
                        "enum": ["fresh", "reused", "timed_out", "crashed"],
                    },
                },
                "required": [
                    "ok", "stdout", "stderr", "value", "exception", "timedOut", "kernel"
                ],
                "additionalProperties": False,
            },
            "e5de9e79f2da6956be1f81b55a9f782098f2cdeb3996cd84a3e38f8efcf40e25",
            "5705e1d85e12b89447ad83e52b7bacb5dea92bbe3480c5fad138364a1540303c",
        ),
    }
    expected["request_scope"] = ({'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}, {'type': 'object', 'properties': {'approved': {'type': 'boolean'}, 'paths': {'type': 'array', 'items': {'type': 'string'}}, 'message': {'type': 'string'}}, 'required': ['approved', 'paths', 'message'], 'additionalProperties': False}, '39b714704935190561ed407980480b9a4a0b346b97346e0bff71fb9ace820194', '942370b44a09b40aec41ffa23bd4694e3238f19b136338abd5cb8061095d54f0')
    assert tuple(tools) == tuple(expected)
    for name, (input_schema, result_schema, input_digest, result_digest) in expected.items():
        assert _thaw(tools[name].input_schema) == input_schema
        assert _thaw(tools[name].result_schema) == result_schema
        assert tools[name].input_schema_digest == input_digest
        assert tools[name].result_schema_digest == result_digest
    assert {policy.classification for policy in MIMIR_HANDS_V1.tools} == {
        "resource_scoped", "admin_required"
    }
    assert {policy.wrapper_name: policy.operation for policy in MIMIR_HANDS_V1.tools} == {
        "hands_read": None,
        "hands_edit": "client_authorized_host_execution",
        "hands_shell": "client_authorized_host_execution",
        "hands_python": "client_authorized_host_execution",
        "hands_request_scope": "client_scope_request",
    }


def test_mimir_hands_profile_projects_shared_contract_exactly() -> None:
    projected = [
        {
            "name": policy.provider_name,
            "description": policy.description,
            "inputSchema": _thaw(policy.input_schema),
            "outputSchema": _thaw(policy.result_schema),
        }
        for policy in MIMIR_HANDS_V1.tools
    ]

    assert projected == hands_v1_wire_descriptors()
    assert {
        policy.provider_name: policy.wrapper_name for policy in MIMIR_HANDS_V1.tools
    } == HANDS_PROVIDER_TO_WRAPPER


def test_client_file_identity_is_opaque_canonical_utf8() -> None:
    assert canonical_client_file_resource("relative/../x") == "client-file:relative%2F..%2Fx"
    assert canonical_client_file_resource("/tmp/a b") == "client-file:%2Ftmp%2Fa%20b"
    assert canonical_client_file_resource("é\\file") == "client-file:%C3%A9%5Cfile"
    assert canonical_client_file_resource("") is None
    assert canonical_client_file_resource("bad\x00path") is None
    assert canonical_client_file_resource("\ud800") is None
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("client-file:any%2Fpath")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("client-file:any/path")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("client-file:%gg")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("file:any")


@pytest.mark.parametrize("wrapper", [hands_read, hands_edit])
@pytest.mark.parametrize(("path", "resource"), [
    ("relative/../x", "client-file:%2Ftmp%2Fx"),
    ("/tmp/a b", "client-file:%2Ftmp%2Fa%20b"),
    ("%2f//é\\file", "client-file:%2Ftmp%2F%252f%2F%C3%A9%5Cfile"),
])
@pytest.mark.parametrize("allowed", [True, False])
async def test_eligibility_resource_matches_wrapper_policy(
    wrapper: Any, path: str, resource: str, allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = {"path": path}
    wire_arguments = {"path": client_file_resource_path(resource)}
    if wrapper is hands_edit:
        arguments.update(old_text="old", new_text="new")
        wire_arguments.update(oldText="old", newText="new")
    checked = []

    def allows(self: ClientFileResourcePolicy, candidate: str) -> bool:
        assert self == ClientFileResourcePolicy.for_cwd("/tmp")
        assert self is not CLIENT_FILE_RESOURCE_POLICY
        checked.append(candidate)
        return allowed

    monkeypatch.setattr(ClientFileResourcePolicy, "allows", allows)
    provider = FakeProvider({"read": {"content": "ok"}, "edit": {"changed": True}})
    token = set_turn_capability_context(_context(provider))
    try:
        eligibility = PermissionEligibility("call", wrapper.name, "other", arguments)
        assert eligibility.canonical_client_resource == resource
        if allowed:
            await wrapper.ainvoke(arguments)
        else:
            with pytest.raises(ToolException, match="path is not authorized"):
                await wrapper.ainvoke(arguments)
    finally:
        reset_turn_capability_context(token)
    assert checked == [eligibility.canonical_client_resource]
    assert provider.calls == ([(wrapper.name.removeprefix("hands_"), wire_arguments)] if allowed else [])


@pytest.mark.parametrize("wrapper", [hands_read, hands_edit])
@pytest.mark.parametrize(("path", "absolute"), [
    ("notes.txt", "/tmp/work/notes.txt"),
    ("sub/../notes.txt", "/tmp/work/notes.txt"),
    ("/tmp/work//sub/./notes.txt", "/tmp/work/sub/notes.txt"),
    ("//tmp/work/notes.txt", "/tmp/work/notes.txt"),
    ("../work/notes.txt", "/tmp/work/notes.txt"),
    ("%2e%2e/notes.txt", "/tmp/work/%2e%2e/notes.txt"),
    ("/tmp/work", "/tmp/work"),
    ("../notes.txt", None),
    ("sub/../../notes.txt", None),
    ("/tmp/work/../notes.txt", None),
    ("/tmp/work-sibling/notes.txt", None),
    ("/etc/passwd", None),
])
async def test_file_wrappers_confine_and_dispatch_absolute_paths(
    wrapper: Any, path: str, absolute: str | None,
) -> None:
    provider = FakeProvider({"read": {"content": "ok"}, "edit": {"changed": True}})
    context = replace(_context(provider), cwd="/tmp/work/./")
    arguments = {"path": path}
    if wrapper is hands_edit:
        arguments.update(old_text="old", new_text="new")
    token = set_turn_capability_context(context)
    try:
        if absolute is None:
            with pytest.raises(ToolException, match="cwd boundary '/tmp/work/./'"):
                await wrapper.ainvoke(arguments)
            assert provider.calls == []
        else:
            await wrapper.ainvoke(arguments)
            wire = {"path": absolute}
            if wrapper is hands_edit:
                wire.update(oldText="old", newText="new")
            assert provider.calls == [(wrapper.name.removeprefix("hands_"), wire)]
    finally:
        reset_turn_capability_context(token)


@pytest.mark.parametrize("wrapper", [hands_read, hands_edit])
@pytest.mark.parametrize("cwd", [None, "", "relative", "../tmp", "file:///tmp", 1, "/bad\x00cwd", "/\ud800"])
@pytest.mark.parametrize("path", ["notes.txt", "/tmp/notes.txt"])
async def test_file_wrappers_fail_closed_for_invalid_cwd(
    wrapper: Any, cwd: object, path: str,
) -> None:
    provider = FakeProvider({})
    context = replace(_context(provider), cwd=cwd)
    assert not context.resource_policy.allows(canonical_client_file_resource(path))
    arguments = {"path": path}
    if wrapper is hands_edit:
        arguments.update(old_text="old", new_text="new")
    token = set_turn_capability_context(context)
    try:
        with pytest.raises(ToolException, match="cwd boundary"):
            await wrapper.ainvoke(arguments)
    finally:
        reset_turn_capability_context(token)
    assert provider.calls == []


@pytest.mark.parametrize(("resource", "allowed"), [
    ("client-file:%2ftmp%2f%77ork%2fnotes.txt", True),
    ("client-file:%2Ftmp%2Fwork%2Fsub%2F%2e%2E%2Fnotes.txt", True),
    ("client-file:%2Ftmp%2Fwork%2F%2e%2e%2Fsecret", False),
    ("client-file:%2Ftmp%2Fwork-sibling%2Fsecret", False),
    ("client-file:%2Ftmp%2Fwork%2F%252e%252e%2Fnotes.txt", True),
    ("client-file:notes.txt", False),
    ("client-file:%2Ftmp%2Fwork%2F%00", False),
    ("client-file:%2Ftmp%2Fwork%2F%ff", False),
    ("client-file:%2Ftmp%2Fwork%2F%gg", False),
    ("client-file:/tmp/work/notes.txt", False),
])
def test_cwd_policy_compares_decoded_normalized_resources(resource: str, allowed: bool) -> None:
    policy = ClientFileResourcePolicy.for_cwd("/tmp//work/sub/../")
    assert policy.grant == "client-file:%2Ftmp%2Fwork%2F*"
    assert policy.allows(resource) is allowed
    assert not CLIENT_FILE_RESOURCE_POLICY.allows(resource)


def test_root_cwd_policy_and_encoded_grant() -> None:
    assert ClientFileResourcePolicy.for_cwd("/").allows("client-file:%2Fetc%2Ffile")
    policy = ClientFileResourcePolicy("client-file", "client-file:%2ftmp%2f%77ork%2f*")
    assert policy.allows("client-file:%2Ftmp%2Fwork%2Ffile")
    assert not policy.allows("client-file:%2Ftmp%2Fwork-other%2Ffile")


@pytest.mark.parametrize(("namespace", "grant"), [
    ("other", "client-file:%2Ftmp%2Fwork%2F*"),
    ("client-file", "client-file:%2Ftmp%2Fwork%2F"),
    ("client-file", "client-file:*"),
    ("client-file", "client-file:relative%2F*"),
    ("client-file", "client-file:%2Ftmp%2Fwork*"),
])
def test_cwd_policy_rejects_invalid_grants(namespace: str, grant: str) -> None:
    assert not ClientFileResourcePolicy(namespace, grant).allows(
        "client-file:%2Ftmp%2Fwork%2Ffile",
    )


@pytest.mark.parametrize("enforce", [True, False])
@pytest.mark.parametrize("cwd", ["/tmp/work", None, "relative"])
async def test_denied_cwd_read_never_earns_trust(enforce: bool, cwd: str | None) -> None:
    provider = FakeProvider({})
    token = set_turn_capability_context(replace(_context(provider), cwd=cwd))
    try:
        authorization = get_tool_registry().authorize_tool(
            "hands_read", _admin_auth(), enforce=enforce,
            arguments={"path": "/tmp/work-sibling/secret"},
        )
        assert authorization.allowed is (not enforce)
        assert authorization.would_block
        assert authorization.result_integrity == "untrusted"
        assert authorization.protected_source_resources == ()
        assert "cwd boundary" in authorization.reason
        with pytest.raises(ToolException, match="cwd boundary"):
            await hands_read.ainvoke({"path": "/tmp/work-sibling/secret"})
        assert provider.calls == []
    finally:
        reset_turn_capability_context(token)


@pytest.mark.parametrize("wrapper", [hands_read, hands_edit])
async def test_concurrent_sessions_keep_independent_cwd_policies(wrapper: Any) -> None:
    ready = [asyncio.Event(), asyncio.Event()]

    async def session(index: int) -> None:
        provider = FakeProvider({"read": {"content": "ok"}, "edit": {"changed": True}})
        cwd = f"/tmp/session-{index}"
        context = replace(_context(provider), cwd=cwd)
        assert context.profile_policy is MIMIR_HANDS_V1
        token = set_turn_capability_context(context)
        try:
            ready[index].set()
            await ready[1 - index].wait()
            arguments = {"path": "notes.txt"}
            if wrapper is hands_edit:
                arguments.update(old_text="old", new_text="new")
            eligibility = PermissionEligibility("call", wrapper.name, "other", arguments)
            assert eligibility.canonical_client_resource == canonical_client_file_resource(
                f"{cwd}/notes.txt",
            )
            await wrapper.ainvoke(arguments)
            assert provider.calls[0][1]["path"] == f"{cwd}/notes.txt"
            with pytest.raises(ToolException, match="cwd boundary"):
                await wrapper.ainvoke({**arguments, "path": f"/tmp/session-{1 - index}/notes.txt"})
            assert len(provider.calls) == 1
        finally:
            reset_turn_capability_context(token)

    await asyncio.gather(session(0), session(1))


@pytest.mark.parametrize("wrapper", [hands_read, hands_edit])
@pytest.mark.parametrize("path", ["", "bad\x00path", "\ud800"])
async def test_invalid_eligibility_resource_still_denied(wrapper: Any, path: str) -> None:
    arguments = {"path": path}
    if wrapper is hands_edit:
        arguments.update(old_text="old", new_text="new")
    eligibility = PermissionEligibility("call", wrapper.name, "other", arguments)
    assert eligibility.canonical_client_resource is None
    provider = FakeProvider({})
    token = set_turn_capability_context(_context(provider))
    try:
        with pytest.raises(ToolException, match="path is not authorized"):
            await wrapper.ainvoke(arguments)
    finally:
        reset_turn_capability_context(token)
    assert provider.calls == []


@pytest.mark.parametrize(("title", "arguments"), [
    ("hands_shell", {"command": "pwd"}),
    ("hands_python", {"code": "1 + 1"}),
    ("other_tool", {"path": "not-a-client-target"}),
    ("hands_read", {}),
    ("hands_edit", {"path": None}),
    ("hands_read", {"path": 1}),
])
def test_eligibility_without_client_file_target_has_no_resource(
    title: str, arguments: dict[str, Any],
) -> None:
    assert PermissionEligibility("call", title, "other", arguments).canonical_client_resource is None


def test_eligibility_resource_cannot_be_supplied_as_trusted_metadata() -> None:
    arguments = {"path": "%2F", "canonical_client_resource": "client-file:forged"}
    eligibility = PermissionEligibility("call", "hands_read", "other", arguments)
    assert eligibility.canonical_client_resource == "client-file:%252F"
    with pytest.raises(TypeError, match="canonical_client_resource"):
        PermissionEligibility(
            "call", "hands_read", "other", arguments,
            canonical_client_resource="client-file:forged",
        )
    with pytest.raises(FrozenInstanceError):
        eligibility.canonical_client_resource = "client-file:forged"


@pytest.mark.asyncio
async def test_wrappers_route_through_current_turn_provider() -> None:
    first = FakeProvider({
        "read": {"content": "one"},
        "edit": {"changed": True},
        "shell": {"stdout": "ok", "stderr": "", "exitCode": 0},
    })
    second = FakeProvider({"read": {"content": "two"}})
    token = set_turn_capability_context(_context(first))
    try:
        assert await hands_read.ainvoke({"path": "a"}) == {"content": "one"}
        assert await hands_edit.ainvoke(
            {"path": "a", "old_text": "x", "new_text": "y"}
        ) == {"changed": True}
        assert await hands_shell.ainvoke({"command": "pwd"}) == {
            "stdout": "ok", "stderr": "", "exitCode": 0
        }
        nested = set_turn_capability_context(_context(second))
        try:
            assert await hands_read.ainvoke({"path": "a"}) == {"content": "two"}
        finally:
            reset_turn_capability_context(nested)
        assert await hands_read.ainvoke({"path": "a"}) == {"content": "one"}
    finally:
        reset_turn_capability_context(token)
    assert first.calls[1] == (
        "edit", {"path": "/tmp/a", "oldText": "x", "newText": "y"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper_name", "structured_content"),
    [
        ("hands_read", {"content": "file contents"}),
        ("hands_edit", {"changed": True}),
        ("hands_shell", {"stdout": "/workspace\n", "stderr": "", "exitCode": 0}),
        (
            "hands_python",
            {
                "ok": True, "stdout": "", "stderr": "", "value": "2",
                "exception": "", "timedOut": False, "kernel": "fresh",
            },
        ),
    ],
)
async def test_wrappers_accept_recorded_stock_mcp_results(
    wrapper_name: str, structured_content: dict[str, Any]
) -> None:
    response = {
        "content": [{"type": "text", "text": "Tool completed"}],
        "structuredContent": structured_content,
        "isError": False,
    }

    assert await _invoke_wrapper(wrapper_name, _stock_provider(response)) == structured_content


@pytest.mark.asyncio
async def test_advertised_output_schemas_drive_accepted_result_shapes() -> None:
    values_by_type = {"string": "value", "boolean": True, "integer": 1, "array": ["/tmp"]}

    for policy in MIMIR_HANDS_V1.tools:
        schema = _thaw(policy.result_schema)
        structured_content = {
            name: property_schema.get("enum", [values_by_type[property_schema["type"]]])[0]
            for name, property_schema in schema["properties"].items()
        }
        assert set(structured_content) == set(schema["required"])
        response = {
            "content": [{"type": "text", "text": "Tool completed"}],
            "structuredContent": structured_content,
        }

        assert await _invoke_wrapper(
            policy.wrapper_name, _stock_provider(response)
        ) == structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "structured_content",
    [
        {"content": "ok", "extra": "unvalidated"},
        {},
        {"content": 1},
    ],
)
async def test_unwrapped_payload_remains_exactly_validated(
    structured_content: dict[str, Any]
) -> None:
    response = {
        "content": [{"type": "text", "text": "Tool completed"}],
        "structuredContent": structured_content,
    }

    with pytest.raises(ToolException, match="hands_read returned a malformed result"):
        await _invoke_wrapper("hands_read", _stock_provider(response))


@pytest.mark.asyncio
async def test_schema_backed_result_requires_structured_content() -> None:
    response = {"content": [{"type": "text", "text": "file contents"}]}

    with pytest.raises(RuntimeError, match="missing structuredContent"):
        await _invoke_wrapper("hands_read", _stock_provider(response))


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_name", ["hands_read", "hands_edit", "hands_shell", "hands_python"])
@pytest.mark.parametrize("structured", [False, True])
async def test_mcp_is_error_result_is_a_tool_failure(wrapper_name: str, structured: bool) -> None:
    response = {
        "content": [{"type": "text", "text": "provider failed"}],
        "isError": True,
    }
    if structured:
        response["structuredContent"] = {"content": "must not be returned"}

    with pytest.raises(ToolException) as raised:
        await _invoke_wrapper(wrapper_name, _stock_provider(response))
    assert str(raised.value) == "provider failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrapper_name", "message"), [
    ("hands_read", "hands_read failed: No such file or directory"),
    ("hands_read", "hands_read failed: Permission denied"),
    ("hands_read", "File too large"),
    ("hands_edit", "oldText not found"),
    ("hands_edit", "Path is unwritable"),
    ("hands_shell", "Spawn failed"),
    ("hands_python", "Kernel unavailable"),
])
async def test_provider_rpc_errors_are_tool_failures(wrapper_name: str, message: str) -> None:
    provider = _stock_provider({})

    async def fail(*args: Any) -> Any:
        raise agent_module.RequestError(-32000, message, {"internal": "must not leak"})

    provider.peer.message_mcp = fail
    with pytest.raises(ToolException) as raised:
        await _invoke_wrapper(wrapper_name, provider)
    assert str(raised.value) == message


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "closed", "generation", "replaced"])
@pytest.mark.parametrize("reply", ["rpc", "isError", "success"])
async def test_provider_faults_are_not_tool_errors(failure: str, reply: str) -> None:
    provider = _stock_provider({})

    async def fail(*args: Any) -> Any:
        if failure == "transport":
            raise ConnectionError("provider connection lost")
        if failure == "closed":
            provider.closed = True
        elif failure == "generation":
            provider.session.active_prompt = None
        else:
            provider.session.provider = object()
        if reply == "rpc":
            raise agent_module.RequestError(-32000, "ordinary provider failure")
        if reply == "isError":
            return {"isError": True, "content": [{"type": "text", "text": "failure"}]}
        return {"structuredContent": {"content": "stale"}}

    provider.peer.message_mcp = fail
    expected = ConnectionError if failure == "transport" else RuntimeError
    with pytest.raises(expected) as raised:
        await _invoke_wrapper("hands_read", provider)
    assert not isinstance(raised.value, ToolException)
    assert str(raised.value) == (
        "provider connection lost" if failure == "transport" else "Stale client provider result"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "resource"),
    [
        ("notes.txt", "client-file:%2Ftmp%2Fnotes.txt"),
        ("/tmp/a b", "client-file:%2Ftmp%2Fa%20b"),
        ("client/../notes.txt", "client-file:%2Ftmp%2Fnotes.txt"),
        ("é\\file", "client-file:%2Ftmp%2F%C3%A9%5Cfile"),
    ],
)
async def test_hands_read_central_authorization_allows_admitted_admin(
    path: str, resource: str
) -> None:
    provider = FakeProvider({"read": {"content": "trusted route"}})
    token = set_turn_capability_context(_context(provider))
    try:
        authorization = get_tool_registry().authorize_tool(
            "hands_read",
            _admin_auth(),
            enforce=True,
            arguments={"path": path},
        )
        assert authorization.allowed is True
        assert authorization.protected_source_resources == (resource,)
        assert authorization.flow_direction is ToolFlowDirection.SOURCE
        assert authorization.result_integrity == "trusted"
        assert provider.calls == []
        assert await hands_read.ainvoke({"path": path}) == {
            "content": "trusted route"
        }
    finally:
        reset_turn_capability_context(token)
    assert provider.calls == [("read", {"path": client_file_resource_path(resource)})]


@pytest.mark.asyncio
async def test_hands_read_central_authorization_denies_before_provider_dispatch() -> None:
    cases = [
        (None, MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), principal=None), MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), principal=""), MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), canonical_principal=None), MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), canonical_principal=""), MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), roles=("user",)), MIMIR_HANDS_V1, "a"),
        (_admin_auth(), None, "a"),
        (_admin_auth(), replace(MIMIR_HANDS_V1, profile_id="other"), "a"),
        (_admin_auth(), replace(MIMIR_HANDS_V1, resource_policy=None), "a"),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="client-file", grant="client-file:*"
                ),
            ),
            "a",
        ),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="other", grant="other:*"
                ),
            ),
            "a",
        ),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="client-file", grant="client-file:specific"
                ),
            ),
            "a",
        ),
        (_admin_auth(), MIMIR_HANDS_V1, ""),
        (_admin_auth(), MIMIR_HANDS_V1, "bad\x00path"),
        (_admin_auth(), MIMIR_HANDS_V1, 1),
    ]
    for auth_context, profile_policy, path in cases:
        provider = FakeProvider({"read": {"content": "must not run"}})
        with pytest.raises(ToolException):
            await _invoke_authorized_read(
                provider,
                path,
                auth_context=auth_context,
                profile_policy=profile_policy,
            )
        assert provider.calls == []


@pytest.mark.asyncio
async def test_hands_read_rejects_malformed_provider_result() -> None:
    provider = FakeProvider({"read": {"content": 1}})

    with pytest.raises(ToolException, match="malformed result"):
        await _invoke_authorized_read(provider, "a", auth_context=_admin_auth())
    assert provider.calls == [("read", {"path": "/tmp/a"})]


def test_hands_surface_is_static_without_client_mcp_registration() -> None:
    names = [tool.name for tool in all_mimir_tools(require_coding_available=False)]
    assert names.count("hands_read") == 1
    assert names.count("hands_edit") == 1
    assert names.count("hands_shell") == 1
    assert names.count("hands_python") == 1
    assert tuple(tool.name for tool in HANDS_TOOLS) == (
        "hands_read", "hands_edit", "hands_shell", "hands_python", "hands_request_scope"
    )
    source = Path(__file__).parents[1] / "mimir" / "tools" / "client_provider.py"
    tree = ast.parse(source.read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(module.endswith("tools.mcp") or module == ".mcp" for module in imported)


def test_access_control_catalogs_hands_profile_policy() -> None:
    catalog = get_operation_catalog()
    assert catalog.get_decision("hands_read") is OperationDecision.RESOURCE_SCOPED
    assert catalog.get_decision("hands_edit") is OperationDecision.ADMIN_REQUIRED
    assert catalog.get_decision("hands_shell") is OperationDecision.ADMIN_REQUIRED
    assert catalog.get_decision("hands_python") is OperationDecision.ADMIN_REQUIRED
    assert get_tool_flow_direction("hands_read") is ToolFlowDirection.SOURCE
    assert get_tool_flow_direction("hands_edit") is ToolFlowDirection.BOTH
    assert get_tool_flow_direction("hands_shell") is ToolFlowDirection.BOTH
    assert get_tool_flow_direction("hands_python") is ToolFlowDirection.BOTH
    assert get_sink_category("hands_edit") is SinkCategory.EXTERNAL_MCP
    assert get_sink_category("hands_shell") is SinkCategory.SHELL_PROCESS
    assert get_sink_category("hands_python") is SinkCategory.SHELL_PROCESS


def test_hands_python_policy_and_wire_schema_are_exact() -> None:
    policy = MIMIR_HANDS_V1.tool("hands_python")
    assert policy is not None
    assert (
        policy.provider_name,
        policy.classification,
        policy.flow,
        policy.sink,
        policy.resource_namespace,
        policy.operation,
    ) == (
        "python",
        "admin_required",
        "both",
        "shell_process",
        None,
        "client_authorized_host_execution",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/outside/exact.txt"])
async def test_scope_wrapper_routes_exact_path_without_wrapper_permission(path: str) -> None:
    from mimir.tools.client_provider import hands_request_scope
    from mimir.tools.budget_gate import _permission_eligibility
    result = {"approved": True, "paths": ["/tmp", "/outside/exact.txt"], "message": "Scope"}
    provider = FakeProvider({"request_scope": result})
    token = set_turn_capability_context(_context(provider))
    try:
        assert await hands_request_scope.ainvoke({"path": path}) == result
        assert provider.calls == [("request_scope", {"path": path})]
        assert _permission_eligibility(None, "hands_request_scope", None, {"path": path}) is None
    finally:
        reset_turn_capability_context(token)


@pytest.mark.parametrize("change", [{}, {"roles": ("user",)}, {"is_service": True}, {"principal": ""}])
def test_scope_request_requires_live_acp_admin(change: dict) -> None:
    from mimir.acp.journal import JournalLease
    context = replace(_context(FakeProvider({})), lease=JournalLease("scope", 1, 1))
    token = set_turn_capability_context(context)
    try:
        auth = replace(_admin_auth(), **change)
        verdict = get_tool_registry().authorize_tool("hands_request_scope", auth, enforce=False, arguments={"path": ""})
        assert verdict.allowed is (not change)
        assert get_tool_flow_direction("hands_request_scope") is ToolFlowDirection.NEITHER
        assert get_sink_category("hands_request_scope") is SinkCategory.UNKNOWN
        assert issue_client_authorized_host_execution(request_identity=object(), auth_context_identity=auth, wrapper_name="hands_request_scope", tainted=True) is None
    finally:
        reset_turn_capability_context(token)
    assert not get_tool_registry().authorize_tool("hands_request_scope", _admin_auth(), enforce=False, arguments={"path": ""}).allowed


@pytest.mark.asyncio
@pytest.mark.usefixtures("middleware_event_logger")
async def test_scope_request_middleware_routes_tainted_turn_without_execution_grant() -> None:
    from langchain.agents.middleware import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langgraph.runtime import Runtime
    from mimir.acp.journal import JournalLease
    from mimir.models import InformationFlowLabels, InformationFlowState, SourceLabel
    from mimir.tools.budget_gate import BudgetGateMiddleware

    class NeverBroker:
        async def request_permission(self, eligibility):
            pytest.fail("scope requests must reach the provider, not wrapper permission")

    labels = InformationFlowLabels().with_source(SourceLabel(
        source_kind="acp_hands_result", principal="admin", domain="client_provider",
        resource_id="shell", bridge_instance="acp", sensitivity="private",
        integrity="untrusted", integrity_effect="active_ingest",
    ))
    auth = replace(_admin_auth(), ifc_labels=labels, ifc_state=InformationFlowState(labels))
    context = replace(_context(FakeProvider({})), lease=JournalLease("scope", 1, 1), permission_broker=NeverBroker())
    token = set_turn_capability_context(context)
    calls = []
    request = ToolCallRequest(
        tool_call={"name": "hands_request_scope", "args": {"path": ""}, "id": "scope", "type": "tool_call"},
        tool=hands_request_scope, state=None, runtime=Runtime(context=auth),
    )
    async def handler(r):
        calls.append(r.tool_call)
        return ToolMessage(content='{"approved":true,"paths":["/tmp"],"message":"Current scope"}', tool_call_id="scope")
    try:
        before = auth.ifc_state.current(labels)
        result = await BudgetGateMiddleware().awrap_tool_call(request, handler)
        assert result.status == "success"
        assert len(calls) == 1
        assert auth.ifc_state.current(labels) == before
    finally:
        reset_turn_capability_context(token)


@pytest.mark.asyncio
@pytest.mark.usefixtures("middleware_event_logger")
async def test_accepted_unconfined_python_does_not_inherit_cwd_read_trust_or_acknowledge_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from langchain.agents.middleware import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langgraph.runtime import Runtime
    from mimir.acp import confinement
    from mimir.acp.hosted import HostedHandsProvider
    from mimir.acp.journal import JournalLease
    from mimir.access_control import classify_protected_result
    from mimir.models import InformationFlowLabels, InformationFlowState, SourceLabel
    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir.tools.client_provider import PermissionDecision

    def unavailable() -> object:
        raise confinement.BackendUnavailable("test missing backend")

    monkeypatch.setattr(confinement, "_backend", unavailable)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "trusted.txt").write_text("cwd content")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content")
    initial = InformationFlowLabels().with_source(SourceLabel(
        source_kind="protected_tool", principal="admin", domain="web",
        resource_id="https://example.test/input", bridge_instance="web",
        authorized_principals=frozenset({"admin"}), sensitivity="internal",
        integrity="untrusted", integrity_effect="active_ingest",
    ))
    state = InformationFlowState(initial)
    auth = replace(
        _admin_auth(), channel_id="acp:provenance", resource_id="acp:provenance",
        bridge_instance="acp-stdio", domain="channel", origin_trigger="acp_session",
        ifc_labels=initial, ifc_state=state,
    )
    risk_requests = []

    async def accept_risk(session_id: str) -> bool:
        risk_requests.append(session_id)
        assert state.current() == initial
        assert state.permission_has_untrusted_active_ingest(initial)
        return True  # Independent operator risk consent, not ingest acknowledgement.

    class WrapperBroker:
        def __init__(self) -> None:
            self.calls = []

        async def request_permission(self, eligibility):
            self.calls.append(eligibility)
            return PermissionDecision.ALLOW_SESSION

    broker = WrapperBroker()
    hosted = HostedHandsProvider(request_unconfined_permission=accept_risk)
    hosted.bind_session("hosted", cwd)
    context = replace(
        _context(FakeProvider({})), cwd=str(cwd),
        lease=JournalLease("provenance", 1, 1), permission_broker=broker,
    )
    token = set_turn_capability_context(context)
    code = f"from pathlib import Path\nPath({str(outside)!r}).read_text()"

    async def execute(request):
        result = await hosted.execute_python(hosted._sessions["hosted"], code)
        assert result["ok"] is True
        assert result["value"] == repr("outside content")
        assert "UNCONFINED" in result["stderr"]
        return ToolMessage(content=json.dumps(result), tool_call_id=request.tool_call["id"])

    try:
        for call_id in ("python-1", "python-2"):
            request = ToolCallRequest(
                tool_call={"name": "hands_python", "args": {"code": code}, "id": call_id, "type": "tool_call"},
                tool=hands_python, state=None, runtime=Runtime(context=auth),
            )
            result = await BudgetGateMiddleware().awrap_tool_call(request, execute)
            assert result.status == "success"
        assert risk_requests == ["hosted"]
        assert len(broker.calls) == 2
        assert all(call.host_execution.tainted is True for call in broker.calls)
        current = state.current()
        assert initial.sources <= current.sources
        python_sources = [source for source in current.sources if source.source_kind == "acp_hands_result"]
        assert python_sources
        assert all(source.integrity == "untrusted" for source in python_sources)
        assert all(source.integrity_effect == "active_ingest" for source in python_sources)
        assert current.has_untrusted_active_ingest
        assert state.permission_has_untrusted_active_ingest(initial)

        # The separate validated cwd-read operation still earns #1584's trust.
        read_args = {"path": "trusted.txt"}
        verdict = get_tool_registry().authorize_tool("hands_read", auth, enforce=True, arguments=read_args)
        assert verdict.allowed and verdict.result_integrity == "trusted"
        read_labels = classify_protected_result(
            "hands_read", read_args, auth, verdict, result={"content": "cwd content"},
        )
        assert read_labels.sources
        assert all(source.integrity == "trusted" for source in read_labels.sources)
        assert not read_labels.has_untrusted_active_ingest
        # A trusted read does not erase the independent Python/web ingestion.
        assert state.permission_has_untrusted_active_ingest(initial)
    finally:
        reset_turn_capability_context(token)
        await hosted.close()
