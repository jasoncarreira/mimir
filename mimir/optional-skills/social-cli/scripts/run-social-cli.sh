#!/usr/bin/env bash
# Run the read/dispatch social-cli operations used by poller turns with the
# state directory and Python package path they otherwise have to shell-prefix.
set -euo pipefail

POLLER_NAME="${1:?usage: run-social-cli.sh <poller> <count|dispatch> [args...]}"
case "$POLLER_NAME" in
  social-cli-feed|social-cli-notifications) ;;
  *) echo "run-social-cli.sh: unsupported poller" >&2; exit 2 ;;
esac
shift
STATE_DIR="${MIMIR_HOME:-/mimir-home}/state/pollers/$POLLER_NAME"

SUBCOMMAND="${1:-}"
case "$SUBCOMMAND" in
  count|dispatch) ;;
  *) echo "run-social-cli.sh: unsupported subcommand" >&2; exit 2 ;;
esac
shift

EXPECT_VALUE=""
for ARG in "$@"; do
  if [[ -n "$EXPECT_VALUE" ]]; then
    EXPECT_VALUE=""
    continue
  fi
  case "$SUBCOMMAND:$ARG" in
    count:--platform|count:--action|count:--since|dispatch:--platform) EXPECT_VALUE="$ARG" ;;
    count:--json|dispatch:--dry-run) ;;
    *:-*) echo "run-social-cli.sh: unsupported option: $ARG" >&2; exit 2 ;;
  esac
done
if [[ -n "$EXPECT_VALUE" ]]; then
  echo "run-social-cli.sh: missing value for $EXPECT_VALUE" >&2
  exit 2
fi

export STATE_DIR
export PYTHONPATH="/home/mimir/venv/lib/python3.11/site-packages"
cd "$STATE_DIR"
exec /usr/local/bin/social-cli "$SUBCOMMAND" "$@"
