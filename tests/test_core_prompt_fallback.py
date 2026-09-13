"""Issue #1703 B: observable prompt degradation without losing conventions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.agent import Agent, _DEFAULT_SYSTEM_PROMPT
from mimir.core_blocks import load_core
from mimir.prompts import (
    ENFORCEMENT_GUIDANCE,
    _DEFAULT_CONVENTIONS,
    build_system_prompt,
)


@pytest.fixture
def agent(tmp_path):
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    for i in range(5):
        (core / f"{i:02d}-block.md").write_text("x" * 300, encoding="utf-8")
    agent = Agent.__new__(Agent)
    agent._config = SimpleNamespace(
        home=tmp_path, writable_dirs=["memory", "scratch"],
        access_control_enforced=False, operator_alert_channel="alerts",
    )
    agent._indexes = Mock()
    agent._indexes.read_memory_index.return_value = "Index body"
    return agent


@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("failure_site", ["load", "index", "builder"])
def test_assembly_fallback_reports_exception_and_retains_conventions(
    agent, monkeypatch, enforced, failure_site,
):
    agent._config.access_control_enforced = enforced
    failure = Mock(side_effect=ValueError("private exception detail"))
    if failure_site == "load":
        monkeypatch.setattr("mimir.core_blocks.load_core", failure)
    elif failure_site == "index":
        agent._indexes.read_memory_index = failure
    else:
        monkeypatch.setattr("mimir.prompts.build_system_prompt", failure)
    emit = Mock()
    monkeypatch.setattr("mimir.agent.log_event_sync", emit)

    prompt = agent._build_system_prompt()

    assert prompt == "\n\n".join(
        [_DEFAULT_SYSTEM_PROMPT, _DEFAULT_CONVENTIONS]
        + ([ENFORCEMENT_GUIDANCE] if enforced else [])
    )
    emit.assert_called_once_with(
        "core_prompt_degraded", reason="assembly_fallback", error_type="ValueError",
    )


@pytest.mark.parametrize("emit_health_events", [False, True])
def test_fallback_survives_event_failure_and_respects_comparison_mode(
    agent, monkeypatch, emit_health_events,
):
    agent._indexes.read_memory_index.side_effect = RuntimeError("index failed")
    emit = Mock(side_effect=RuntimeError("logger unavailable"))
    monkeypatch.setattr("mimir.agent.log_event_sync", emit)
    assert _DEFAULT_CONVENTIONS in agent._build_system_prompt(
        emit_health_events=emit_health_events,
    )
    assert emit.call_count == int(emit_health_events)


def test_healthy_prompt_unchanged_and_no_degradation_event(agent, monkeypatch):
    emit = Mock()
    monkeypatch.setattr("mimir.agent.log_event_sync", emit)
    monkeypatch.setattr("mimir.event_logger.log_event_sync", emit)
    expected = build_system_prompt(
        core_blocks=load_core(agent._config.home), memory_index_body="Index body",
        operator_alert_channel="alerts", skill_block=None,
        home_dir=str(agent._config.home), writable_dirs=["memory", "scratch"],
        access_control_enforced=False,
    )
    assert agent._build_system_prompt() == expected
    emit.assert_not_called()
