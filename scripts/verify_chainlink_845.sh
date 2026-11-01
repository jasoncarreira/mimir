#!/bin/sh
set -eu

env -u MIMIR_ACCESS_CONTROL_ENFORCED uv run --extra dev --extra bench --extra otel pytest -q -n 6
MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run --extra dev --extra bench --extra otel pytest -q -n 6
env -u MIMIR_ACCESS_CONTROL_ENFORCED MIMIR_PLUGINS=cultivate uv run --extra dev --extra bench --extra otel pytest -q -n 6 tests plugins/cultivate/tests
MIMIR_ACCESS_CONTROL_ENFORCED=1 MIMIR_PLUGINS=cultivate uv run --extra dev --extra bench --extra otel pytest -q -n 6 tests plugins/cultivate/tests
