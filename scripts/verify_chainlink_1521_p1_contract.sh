#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_dependency_closure.py
uv run pytest -q tests/test_acp_hands_contract.py
uv run pytest -q tests/test_acp_packaging.py
uv run pytest -q tests/test_acp_sessions.py
uv run pytest -q tests/test_client_provider.py
uv run pytest -q
