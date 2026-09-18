#!/bin/sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

uv sync --locked --extra dev --extra bench

uv run pytest -q tests/test_worklink_dispatch_failures.py
uv run pytest -q tests/test_worklink_attention.py
uv run pytest -q tests/test_worklink_attention_boundaries.py
uv run pytest -q tests/test_worklink_claims.py
uv run pytest -q tests/test_worklink_claim_reset.py
uv run pytest -q tests/test_worklink_orchestrator.py
uv run pytest -q tests/test_worklink_autonomy.py
uv run pytest -q tests/test_worklink_autonomy_policy.py
uv run pytest -q tests/test_worklink_cli.py
uv run pytest -q tests/test_worklink_factory_state.py
uv run pytest -q tests/test_worklink_factory_supervisor.py
uv run pytest -q tests/test_worklink_feature_factory.py
uv run pytest -q tests/test_worklink_reattach.py
uv run pytest -q tests/test_worklink_stop.py
uv run pytest -q tests/test_worklink_continuation.py
uv run pytest -q tests/test_registry_tools.py
uv run pytest -q tests/test_tool_registry.py
uv run pytest -q tests/test_server.py
uv run pytest -q tests/test_pollers.py
uv run pytest -q tests/test_env_isolation.py

uv run pytest -q \
  tests/test_worklink_dispatch_failures.py \
  tests/test_worklink_attention.py \
  tests/test_worklink_attention_boundaries.py \
  tests/test_worklink_claims.py \
  tests/test_worklink_claim_reset.py \
  tests/test_worklink_orchestrator.py \
  tests/test_worklink_autonomy.py \
  tests/test_worklink_autonomy_policy.py \
  tests/test_worklink_cli.py \
  tests/test_worklink_factory_state.py \
  tests/test_worklink_factory_supervisor.py \
  tests/test_worklink_feature_factory.py \
  tests/test_worklink_reattach.py \
  tests/test_worklink_stop.py \
  tests/test_worklink_continuation.py \
  tests/test_registry_tools.py \
  tests/test_tool_registry.py \
  tests/test_server.py \
  tests/test_pollers.py \
  tests/test_env_isolation.py

uv run pytest -q
env -u MIMIR_ACCESS_CONTROL_ENFORCED uv run --extra dev --extra bench pytest -q -n 6
env MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run --extra dev --extra bench pytest -q -n 6
