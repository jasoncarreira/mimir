"""Selected-server callback registration and disabled-provider isolation."""

from __future__ import annotations

import pytest
from aiohttp import web

from mimir.plugins.manager import load_plugins
from mimir.plugins.registry import PolicyRegistrationError
from mimir.server import _capture_controller_source_commit, _handle_health


def test_server_callbacks_belong_to_selected_provider() -> None:
    selected = load_plugins(enable="cultivate").policy
    assert selected.frozen
    assert ("cultivate", "health") in selected.server_hooks
    assert ("cultivate", "shutdown_claims") in selected.server_hooks
    assert all(owner == "cultivate" for owner, _ in selected.server_hooks)

    disabled = load_plugins(enable="").policy
    assert not disabled.server_hooks


def test_selected_process_identity_uses_current_generation(monkeypatch) -> None:
    monkeypatch.delenv("MIMIR_WORKLINK_AGENT_ID", raising=False)
    policy = load_plugins(enable="cultivate").policy
    policy.server_hooks["cultivate", "process_identity"]("session-123")
    import os

    assert os.environ["MIMIR_WORKLINK_AGENT_ID"] == "mimir-worklink:session-123"


def test_server_callback_registration_is_owner_bound() -> None:
    selected = load_plugins(enable="cultivate").policy
    with pytest.raises(PolicyRegistrationError):
        selected.register_server_hook("health", lambda: None, owner="cultivate")


@pytest.mark.asyncio
async def test_disabled_health_does_not_call_worker(monkeypatch) -> None:
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["plugin_manager"] = load_plugins(enable="")
    await _capture_controller_source_commit(app)
    request = make_mocked_request("GET", "/health", app=app)
    response = await _handle_health(request)
    assert response.status == 200
    assert b"worklink_" not in response.body
