#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_acp_ssh.py
uv run pytest -q tests/test_acp_sdk_contract.py
uv run pytest -q tests/test_acp_dependency_closure.py
uv run pytest -q
