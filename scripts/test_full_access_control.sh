#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: scripts/test_full_access_control.sh" >&2
    exit 64
fi

uv run pytest -q
env MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run pytest -q --tb=short
