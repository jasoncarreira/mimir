#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_dependency_closure.py
uv run pytest -q tests/test_acp_hosted.py
uv run pytest -q tests/test_acp_packaging.py
uv run pytest -q
