#!/bin/sh
set -eu

uv run pytest -q tests/test_access_control.py
uv run pytest -q tests/test_client_provider.py
uv run pytest -q
