from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mimir.agent import Agent
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
    monkeypatch.setattr("mimir.tools.all_mimir_tools", lambda: [])
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
