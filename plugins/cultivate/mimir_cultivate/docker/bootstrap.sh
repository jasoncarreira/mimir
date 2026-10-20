#!/bin/sh
set -eu
if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then
    mimir opencode-bootstrap --home /home/mimir
fi
