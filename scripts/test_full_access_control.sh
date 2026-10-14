#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: scripts/test_full_access_control.sh" >&2
    exit 64
fi

# Every stage is required; do not substitute a scoped run for either full suite.
env -u MIMIR_ACCESS_CONTROL_ENFORCED -u MIMIR_PLUGINS \
    uv run --locked --extra dev --extra bench pytest -q -n 6
env -u MIMIR_PLUGINS MIMIR_ACCESS_CONTROL_ENFORCED=1 \
    uv run --locked --extra dev --extra bench pytest -q -n 6

if [ -f plugins/cultivate/pyproject.toml ]; then
    env -u MIMIR_ACCESS_CONTROL_ENFORCED MIMIR_PLUGINS=cultivate \
        uv run --locked --all-packages --extra dev --extra bench \
        pytest -q -n 6 tests plugins/cultivate/tests
    env MIMIR_ACCESS_CONTROL_ENFORCED=1 MIMIR_PLUGINS=cultivate \
        uv run --locked --all-packages --extra dev --extra bench \
        pytest -q -n 6 tests plugins/cultivate/tests
fi

for suite in mimir/optional-skills/*/tests \
    plugins/cultivate/mimir_cultivate/defaults/optional-skills/*/tests \
    plugins/cultivate/optional-skills/*/tests; do
    if [ -d "$suite" ]; then
        if [ -f plugins/cultivate/pyproject.toml ]; then
            env -u MIMIR_PLUGINS -u MIMIR_ACCESS_CONTROL_ENFORCED \
                uv run --locked --all-packages --extra dev --extra bench \
                pytest -q -n 6 "$suite"
        else
            env -u MIMIR_PLUGINS -u MIMIR_ACCESS_CONTROL_ENFORCED \
                uv run --locked --extra dev --extra bench pytest -q -n 6 "$suite"
        fi
    fi
done

if [ -f package.json ] && [ -f package-lock.json ]; then
    npm ci
    npm run test
    npm run build
fi
