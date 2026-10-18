"""Operator-only Cultivate status from durable, server-owned run records."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mimir.agent import _render_plugin_prompt
from mimir.models import AgentEvent, AuthContext, TurnInteractivity
from mimir.plugins.registry import PolicyRegistry
from mimir.plugins.manager import load_plugins
from mimir.prompts import build_turn_prompt
from mimir_cultivate.prompts import coding_status_applies, register_prompt_provider
from mimir_cultivate.prompts import coding_review_applies
from mimir_cultivate.worklink.factory_state import (
    FactoryRunRecord, FactoryStatus, save_factory_record,
)


def _operator() -> AuthContext:
    return AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="user_message", channel_id="chat",
        interactivity=TurnInteractivity.INTERACTIVE, domain="channel", resource_id="chat",
    )


def _record(home: Path, issue: int, *, phase: str = "running", status: str = "running",
            url: str | None = None) -> None:
    run_id = f"chainlink-{issue}"
    sandbox = home / ".factory-sandboxes" / run_id
    save_factory_record(home, FactoryRunRecord(
        run_id=run_id, issue_id=issue, attempt=1, repository="owner/repo", base_ref="main",
        branch=f"build-{issue}", launcher=str(home / "launcher"), sandbox=str(sandbox),
        session=None, handle=None, observed_at=None, controller_phase=phase,
        status=FactoryStatus(run_id=run_id, valid=True, sandbox_path=str(sandbox),
                             status=status, pr_url=url),
    ))


def test_selected_provider_renders_only_server_owned_in_flight_and_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    provider = registry.prompt_providers["coding_status"]
    event = AgentEvent(trigger="user_message", channel_id="chat", content="status?")
    assert provider.owner == "cultivate" and provider.section == "self_state"
    assert coding_status_applies(_operator(), event)
    text, served = _render_plugin_prompt(provider, _operator(), event, tmp_path)
    assert served == ()  # no agent-writable file was read through the prompt reader
    assert "Worklink build #1949" in text
    assert "https://github.com/owner/repo/pull/50" in text
    assert "#1951" not in text and "/pull/51" not in text
    assert "/pull/52" not in text and "evil.example" not in text
    assert "/pull/54" not in text
    prompt = build_turn_prompt(event, plugin_blocks={"self_state": (("coding_status", text),)})
    assert "## Plugin: coding_status\n\nCoding status" in prompt


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


def test_coding_status_empty_and_unconfigured_home(tmp_path, monkeypatch):
    registry = PolicyRegistry()
    register_prompt_provider(registry.registrar("cultivate"))
    provider = registry.prompt_providers["coding_status"]
    event = AgentEvent(trigger="user_message", channel_id="chat")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert _render_plugin_prompt(provider, _operator(), event, tmp_path) == ("", ())
    monkeypatch.delenv("MIMIR_HOME")
    assert _render_plugin_prompt(provider, _operator(), event, tmp_path) == ("", ())


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
