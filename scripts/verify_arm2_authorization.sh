#!/bin/sh
set -eu
uv run pytest -q tests/test_access_control.py
uv run pytest -q tests/test_information_flow.py
uv run pytest -q
