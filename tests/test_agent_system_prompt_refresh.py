from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mimir.agent import Agent, _DEFAULT_SYSTEM_PROMPT
from mimir.config import Config
from mimir.event_logger import init_logger
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
async def test_build_agent_rebuilds_without_coding_tools_after_identity_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    agent._config.coding_enabled = True
    capture = _stub_deepagent_build(monkeypatch)
    requested: list[bool] = []
    monkeypatch.setattr(
        "mimir.tools.all_mimir_tools",
        lambda **kwargs: requested.append(kwargs.get("coding_enabled", False)) or [],
    )
    from mimir.tools import forge as forge_tools
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)

    first = await agent._build_agent_if_needed()
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", True)
    second = await agent._build_agent_if_needed()

    assert first is not second
    assert requested == [True, False]
    assert agent._cached_coding_enabled is False


@pytest.mark.asyncio
async def test_build_agent_restores_and_executes_real_repo_tool_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    agent._config.coding_enabled = True
    capture = _stub_deepagent_build(monkeypatch)
    from mimir.tools import forge as forge_tools
    from mimir.tools import registry
    from mimir.tools import repo as repo_tools

    monkeypatch.setattr(forge_tools, "_github_identity_degraded", True)
    monkeypatch.setattr(
        "mimir.tools.all_mimir_tools",
        lambda **kwargs: registry.all_mimir_tools(
            **kwargs, require_coding_available=False,
        ),
    )

    first = await agent._build_agent_if_needed()
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    second = await agent._build_agent_if_needed()

    assert first is not second
    restored = {tool.name: tool for tool in capture.kwargs[-1]["tools"]}
    assert "repo_status" in restored
    executed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        repo_tools,
        "_execute",
        lambda runtime, repository, pull_request, operation: (
            executed.append((repository, pull_request)) or {"status": "clean"}
        ),
    )
    assert restored["repo_status"].invoke({
        "repository": "owner/repo",
        "pull_request": 17,
    }) == {"status": "clean"}
    assert executed == [("owner/repo", 17)]


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
    todo_middleware = [item for item in middleware if item.name == "TodoListMiddleware"]
    assert len(todo_middleware) == 1
    assert [tool.name for tool in todo_middleware[0].tools] == ["write_todos"]


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
    capture = _stub_deepagent_build(monkeypatch)

    await agent._build_agent_if_needed()
    await agent._build_agent_if_needed()
    _write_core(agent._config.home, "<!-- desc: test -->\n# Test\n\nCHANGED\n")
    await agent._build_agent_if_needed()

    assert len(capture.prompts) == 2
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
