#!/usr/bin/env bash
# Run the read-only gog operations used by Gmail poller turns with the same
# account, credential home, and user-local binary path as the deployment.
set -euo pipefail

: "${GOG_ACCOUNT:?run-gog.sh: GOG_ACCOUNT is required}"

case "${1:-} ${2:-} ${3:-}" in
  "gmail messages search") SUBCOMMAND=(gmail messages search); PREFIX_COUNT=3 ;;
  "auth list ") SUBCOMMAND=(auth list); PREFIX_COUNT=2 ;;
  *) echo "run-gog.sh: unsupported subcommand" >&2; exit 2 ;;
esac
for ((I = 0; I < PREFIX_COUNT; I++)); do shift; done

EXPECT_VALUE=""
ACCOUNT_VALUE=""
for ARG in "$@"; do
  if [[ -n "$EXPECT_VALUE" ]]; then
    if [[ "$EXPECT_VALUE" == "--account" ]]; then ACCOUNT_VALUE="$ARG"; fi
    EXPECT_VALUE=""
    continue
  fi
  case "${SUBCOMMAND[0]}:$ARG" in
    gmail:--account|gmail:--max) EXPECT_VALUE="$ARG" ;;
    gmail:--json|gmail:--no-input) ;;
    *:-*) echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2 ;;
  esac
done
if [[ -n "$EXPECT_VALUE" ]]; then
  echo "run-gog.sh: missing value for $EXPECT_VALUE" >&2
  exit 2
fi
if [[ -n "$ACCOUNT_VALUE" && "$ACCOUNT_VALUE" != "$GOG_ACCOUNT" ]]; then
  echo "run-gog.sh: --account does not match the declared account" >&2
  exit 2
fi

export GOG_ACCOUNT
export GOG_HOME="$HOME/.local/share/gog"
export PATH="$HOME/.local/bin:$PATH"
exec /usr/local/bin/gog "${SUBCOMMAND[@]}" "$@"
