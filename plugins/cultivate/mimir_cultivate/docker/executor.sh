#!/bin/sh
set -eu
git check-ref-format "$MIMIR_GIT_REF"
git init /opt/mimir-worklink/source
git -C /opt/mimir-worklink/source remote add origin "$MIMIR_GIT_URL"
git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 origin "$MIMIR_EXECUTOR_COMMIT" \
    || git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 origin "$MIMIR_GIT_REF"
test "$(git -C /opt/mimir-worklink/source rev-parse FETCH_HEAD)" = "$MIMIR_EXECUTOR_COMMIT"
git -C /opt/mimir-worklink/source checkout --detach FETCH_HEAD
test "$(git -C /opt/mimir-worklink/source rev-parse HEAD)" = "$MIMIR_EXECUTOR_COMMIT"
test -z "$(git -C /opt/mimir-worklink/source status --porcelain=v1)"
printf '%s\n' "$MIMIR_EXECUTOR_COMMIT" > /opt/mimir-worklink/executor-source-commit
rm -rf /opt/mimir-worklink/source/.git
python3 -m venv /opt/mimir-worklink/venv
/opt/mimir-worklink/venv/bin/pip install --no-cache-dir --upgrade pip
/opt/mimir-worklink/venv/bin/pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"
/opt/mimir-worklink/venv/bin/pip install --no-cache-dir 'pypdf>=6.16'
/opt/mimir-worklink/venv/bin/pip install --no-cache-dir --no-deps /opt/mimir-worklink/source
/opt/mimir-worklink/venv/bin/pip install --no-cache-dir uv
ln -s /opt/mimir-worklink/venv/bin/uv /usr/local/bin/uv
UV_CACHE_DIR=/opt/mimir-worklink/uv-cache uv sync --directory /opt/mimir-worklink/source \
    --locked --extra dev --extra bench --no-install-workspace
rm -rf /opt/mimir-worklink/source/.venv
rm -rf /opt/mimir-worklink/source
chown -R root:root /opt/mimir-worklink
chmod -R go-w /opt/mimir-worklink
