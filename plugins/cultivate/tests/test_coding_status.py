"""Operator-only Cultivate status from durable, server-owned run records."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from mimir.agent import Agent, _render_plugin_prompt
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import (
    AgentEvent, AuthContext, InformationFlowLabels, Integrity, IntegrityEffect,
    TurnContext, TurnInteractivity,
)
from mimir.plugins.core import policy_scope
from mimir.plugins.registry import PolicyRegistry
from mimir.plugins.manager import load_plugins
from mimir.prompts import build_turn_prompt
from mimir_cultivate.policy import RepoPRActionScope, ServerDiscoveredPRScopeStore
from mimir_cultivate.prompts import coding_review_applies, coding_status_applies, register_prompt_provider
from mimir_cultivate.worklink.factory_state import (
    FactoryRunRecord, FactoryStatus, save_factory_record,
)
from mimir.turn_logger import TurnLogger


def _operator(store: ServerDiscoveredPRScopeStore | None = None) -> AuthContext:
    return AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="user_message", channel_id="chat",
        interactivity=TurnInteractivity.INTERACTIVE, domain="channel", resource_id="chat",
        server_discovered_pr_scope_store=store,
    )


def _remediation_scope(number: int, *, event_type: str = "pr_ci_failure") -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="server_discovered", canonical_repo="owner/repo",
        canonical_root="/tmp/checkout", canonical_origin="https://github.com/owner/repo.git",
        principal="mimir-bot", event_type=event_type,
        allowed_operations=frozenset({"repo.inspect"}), pr_number=number,
        head_repo="owner/repo", head_remote="origin", destination_ref="refs/heads/fix",
        observed_head_sha="a" * 40, base_ref="main", observed_base_sha="b" * 40,
    )


def _record(home: Path, issue: int, *, phase: str = "running", status: str = "running",
            url: str | None = None, attempt: int = 1) -> None:
    run_id = f"chainlink-{issue}"
    sandbox = home / ".factory-sandboxes" / run_id
    save_factory_record(home, FactoryRunRecord(
        run_id=run_id, issue_id=issue, attempt=attempt, repository="owner/repo", base_ref="main",
        branch=f"build-{issue}", launcher=str(home / "launcher"), sandbox=str(sandbox),
        session=None, handle=None, observed_at=None, controller_phase=phase,
        status=FactoryStatus(run_id=run_id, valid=True, sandbox_path=str(sandbox),
                             status=status, pr_url=url),
    ))


def test_selected_provider_renders_only_server_owned_in_flight_and_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "wrong-home"))
    _record(tmp_path, 1949)
    _record(tmp_path, 1950, phase="parked", status="needs-human",
            url="https://github.com/owner/repo/pull/50")
    _record(tmp_path, 1951, phase="terminal", status="completed",
            url="https://github.com/owner/repo/pull/51")
    _record(tmp_path, 1952, phase="running", status="running",
            url="https://github.com/owner/repo/pull/52")
    _record(tmp_path, 1953, phase="parked", status="needs-human",
            url="https://evil.example/secret")
    _record(tmp_path, 1954, phase="parked", status="needs-human",
            url="https://github.com/unrelated/repo/pull/54")
    _record(tmp_path, 1955, phase="running", status="needs-human",
            url="https://github.com/owner/repo/pull/55")
    _record(tmp_path, 1956, phase="parked", status="needs-human",
            url="https://github.com/owner/repo/pull/56")
    store = ServerDiscoveredPRScopeStore()
    store.remember_server_discovery(_remediation_scope(50))
    store.remember_server_discovery(_remediation_scope(56, event_type="pr_review"))
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    provider = registry.prompt_providers["coding_status"]
    event = AgentEvent(trigger="user_message", channel_id="chat", content="status?")
    assert provider.owner == "cultivate" and provider.section == "self_state"
    assert coding_status_applies(_operator(store), event)
    text, served = _render_plugin_prompt(provider, _operator(store), event, tmp_path)
    assert served == ()  # no agent-writable file was read through the prompt reader
    assert text.count("- Worklink build in flight (attempt 1)") == 2
    assert "PR under remediation: https://github.com/owner/repo/pull/50" in text
    assert "#1949" not in text and "chainlink 1949" not in text.lower()
    assert "#1950" not in text and "chainlink 1950" not in text.lower()
    assert "https://github.com/owner/repo/pull/50" in text
    assert "#1951" not in text and "/pull/51" not in text
    assert "/pull/52" not in text and "evil.example" not in text
    assert "/pull/54" not in text and "/pull/55" not in text and "/pull/56" not in text
    prompt = build_turn_prompt(event, plugin_blocks={"self_state": (("coding_status", text),)})
    assert "## Plugin: coding_status\n\nCoding status" in prompt


def test_running_record_without_title_stays_visible_without_tracker_identity(tmp_path: Path) -> None:
    _record(tmp_path, 2064, attempt=3)
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    event = AgentEvent(trigger="user_message", channel_id="chat")
    text, _ = _render_plugin_prompt(
        registry.prompt_providers["coding_status"], _operator(), event, tmp_path,
    )
    assert text == "Coding status\n- Worklink build in flight (attempt 3)"
    assert "2064" not in text and "chainlink" not in text.lower()


@pytest.mark.parametrize("auth,event", [
    (replace(_operator(), roles=("user",)), AgentEvent(trigger="user_message", channel_id="chat")),
    (replace(_operator(), is_service=True), AgentEvent(trigger="user_message", channel_id="chat")),
    (replace(_operator(), interactivity=TurnInteractivity.NON_INTERACTIVE), AgentEvent(trigger="user_message", channel_id="chat")),
    (replace(_operator(), trigger="poller"), AgentEvent(trigger="user_message", channel_id="chat")),
    (_operator(), AgentEvent(trigger="poller", channel_id="poller:github-activity")),
])
def test_coding_status_never_reads_for_non_operator_turns(auth, event, monkeypatch, tmp_path):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _record(tmp_path, 1949)
    assert not coding_status_applies(auth, event)
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    text, resources = _render_plugin_prompt(registry.prompt_providers["coding_status"], auth, event, tmp_path)
    assert text == "" and resources == ()


def test_coding_status_uses_host_home_not_environment(tmp_path, monkeypatch):
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    provider = registry.prompt_providers["coding_status"]
    event = AgentEvent(trigger="user_message", channel_id="chat")
    _record(tmp_path, 1949)
    other = tmp_path / "other"
    _record(other, 5555, attempt=9)
    monkeypatch.setenv("MIMIR_HOME", str(other))
    text, _ = _render_plugin_prompt(provider, _operator(), event, tmp_path)
    assert "Worklink build in flight (attempt 1)" in text and "attempt 9" not in text
    monkeypatch.delenv("MIMIR_HOME")
    assert "Worklink build in flight (attempt 1)" in _render_plugin_prompt(provider, _operator(), event, tmp_path)[0]
    assert _render_plugin_prompt(provider, _operator(), event, tmp_path / "absent") == ("", ())


def test_coding_status_is_registered_only_for_selected_provider():
    assert load_plugins(enable="cultivate").policy.prompt_providers["coding_status"].owner == "cultivate"
    assert "coding_status" not in load_plugins(enable="").policy.prompt_providers


def test_disabled_selection_does_not_import_cultivate():
    script = (
        "import sys; from mimir.plugins.manager import load_plugins; "
        "p = load_plugins(enable='').policy; "
        "assert 'coding_status' not in p.prompt_providers; "
        "assert not any(k == 'mimir_cultivate' or k.startswith('mimir_cultivate.') "
        "for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "MIMIR_PLUGINS": ""}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_selected_review_guidance_is_only_for_authenticated_service_poller(tmp_path):
    policy = load_plugins(enable="cultivate").policy
    provider = policy.prompt_providers["coding_review_guidance"]
    event = AgentEvent(trigger="poller", channel_id="poller:github-activity")
    auth = replace(_operator(), is_service=True, trigger="poller")
    assert coding_review_applies(auth, event)
    text, served = _render_plugin_prompt(provider, auth, event, tmp_path)
    assert "Complete review work in this turn" in text
    assert "approve_declassification" in text and "`pr_spec`" in text
    assert served == ()
    assert not coding_review_applies(_operator(), event)


@pytest.mark.asyncio
async def test_selected_agent_assembly_labels_status_and_excludes_non_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    config = Config.from_env()
    (config.home / "logs").mkdir(exist_ok=True)
    agent = Agent(
        config=config, turn_logger=TurnLogger(config.turns_log),
        message_buffer=MessageBuffer(history_path=config.home / "messages.jsonl"),
        index_generator=IndexGenerator(config.home),
    )
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "wrong-home"))
    _record(config.home, 1949)
    policy = load_plugins(enable="cultivate").policy
    event = AgentEvent(trigger="user_message", channel_id="chat", author="operator", content="status?")

    def turn(auth: AuthContext) -> TurnContext:
        return TurnContext(
            turn_id="coding-status-turn", session_id="chat", trigger="user_message",
            channel_id="chat", started_at=time.monotonic(), auth_context=auth,
            ifc_labels=InformationFlowLabels(),
        )

    with policy_scope(policy):
        operator = turn(_operator())
        prompt, _ = await agent._build_turn_prompt(operator, event, None)
        ordinary = turn(replace(_operator(), roles=("user",)))
        ordinary_prompt, _ = await agent._build_turn_prompt(ordinary, event, None)

    assert "## Plugin: coding_status\n\nCoding status" in prompt
    assert "Worklink build in flight (attempt 1)" in prompt
    assert "#1949" not in prompt and "chainlink 1949" not in prompt.lower()
    sources = [source for source in operator.ifc_labels.sources if source.domain == "coding_status"]
    assert len(sources) == 1
    source = sources[0]
    assert source.resource_id == "plugin:cultivate:coding_status"
    assert source.principal == "operator"
    assert source.authorized_principals == frozenset({"operator"})
    assert source.sensitivity == "private" and source.source_kind == "agent_self"
    assert source.integrity == Integrity.TRUSTED
    assert source.integrity_effect == IntegrityEffect.INFORMATIONAL
    assert "Plugin: coding_status" not in ordinary_prompt
    assert all(source.domain != "coding_status" for source in ordinary.ifc_labels.sources)
