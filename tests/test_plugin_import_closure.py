"""The disabled coding surface must not be imported by core entry points."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from mimir.http_ingress import (
    HTTP_EVENT_INGRESS_EXTRA_VALUE,
    sanitize_http_extra,
    stamp_http_event_ingress_extra,
)
from mimir.access_control import HTTP_EVENT_INGRESS_EXTRA_KEY


FORBIDDEN = (
    "mimir.worklink", "mimir.forge", "mimir.tools.forge", "mimir.tools.repo",
    "mimir.pr_checkout_lease", "mimir.project_tests", "mimir.repo_tools",
    "mimir.contained_execution", "mimir.opencode_config", "mimir.coding",
)


def test_disabled_plugin_has_no_coding_tools_in_runtime_or_policy_tables() -> None:
    # Run in a fresh interpreter: collection of unrelated tests can import the
    # provider, and the retired flag must not be able to reactivate it.
    from tests.test_plugin_policy_parity import CODING_TOOLS

    script = """
import sys
from mimir.plugins.manager import load_plugins
from mimir.plugins.core import core_policy_registry
from mimir.tools.registry import all_mimir_tools
from mimir import access_control

coding = set(sys.argv[1:])
assert len(coding) == 40
selected = load_plugins(enable='')
assert not selected.plugins
assert coding.isdisjoint(selected.policy.tools)
assert coding.isdisjoint(core_policy_registry().tools)
assert coding.isdisjoint(access_control._TOOL_FLOW_MAP)
assert coding.isdisjoint(access_control.TRIGGER_CAPABILITY_TIERS)
assert all(access_control.get_operation_catalog().get_decision(name).value == 'unknown'
           for name in coding)
assert coding.isdisjoint(tool.name for tool in all_mimir_tools(
    model_spec='openai:test', policy=selected.policy,
    require_provider_available=False,
))
assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
               for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *sorted(CODING_TOOLS)],
        env={**os.environ, "MIMIR_PLUGINS": "", "MIMIR_CODING_ENABLED": "true"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("root", (
    "mimir.server", "mimir.agent", "mimir.tools", "mimir.access_control",
))
def test_coding_disabled_import_closure(root: str) -> None:
    script = """
import importlib
import sys
importlib.import_module(sys.argv[1])
forbidden = sys.argv[2:]
loaded = sorted(name for name in sys.modules
                if any(name == prefix or name.startswith(prefix + '.')
                       for prefix in forbidden))
assert not loaded, loaded
"""
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    env["MIMIR_CODING_ENABLED"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", script, root, *FORBIDDEN],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("coding_enabled", [False, True])
def test_runtime_construction_coding_imports(tmp_path, coding_enabled: bool) -> None:
    if coding_enabled:
        pytest.importorskip("mimir_cultivate")
    script = textwrap.dedent("""
        import asyncio
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        from mimir.runtime import CoreServices, RuntimeAdapters, create_agent_runtime
        from unittest.mock import patch
        import mimir.agent
        import mimir.tools

        class Dispatcher:
            _run_turn = None

            def __getattr__(self, name):
                if name.startswith('set_'):
                    return lambda value: setattr(self, '_' + name.removeprefix('set_'), value)
                raise AttributeError(name)

            def is_channel_busy(self, channel_id):
                return False

        class Scheduler:
            _started = False

            def set_arbiter(self, value):
                self._arbiter = value

        class Agent:
            def __init__(self, *args, **kwargs):
                self._arbiter = object()
                self._shell_jobs = object()
                self._handle_shell_job_complete = lambda job: None

            async def run_turn(self, event):
                pass

            async def on_message_injected(self, event):
                pass

        async def main():
            home = Path(sys.argv[1])
            from mimir.plugins.manager import load_plugins
            enabled = sys.argv[2] == '1'
            config = SimpleNamespace(
                home=home,
                plugin_manager=None,
                turns_log=home / 'logs' / 'turns.jsonl', max_turns_kept=100,
                history_global_max=50, history_per_channel_max=25,
                cross_platform_pull=True, saga_session_idle_minutes=10,
                saga_session_max_turns=10,
                commitments_log=home / '.mimir' / 'commitments.jsonl',
                operator_alert_channel='',
            )
            if enabled:
                from mimir_cultivate import forge
                initialized = []
                identity_patch = patch.object(
                    forge, 'initialize_github_forge_identity',
                    side_effect=lambda: initialized.append(True) or True,
                )
                identity_patch.start()
            config.plugin_manager = load_plugins(enable='cultivate' if enabled else '')
            core = CoreServices(
                identity_resolver=SimpleNamespace(), aliases_loaded=0,
                saga_db_path=home / '.mimir' / 'saga.db',
                chat_skill_registry=SimpleNamespace(),
            )
            adapters = RuntimeAdapters(
                dispatcher=Dispatcher(), scheduler=Scheduler(),
                channels=SimpleNamespace(), pairing_notifier=SimpleNamespace(),
                spawn_background_task=lambda coro, name: asyncio.create_task(coro, name=name),
            )
            with patch.object(mimir.agent, 'Agent', Agent), patch.object(
                mimir.tools, 'all_mimir_tools', return_value=[],
            ):
                bundle = await create_agent_runtime(config, core, adapters)
            try:
                forbidden = sys.argv[3:]
                loaded = sorted(name for name in sys.modules
                                if any(name == prefix or name.startswith(prefix + '.')
                                       for prefix in forbidden))
                if enabled:
                    assert not loaded, loaded
                    assert initialized == [True]
                    assert forge._github_identity_degraded_callback is not None
                else:
                    assert not loaded, loaded
                    assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
                                   for name in sys.modules)
            finally:
                await bundle.aclose()
                if enabled:
                    identity_patch.stop()

        asyncio.run(main())
    """)
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    env["MIMIR_HOME"] = str(tmp_path)
    env["MIMIR_CODING_ENABLED"] = "1" if coding_enabled else "0"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(int(coding_enabled)), *FORBIDDEN],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_http_ingress_strips_nested_hints_and_stamps_server_marker() -> None:
    extra = {
        "data": [{"issue_id": 42, "safe": "retained"}],
        HTTP_EVENT_INGRESS_EXTRA_KEY: "spoofed",
    }
    sanitized = sanitize_http_extra(extra)
    assert sanitized == {"data": [{"safe": "retained"}]}
    assert stamp_http_event_ingress_extra(sanitized) == {
        "data": [{"safe": "retained"}],
        HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
    }
    assert extra["data"][0]["issue_id"] == 42
