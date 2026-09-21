#!/bin/sh
set -eu

usage() {
    echo "usage: scripts/test_feature_factory_slice.sh tests/test_<module>.py [...]" >&2
    exit 64
}

if [ "$#" -eq 0 ]; then
    usage
fi

for test_module in "$@"; do
    case "$test_module" in
        tests/test_*.py)
            ;;
        *)
            usage
            ;;
    esac

    module_name=${test_module#tests/test_}
    module_name=${module_name%.py}
    case "$module_name" in
        ""|*[!A-Za-z0-9_]*)
            usage
            ;;
    esac

    if [ ! -f "$test_module" ] || [ -L "$test_module" ]; then
        usage
    fi
done

for test_module in "$@"; do
    env -u MIMIR_ACCESS_CONTROL_ENFORCED uv run --extra dev --extra bench pytest -q -n 6 "$test_module"
done

env -u MIMIR_ACCESS_CONTROL_ENFORCED uv run --extra dev --extra bench pytest -q -n 6
env MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run --extra dev --extra bench pytest -q -n 6
