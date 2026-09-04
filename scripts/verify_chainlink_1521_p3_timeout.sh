#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_profiles.py
uv run pytest -q tests/test_acp_bootstrap.py
uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_acp_ssh.py
uv run pytest -q tests/test_acp_hosted.py
uv run pytest -q
