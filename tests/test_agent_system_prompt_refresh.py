from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path

import pytest

from mimir.agent import Agent, _DEFAULT_SYSTEM_PROMPT
from mimir.config import Config
from mimir.event_logger import get_logger, init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.turn_logger import TurnLogger


def _write_core(home: Path, body: str) -> None:
    core = home / "memory" / "core"
    core.mkdir(parents=True, exist_ok=True)
    (core / "00-test.md").write_text(body, encoding="utf-8")


def _make_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Agent:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "memory").mkdir(parents=True, exist_ok=True)
    (home / "memory" / "INDEX.md").write_text(
        "# Memory Index\n\n- initial", encoding="utf-8"
    )
    _write_core(home, "<!-- desc: test -->\n# Test\n\nINITIAL CORE BODY\n")
    init_logger(home / "logs" / "events.jsonl", session_id="test")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    cfg = Config.from_env()
    return Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=home / "messages.jsonl"),
        index_generator=IndexGenerator(home),
    )


class _PromptCapture:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []
        self.graphs: list[object] = []

    def create_deep_agent(self, **kwargs):
        self.prompts.append(kwargs["system_prompt"])
        self.kwargs.append(kwargs)
        graph = object()
        self.graphs.append(graph)
        return graph


def _stub_deepagent_build(monkeypatch: pytest.MonkeyPatch) -> _PromptCapture:
    capture = _PromptCapture()
    monkeypatch.setitem(
        sys.modules,
        "deepagents",
        types.SimpleNamespace(create_deep_agent=capture.create_deep_agent),
    )
    monkeypatch.setattr("mimir.agent.resolve_model_from_config", lambda *a, **kw: object())
    monkeypatch.setattr("mimir.tools.all_mimir_tools", lambda **_kwargs: [])
    return capture


@pytest.mark.asyncio
async def test_build_agent_reuses_graph_when_prompt_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)

    first = await agent._build_agent_if_needed()
    second = await agent._build_agent_if_needed()

    assert first is second
    assert capture.graphs == [first]
    assert len(capture.prompts) == 1
    assert "INITIAL CORE BODY" in capture.prompts[0]


@pytest.mark.asyncio
async def test_build_agent_renders_system_prompt_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    _stub_deepagent_build(monkeypatch)
    loop_thread = threading.get_ident()
    render_threads: list[int] = []

    def render(*, emit_health_events: bool = False) -> str:
        render_threads.append(threading.get_ident())
        return f"prompt:{emit_health_events}"

    monkeypatch.setattr(agent, "_current_system_prompt", render)

    await agent._build_agent_if_needed()

    assert render_threads
    assert all(thread_id != loop_thread for thread_id in render_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_first_build", [False, True])
async def test_build_agent_routes_lease_root_created_after_first_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_first_build: bool,
) -> None:
    from mimir.tools import registry

    agent = _make_agent(tmp_path, monkeypatch)
    _stub_deepagent_build(monkeypatch)
    lease_root = tmp_path / "leases"
    from mimir.plugins.manager import load_plugins

    agent._config.plugin_manager = load_plugins(enable="")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    agent._config.coding_enabled = failed_first_build
    monkeypatch.setattr("mimir.tools.all_mimir_tools", registry.all_mimir_tools)

    await agent._build_agent_if_needed()
    previous_backend = agent._backend
    checkout = lease_root / "checkout"
    checkout.mkdir(parents=True)
    target = checkout / "review.txt"
    target.write_text("leased checkout content\n", encoding="utf-8")
    assert "outside_file_tool_roots" in previous_backend.read(str(target)).error

    _write_core(agent._config.home, "<!-- desc: test -->\n# Changed core\n")
    graph = await agent._build_agent_if_needed()
    result = agent._backend.read(str(target))
    assert result.error is None
    assert result.file_data["content"] == "leased checkout content\n"
    refreshed_backend = agent._backend
    assert refreshed_backend is not previous_backend
    assert await agent._build_agent_if_needed() is graph
    assert agent._backend is refreshed_backend


@pytest.mark.asyncio
async def test_build_agent_always_routes_worklink_root_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control

    retained = tmp_path / "retained"
    retained.mkdir()
    target = retained / "issue.txt"
    target.write_text("retained", encoding="utf-8")
    monkeypatch.setattr(access_control, "worklink_retained_checkout_root", lambda: retained)
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    agent = _make_agent(tmp_path, monkeypatch)
    _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()

    assert str(retained) + "/" in agent._backend.routes
    assert agent._backend.read(str(target)).error is None
    assert agent._backend.write(str(retained / "new.txt"), "blocked").error
    assert not (retained / "new.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_root", ["same", "parent"])
async def test_build_agent_retained_route_overrides_operator_rw_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_root: str,
) -> None:
    from mimir import access_control

    operator_root = tmp_path / "workspace"
    retained = operator_root if configured_root == "same" else operator_root / ".worklink"
    retained.mkdir(parents=True)
    monkeypatch.setattr(access_control, "worklink_retained_checkout_root", lambda: retained)
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{operator_root}:rw")
    agent = _make_agent(tmp_path, monkeypatch)
    _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()

    assert str(operator_root) + "/" in agent._backend.routes
    assert str(retained) + "/" in agent._backend.routes
    target = retained / "new.txt"
    assert agent._backend.write(str(target), "blocked").error
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["symlink", "relative", "missing", "file"])
async def test_build_agent_does_not_route_invalid_lease_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    _stub_deepagent_build(monkeypatch)
    target_root = tmp_path / "outside"
    target_root.mkdir()
    target = target_root / "private.txt"
    target.write_text("not routed", encoding="utf-8")
    lease_root = tmp_path / "leases"
    if invalid == "symlink":
        lease_root.symlink_to(target_root, target_is_directory=True)
    elif invalid == "relative":
        monkeypatch.chdir(tmp_path)
        lease_root = Path("outside")
    elif invalid == "file":
        lease_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    await agent._build_agent_if_needed()

    assert str(lease_root.resolve()) + "/" not in getattr(agent._backend, "routes", {})
    assert agent._backend.read(str(target)).error is not None


@pytest.mark.asyncio
async def test_build_agent_rebuilds_without_coding_tools_after_identity_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    agent = _make_agent(tmp_path, monkeypatch)
    from mimir.plugins.manager import load_plugins

    agent._config.plugin_manager = load_plugins(enable="cultivate")
    capture = _stub_deepagent_build(monkeypatch)
    requested: list[dict] = []
    monkeypatch.setattr(
        "mimir.tools.all_mimir_tools",
        lambda **kwargs: requested.append(kwargs) or [],
    )
    from mimir_cultivate import forge as forge_tools
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)

    first = await agent._build_agent_if_needed()
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", True)
    second = await agent._build_agent_if_needed()

    assert first is not second
    assert len(requested) == 2
    assert all("coding_enabled" not in kwargs for kwargs in requested)
    assert "cultivate" in agent._cached_unready_tool_owners


@pytest.mark.asyncio
async def test_build_agent_restores_and_executes_real_repo_tool_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)
    from mimir_cultivate import forge as forge_tools
    from mimir_cultivate import repository as repo_tools
    from langchain.tools import ToolRuntime
    from mimir.access_control import ScopedPRGrants, _SCOPED_PR_ISSUER
    from mimir.models import AuthContext
    from mimir_cultivate.policy import RepoPRScopeRegistry, RepoReviewState
    from mimir.plugins.core import policy_scope
    from tests.test_pr_checkout_lease import _repo_and_scope
    from mimir.tools import registry

    monkeypatch.setattr(forge_tools, "_github_identity_degraded", True)
    monkeypatch.setattr(
        "mimir.tools.all_mimir_tools",
        lambda **kwargs: registry.all_mimir_tools(
            **kwargs, require_provider_available=False,
        ),
    )

    first = await agent._build_agent_if_needed()
    assert "repo_status" not in {tool.name for tool in capture.kwargs[-1]["tools"]}
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    second = await agent._build_agent_if_needed()

    assert first is not second
    restored = {tool.name: tool for tool in capture.kwargs[-1]["tools"]}
    assert "repo_status" in restored
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    lease = repo_tools.create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root, review_state=state,
    )
    selected = agent._config.plugin_manager.policy
    grants = ScopedPRGrants(
        scope.principal, (scope,), protocols=selected.scoped_actions,
        _issuer=_SCOPED_PR_ISSUER,
    )
    checkout_token = grants.consume_checkout("cultivate", scope)
    assert checkout_token is not None
    assert grants.observe_checkout("cultivate", scope, lease, checkout_token)
    auth = AuthContext(
        principal=scope.principal, canonical_principal=scope.principal,
        roles=("user",), event_ingress=None, trigger="user_message",
        channel_id="web:recovery", interactivity=None, enforcement_enabled=True,
        repo_pr_scope_registry=RepoPRScopeRegistry((state,)),
        repo_review_state=state, repo_pr_action_scope=scope,
        scoped_pr_grants=grants,
    )
    runtime = ToolRuntime(
        state={}, context=auth, config={}, stream_writer=lambda _: None,
        tool_call_id="recovered-tool", store=None,
    )
    with policy_scope(selected):
        result = restored["repo_status"].invoke({
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
            "runtime": runtime,
        })
    assert result["code"] == "ok"
    assert state.checkout_lease is lease


@pytest.mark.asyncio
async def test_agent_assembles_registered_worklink_runner_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    lease_root = tmp_path / "pr-leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)
    from mimir.tools import registry
    from mimir_cultivate import forge
    from mimir_cultivate.worklink import autonomy, orchestrator

    monkeypatch.setattr(forge, "_github_identity_degraded", False)
    monkeypatch.setattr("mimir.tools.all_mimir_tools", registry.all_mimir_tools)
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setenv("WORKLINK_REPO", str(repository))
    monkeypatch.setattr(autonomy, "check_concurrency", lambda _home: autonomy.ConcurrencyCheck(True, 0, 2))
    registry.set_arbiter(None)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(
            issue_id=1949, status="completed", attempt=1,
            review_ready=True, pr_url=None, evidence_path=None, reason=None,
        )

    monkeypatch.setattr(orchestrator, "run_worklink", dispatch)
    await agent._build_agent_if_needed()
    tools = {tool.name: tool for tool in capture.kwargs[-1]["tools"]}
    assert "worklink_run" in tools
    result = await tools["worklink_run"].ainvoke({"issue_id": 1949})
    assert "worklink_run #1949: completed" in result
    assert calls == [{
        "home": agent._config.home, "repo": repository,
        "issue_id": 1949, "backend": None, "autonomous": True,
    }]


@pytest.mark.asyncio
async def test_build_agent_registers_structured_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()

    subagents = capture.kwargs[0]["subagents"]
    # Worklink epic roles are per-run tool-armed agents (the retired epic roles (removed #830)),
    # not agent-wide registrations.
    assert [spec["name"] for spec in subagents] == [
        "general-purpose",
        "critic-structured",
    ]
    middleware = capture.kwargs[0]["middleware"]
    # These hooks operate on the parent's skill catalog and inbound queue.
    # Any new parent middleware needs an explicit child-inheritance decision.
    parent_only = {"SkillMemoryInjectionMiddleware", "MidTurnInjectionMiddleware"}
    shared = [item.name for item in middleware if item.name not in parent_only]
    for spec in subagents:
        child_only = {"StructuredOutputRetryMiddleware"}
        assert [item.name for item in spec["middleware"] if item.name not in child_only] == shared
    todo_middleware = [item for item in middleware if item.name == "TodoListMiddleware"]
    assert len(todo_middleware) == 1
    assert [tool.name for tool in todo_middleware[0].tools] == ["write_todos"]
    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir.tools.service_tool_surface import ServiceToolSurfaceMiddleware

    assert sum(isinstance(item, ServiceToolSurfaceMiddleware) for item in middleware) == 1
    surface_index = next(i for i, item in enumerate(middleware)
                         if isinstance(item, ServiceToolSurfaceMiddleware))
    assert surface_index + 1 < len(middleware), "service surface must precede BudgetGate"
    assert isinstance(middleware[surface_index + 1], BudgetGateMiddleware)


@pytest.mark.asyncio
async def test_build_agent_rebuilds_when_rendered_system_prompt_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)

    first = await agent._build_agent_if_needed()
    _write_core(
        agent._config.home,
        "<!-- desc: test -->\n# Test\n\nUPDATED CORE BODY\n",
    )
    second = await agent._build_agent_if_needed()
    third = await agent._build_agent_if_needed()

    assert first is not second
    assert second is third
    assert len(capture.prompts) == 2
    assert "INITIAL CORE BODY" in capture.prompts[0]
    assert "UPDATED CORE BODY" in capture.prompts[1]


@pytest.mark.asyncio
async def test_build_agent_rebuilds_when_memory_index_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()
    (agent._config.home / "memory" / "INDEX.md").write_text(
        "# Memory Index\n\n- UPDATED INDEX ENTRY", encoding="utf-8"
    )
    await agent._build_agent_if_needed()

    assert len(capture.prompts) == 2
    assert "- initial" in capture.prompts[0]
    assert "UPDATED INDEX ENTRY" in capture.prompts[1]


@pytest.mark.asyncio
async def test_build_agent_rebuilds_when_skill_catalog_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    capture = _stub_deepagent_build(monkeypatch)

    builtin_skill = agent._config.home / ".mimir_builtin_skills" / "demo"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: initial\n---\n", encoding="utf-8"
    )

    first = await agent._build_agent_if_needed()
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: updated\n---\n", encoding="utf-8"
    )
    second = await agent._build_agent_if_needed()
    third = await agent._build_agent_if_needed()

    assert first is not second
    assert second is third
    assert len(capture.prompts) == 2


@pytest.mark.asyncio
async def test_core_prompt_degraded_event_emits_only_on_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    logger = get_logger()
    capture = _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()
    await agent._build_agent_if_needed()
    _write_core(agent._config.home, "<!-- desc: test -->\n# Test\n\nCHANGED\n")
    await agent._build_agent_if_needed()

    assert len(capture.prompts) == 2
    await asyncio.to_thread(logger.flush_sync)
    events = agent._config.events_log.read_text(encoding="utf-8")
    assert events.count('"type": "core_prompt_degraded"') == 2


def test_normal_system_prompt_includes_create_only_write_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)

    prompt = agent._current_system_prompt()

    assert (
        "`write_file` creates a new file only. It never overwrites an existing path; "
        "when the target exists, use `edit_file` instead."
    ) in prompt


def test_fallback_system_prompt_includes_create_only_write_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    monkeypatch.setattr("mimir.core_blocks.load_core", lambda _home: 1 / 0)

    prompt = agent._build_system_prompt()

    # Containment, not equality: under MIMIR_ACCESS_CONTROL_ENFORCED the agent
    # appends the declassification guidance block (mimir/prompts.py), so the
    # fallback prompt is the default PLUS that block. This test is about the
    # create-only guidance being present, which holds in both modes.
    assert _DEFAULT_SYSTEM_PROMPT in prompt
    assert (
        "`write_file` creates a new file only. It never overwrites an existing path; "
        "when the target exists, use `edit_file` instead."
    ) in prompt


def test_system_prompt_override_is_preserved_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    override = "Operator-owned prompt without appended defaults."
    monkeypatch.setenv("MIMIR_SYSTEM_PROMPT_OVERRIDE", override)

    assert agent._current_system_prompt() == override


def test_real_graph_binds_only_the_mimir_system_prompt(tmp_path: Path) -> None:
    """The caller-owned prompt reaches the model without framework prose."""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from mimir.readonly_backend import MimirFilesystemMiddleware, WriteGuardBackend

    sentinel = "MIMIR-ONLY-PROMPT-SENTINEL"
    model_requests: list[list[object]] = []

    class _PromptCapturingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            model_requests.append(list(messages))
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs,
            )

    model = _PromptCapturingModel(messages=iter([AIMessage(content="done")]))
    backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
    graph = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=sentinel,
        backend=backend,
        middleware=[MimirFilesystemMiddleware(backend=backend)],
    )
    graph.invoke({"messages": [HumanMessage(content="finish")]})

    system_prompts = [
        "".join(
            part["text"] if isinstance(part, dict) else part.text
            for part in message.content
        ) if isinstance(message.content, list) else message.content
        for request in model_requests
        for message in request
        if isinstance(message, SystemMessage)
    ]
    assert system_prompts == [sentinel]
