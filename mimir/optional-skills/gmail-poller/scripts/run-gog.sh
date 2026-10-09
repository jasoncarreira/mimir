#!/usr/bin/env bash
# Run the read-only gog operations used by Gmail poller turns with the same
# account, credential home, and user-local binary path as the deployment.
set -euo pipefail

if [[ -z "${GOG_ACCOUNT:-}" ]]; then
  echo "run-gog.sh: GOG_ACCOUNT is required" >&2
  exit 2
fi

if [[ "${1:-}" == gmail && "${2:-}" == messages && "${3:-}" == search ]]; then
  SUBCOMMAND=(gmail messages search)
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
POSITIONALS=0
for ARG in "$@"; do
  if [[ -n "$EXPECT_VALUE" ]]; then
    if [[ "$ARG" == -* || -z "$ARG" ]]; then
      echo "run-gog.sh: invalid value for $EXPECT_VALUE" >&2
      exit 2
    fi
    if [[ "$EXPECT_VALUE" == "--account" && "$ARG" != "$GOG_ACCOUNT" ]]; then
      echo "run-gog.sh: --account does not match the declared account" >&2
      exit 2
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

export GOG_ACCOUNT
export GOG_HOME="$HOME/.local/share/gog"
export PATH="$HOME/.local/bin:$PATH"
exec gog --readonly --gmail-no-send "${SUBCOMMAND[@]}" "$@"
