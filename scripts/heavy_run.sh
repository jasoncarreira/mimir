#!/bin/sh
# Run a heavy command (a full verify or test-suite run) under the host's shared
# heavy-run lock when the host provides one; otherwise run it directly.
#
# A host that also runs other agents' timed measurements and long suites can
# serialize them through one lock file. This wrapper takes that lock with
# `lockf -k` (wait for the lock, hold it for the command's lifetime) under a
# `gtimeout` hang backstop. The timeout covers the time spent waiting for the
# lock as well as the run, so it is sized for a long queue, not for the run.
#
# It runs the command unwrapped when:
# - either tool is missing (Linux containers, CI runners); or
# - MIMIR_HEAVY_LOCK_HELD is set, meaning an ancestor already holds the lock.
#   `lockf` is not re-entrant, so taking it again from a child would wait on
#   the parent forever.
#
# Environment:
#   MIMIR_HEAVY_LOCK_FILE     lock path (default /tmp/trellis-factory-verify.lock)
#   MIMIR_HEAVY_LOCK_TIMEOUT  seconds for gtimeout (default 7200)
#   MIMIR_HEAVY_LOCKF, MIMIR_HEAVY_GTIMEOUT  tool paths (defaults below)
set -eu

if [ "$#" -eq 0 ]; then
    echo "usage: scripts/heavy_run.sh <command> [args...]" >&2
    exit 64
fi

lockf_bin="${MIMIR_HEAVY_LOCKF:-/usr/bin/lockf}"
gtimeout_bin="${MIMIR_HEAVY_GTIMEOUT:-/opt/homebrew/bin/gtimeout}"
lock_file="${MIMIR_HEAVY_LOCK_FILE:-/tmp/trellis-factory-verify.lock}"
timeout_s="${MIMIR_HEAVY_LOCK_TIMEOUT:-7200}"

if [ -n "${MIMIR_HEAVY_LOCK_HELD:-}" ] || [ ! -x "$lockf_bin" ] || [ ! -x "$gtimeout_bin" ]; then
    exec "$@"
fi

MIMIR_HEAVY_LOCK_HELD=1
export MIMIR_HEAVY_LOCK_HELD
exec "$gtimeout_bin" -k 10s "${timeout_s}s" "$lockf_bin" -k "$lock_file" "$@"
