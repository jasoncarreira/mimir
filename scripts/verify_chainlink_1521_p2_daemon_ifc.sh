#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_sdk_contract.py
uv run pytest -q tests/test_acp_sessions.py
uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_client_provider.py
uv run pytest -q tests/test_access_control.py
uv run pytest -q
