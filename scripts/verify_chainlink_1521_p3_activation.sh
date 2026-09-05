#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_hands_contract.py
uv run pytest -q tests/test_acp_hosted.py
uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_acp_sdk_contract.py
uv run pytest -q tests/test_acp_sessions.py
uv run pytest -q tests/test_client_provider.py
uv run pytest -q tests/test_prohibited_action_guard.py
uv run pytest -q tests/test_tool_registry.py
uv run pytest -q tests/test_acp_registry.py
uv run pytest -q tests/test_access_control.py
uv run pytest -q tests/test_information_flow.py
uv run pytest -q
