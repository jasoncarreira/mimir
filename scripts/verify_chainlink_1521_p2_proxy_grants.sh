#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_acp_ssh.py
uv run pytest -q
