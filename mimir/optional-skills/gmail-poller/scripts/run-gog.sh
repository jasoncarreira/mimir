#!/usr/bin/env bash
# Run the read-only gog operations used by Gmail poller turns with the same
# account and credential home as the deployment, using its pinned gog binary.
set -euo pipefail

if [[ "${1:-}" == gmail && "${2:-}" == messages && "${3:-}" == search ]]; then
  SUBCOMMAND=(gmail messages search)
  shift 3
elif [[ "${1:-}" == gmail && "${2:-}" == messages && "${3:-}" == get ]]; then
  SUBCOMMAND=(gmail get)
  shift 3
elif [[ "${1:-}" == gmail && "${2:-}" == get ]]; then
  SUBCOMMAND=(gmail get)
  shift 2
elif [[ "${1:-}" == gmail && "${2:-}" == thread && "${3:-}" == get ]]; then
  SUBCOMMAND=(gmail thread get)
  shift 3
elif [[ "${1:-}" == auth && "${2:-}" == list ]]; then
  SUBCOMMAND=(auth list)
  shift 2
else
  echo "run-gog.sh: unsupported subcommand" >&2
  exit 2
fi

EXPECT_VALUE=""
ACCOUNT_PROVIDED=0
ACCOUNT_VALUES=()
POSITIONALS=0
for ARG in "$@"; do
  if [[ -n "$EXPECT_VALUE" ]]; then
    if [[ "$ARG" == -* || -z "$ARG" ]]; then
      echo "run-gog.sh: invalid value for $EXPECT_VALUE" >&2
      exit 2
    fi
    if [[ "$EXPECT_VALUE" == "--account" ]]; then
      if [[ -n "${GOG_ACCOUNT:-}" && "$ARG" != "$GOG_ACCOUNT" ]]; then
        echo "run-gog.sh: --account does not match the declared account" >&2
        exit 2
      fi
      ACCOUNT_PROVIDED=1
      ACCOUNT_VALUES+=("$ARG")
    fi
    if [[ "$EXPECT_VALUE" == "--max" && ! "$ARG" =~ ^[0-9]+$ ]]; then
      echo "run-gog.sh: invalid value for --max" >&2
      exit 2
    fi
    EXPECT_VALUE=""
    continue
  fi
  case "$ARG" in
    --account) [[ "${SUBCOMMAND[0]}" == gmail ]] || { echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2; }; EXPECT_VALUE="$ARG" ;;
    --max) [[ "${SUBCOMMAND[2]:-}" == search ]] || { echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2; }; EXPECT_VALUE="$ARG" ;;
    --json|--no-input) [[ "${SUBCOMMAND[0]}" == gmail ]] || { echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2; } ;;
    --full) [[ "${SUBCOMMAND[1]}" == get || "${SUBCOMMAND[2]:-}" == get ]] || { echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2; } ;;
    -*) echo "run-gog.sh: unsupported option: $ARG" >&2; exit 2 ;;
    *)
      if [[ "${SUBCOMMAND[0]}" == auth ]]; then
        echo "run-gog.sh: unexpected positional argument" >&2
        exit 2
      fi
      ((POSITIONALS += 1))
      if [[ "${SUBCOMMAND[2]:-}" != search && ! "$ARG" =~ ^[A-Za-z0-9_-]+$ ]]; then
        echo "run-gog.sh: invalid message or thread ID" >&2
        exit 2
      fi
      ;;
  esac
done
if [[ -n "$EXPECT_VALUE" ]]; then
  echo "run-gog.sh: missing value for $EXPECT_VALUE" >&2
  exit 2
fi
if [[ "${SUBCOMMAND[0]}" == gmail && "$POSITIONALS" -ne 1 ]]; then
  echo "run-gog.sh: expected exactly one positional argument" >&2
  exit 2
fi

if [[ "${SUBCOMMAND[0]}" == gmail ]]; then
  if [[ -n "${GOG_ACCOUNT:-}" ]]; then
    if [[ "$ACCOUNT_PROVIDED" -eq 0 ]]; then
      set -- "$@" --account "$GOG_ACCOUNT"
    fi
  else
    # The poller stores account config in STATE_DIR; turns use the same
    # persistent location under MIMIR_HOME when STATE_DIR is not injected.
    CONFIG_DIR="${STATE_DIR:-${MIMIR_HOME:-}/state/pollers/gmail-inbox}"
    /usr/bin/python3 - "$CONFIG_DIR/config.json" "${ACCOUNT_VALUES[@]}" <<'PY' || exit 2
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as config_file:
        data = json.load(config_file)
    entries = data["accounts"]
    if not isinstance(entries, list):
        raise ValueError("invalid accounts")
    accounts = {
        entry["email"].strip(): entry["name"].strip()
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("email"), str) and entry["email"].strip()
        and isinstance(entry.get("name"), str) and entry["name"].strip()
    }
except (OSError, UnicodeError, ValueError, TypeError, KeyError):
    accounts = {}

if not sys.argv[2:] or any(account not in accounts for account in sys.argv[2:]):
    names = ", ".join(
        name if "@" not in name else "(unnamed)" for name in accounts.values()
    ) or "(none)"
    print(f"run-gog.sh: pass --account for a configured account name: {names}", file=sys.stderr)
    sys.exit(2)
PY
  fi
fi

if [[ -n "${GOG_ACCOUNT:-}" ]]; then
  export GOG_ACCOUNT
fi
export GOG_HOME="$HOME/.local/share/gog"
exec /usr/local/bin/gog --readonly --gmail-no-send "${SUBCOMMAND[@]}" "$@"
