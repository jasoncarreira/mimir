#!/bin/sh
set -eu

uv run pytest -q tests/test_budget_gate_and_alias.py
uv run pytest -q
