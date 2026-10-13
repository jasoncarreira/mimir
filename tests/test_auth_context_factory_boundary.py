from __future__ import annotations

import ast
from argparse import Namespace
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks.longmemeval_via_memory import runner
from mimir.access_control import create_auth_context, create_local_operator_auth_context
from mimir._context import reset_current_turn, set_current_turn
from mimir._deepagents_subagent_auth import _subagent_auth_context, _wrap_task_tool
from mimir.commitments import cli as commitments_cli
from mimir.models import AgentEvent, TurnContext, TurnInteractivity
from mimir.plugins.registry import PolicyRegistry
from mimir.reflection import most_retrieved
from mimir import scheduler_dashboard


ROOT = Path(__file__).resolve().parents[1]


def test_owned_state_slots_follow_live_carriers_without_cross_owner_writes() -> None:
    @dataclass(frozen=True)
    class Alpha:
        value: str

    @dataclass(frozen=True)
    class Beta:
        value: int

    policy = PolicyRegistry()
    alpha = policy.registrar("alpha")
    beta = policy.registrar("beta")
    alpha.register_state_slot(Alpha)
    beta.register_state_slot(Beta)
    policy.freeze()
    auth = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="channel", author="user"),
        policy=policy,
    )
    assert auth.plugin_state("alpha") is None
    with pytest.raises(TypeError, match="registered slot type"):
        auth.with_plugin_state(alpha, Beta(1))
    with pytest.raises(ValueError, match="matching owner registrar"):
        auth.with_plugin_state(PolicyRegistry().registrar("alpha"), Alpha("wrong"))

    bound = auth.with_plugin_state(alpha, Alpha("initial"))
    assert auth.plugin_state("alpha") is None
    turn = TurnContext("turn", "channel", "user_message", "channel", 0, auth_context=bound)
    turn.set_plugin_state(beta, Beta(2))
    assert turn.plugin_state("alpha") == Alpha("initial")
    assert turn.plugin_state("beta") == Beta(2)
    assert bound.plugin_state("beta") is None
    snapshot = turn.auth_with_plugin_state()
    assert snapshot.plugin_state("beta") == Beta(2)
    assert replace(snapshot, trigger="continuation").plugin_state("alpha") == Alpha("initial")
    other = TurnContext("other", "channel", "user_message", "channel", 0, auth_context=bound)
    assert other.plugin_state("beta") is None


def test_plugin_state_keeps_auth_context_opaque_to_pydantic() -> None:
    from pydantic import TypeAdapter

    @dataclass(frozen=True)
    class State:
        value: str

    policy = PolicyRegistry()
    registrar = policy.registrar("example")
    registrar.register_state_slot(State)
    policy.freeze()
    auth = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="channel", author="user"),
        policy=policy,
    ).with_plugin_state(registrar, State("secret"))
    assert TypeAdapter(type(auth)).dump_python(auth) == {"audience_provider": None}


def test_subagent_task_receives_live_owner_snapshot() -> None:
    @dataclass(frozen=True)
    class State:
        value: str

    policy = PolicyRegistry()
    registrar = policy.registrar("example")
    registrar.register_state_slot(State)
    policy.freeze()
    auth = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="channel", author="user"),
        policy=policy,
    )
    turn = TurnContext("turn", "channel", "user_message", "channel", 0, auth_context=auth)
    turn.set_plugin_state(registrar, State("live"))
    task = _wrap_task_tool(SimpleNamespace(
        func=lambda **_kwargs: _subagent_auth_context.get(),
        coroutine=lambda **_kwargs: None,
    ))
    token = set_current_turn(turn)
    try:
        child = task.func(runtime=SimpleNamespace(context=auth))
    finally:
        reset_current_turn(token)
    assert child.plugin_state("example") == State("live")
    assert auth.plugin_state("example") is None
    assert _subagent_auth_context.get() is None


class _AuthContextConstructorVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[str] = []
        self.constructors: list[tuple[int, str | None]] = []
        self.import_time_operator_contexts: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        constructor = (
            isinstance(node.func, ast.Name) and node.func.id == "AuthContext"
        ) or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "AuthContext"
        )
        function = self.functions[-1] if self.functions else None
        if constructor:
            self.constructors.append((node.lineno, function))
        local_operator_factory = (
            isinstance(node.func, ast.Name)
            and node.func.id == "create_local_operator_auth_context"
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_local_operator_auth_context"
        )
        if local_operator_factory and function is None:
            self.import_time_operator_contexts.append(node.lineno)
        self.generic_visit(node)


def test_only_factory_constructs_auth_context() -> None:
    paths = sorted((ROOT / "mimir").rglob("*.py"))
    paths.append(ROOT / "benchmarks" / "longmemeval_via_memory" / "runner.py")
    constructors: list[tuple[Path, str | None]] = []
    import_time_operator_contexts: list[str] = []

    for path in paths:
        visitor = _AuthContextConstructorVisitor()
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        constructors.extend(
            (path.relative_to(ROOT), function)
            for _, function in visitor.constructors
        )
        import_time_operator_contexts.extend(
            f"{path.relative_to(ROOT)}:{line}"
            for line in visitor.import_time_operator_contexts
        )

    assert constructors == [(Path("mimir/access_control.py"), "create_auth_context")]
    assert import_time_operator_contexts == []


def test_local_operator_context_preserves_authority() -> None:
    context = create_local_operator_auth_context(
        principal="local-operator",
        trigger="local_utility",
        channel_id=None,
        enforce=True,
    )

    assert context.principal == "local-operator"
    assert context.canonical_principal == "local-operator"
    assert context.roles == ("admin",)
    assert context.trigger == "local_utility"
    assert context.channel_id is None
    assert context.interactivity == TurnInteractivity.NON_INTERACTIVE
    assert context.enforcement_enabled is True
    assert context.is_service is False


@pytest.mark.asyncio
async def test_reflection_context_uses_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []

    class Client:
        auth_context: object | None = None

        async def most_retrieved_atoms(self, **kwargs: Any) -> list[dict[str, Any]]:
            self.auth_context = kwargs["auth_context"]
            return []

        async def close(self) -> None:
            return None

    client = Client()
    monkeypatch.setattr(
        most_retrieved,
        "create_local_operator_auth_context",
        lambda **kwargs: calls.append(kwargs) or sentinel,
    )
    monkeypatch.setattr(
        most_retrieved.Config,
        "from_env",
        lambda: SimpleNamespace(home=Path("/tmp/mimir-test")),
    )
    monkeypatch.setattr(most_retrieved, "make_saga_client", lambda **kwargs: client)

    await most_retrieved.run(
        Namespace(days=7, count=10, channel=None, contributed_only=False, trend=None)
    )

    assert calls == [{
        "principal": "operator",
        "trigger": "reflection_cli",
        "channel_id": None,
    }]
    assert client.auth_context is sentinel


def test_commitments_context_uses_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []

    class Store:
        auth_context: object | None = None

        def list(self, **kwargs: Any) -> list[Any]:
            self.auth_context = kwargs["auth_context"]
            return []

    store = Store()
    monkeypatch.setattr(commitments_cli, "_resolve_store", lambda args: store)
    monkeypatch.setattr(
        commitments_cli,
        "create_local_operator_auth_context",
        lambda **kwargs: calls.append(kwargs) or sentinel,
    )

    commitments_cli.cmd_list(
        Namespace(status="pending", channel=None, owner=None, include_service=False)
    )

    assert calls == [{
        "principal": "operator",
        "trigger": "commitments_cli",
        "channel_id": None,
    }]
    assert store.auth_context is sentinel


def test_dashboard_context_uses_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []

    class Store:
        auth_context: object | None = None

        def list(self, **kwargs: Any) -> list[Any]:
            self.auth_context = kwargs["auth_context"]
            return []

    store = Store()
    monkeypatch.setattr(
        scheduler_dashboard,
        "create_local_operator_auth_context",
        lambda **kwargs: calls.append(kwargs) or sentinel,
    )

    scheduler_dashboard._commitment_rows(store, due_window="all", now_unix=0.0)

    assert calls == [{
        "principal": "operator",
        "trigger": "scheduler_dashboard",
        "channel_id": None,
    }]
    assert store.auth_context is sentinel


def test_longmemeval_context_uses_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    monkeypatch.setattr(
        runner,
        "create_local_operator_auth_context",
        lambda **kwargs: calls.append(kwargs) or sentinel,
    )

    context = runner._benchmark_auth_context()

    assert context is sentinel
    assert calls == [{
        "principal": "benchmark-operator",
        "trigger": "longmemeval",
        "channel_id": "longmemeval",
        "enforce": True,
    }]
