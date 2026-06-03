#!/usr/bin/env bash
# Convenience wrapper: dispatch a poller's social-cli outbox from the correct
# STATE_DIR with its credentials loaded.
#
# social-cli reads ATPROTO_HANDLE / X_API_KEY / ... from the environment and its
# outbox.yaml from the working directory. When dispatch is invoked from outside
# the poller's STATE_DIR — e.g. an agent Bash call that doesn't cd first, or a
# context where the .env in cwd isn't auto-sourced — neither the credentials nor
# the outbox are found and the dispatch silently no-ops or errors. This wrapper
# cds into STATE_DIR, sources its .env if present, and runs dispatch there.
#
# Usage: dispatch-outbox.sh <state_dir> [<platform>]
#   state_dir : the poller's STATE_DIR (holds .env + outbox.yaml)
#   platform  : optional — bsky | x. Omit to dispatch all configured platforms.
#
# Honors SOCIAL_CLI_BIN (same override the pollers use) to locate the binary.
set -euo pipefail

STATE_DIR="${1:?usage: dispatch-outbox.sh <state_dir> [<platform>]}"
PLATFORM="${2:-}"
BIN="${SOCIAL_CLI_BIN:-social-cli}"

export STATE_DIR
cd "$STATE_DIR" || { echo "ERROR: cannot cd to $STATE_DIR" >&2; exit 1; }

# Load .env credentials if present (mode 600 — sourced directly). social-cli
# reads the handle/keys from the environment.
if [ -f "$STATE_DIR/.env" ]; then
  set -a
  . "$STATE_DIR/.env"
  set +a
fi

if [ -n "$PLATFORM" ]; then
  exec "$BIN" dispatch --platform "$PLATFORM"
else
  exec "$BIN" dispatch
fi
