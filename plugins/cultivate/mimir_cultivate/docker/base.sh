#!/bin/sh
# factory-version: 0.12.1
set -eu
apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git jq procps ripgrep xz-utils \
    lsof file unzip less make netcat-openbsd iproute2 psmisc \
    poppler-utils tesseract-ocr tesseract-ocr-eng libjemalloc2
curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash -
apt-get install -y --no-install-recommends nodejs
if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then
    npm install --global --prefix /opt/mimir-opencode \
        opencode-ai@1.19.4 \
        @openai/codex@0.139.0 \
        "feature-factory@${FACTORY_VERSION}" \
        "opencode-feature-factory@${FACTORY_VERSION}" \
        opencode-project-memory@0.1.0 \
        opencode-openai-codex-auth@4.4.0 \
        opencode-anthropic-auth@0.0.13
fi
apt-get clean
rm -rf /var/lib/apt/lists/*
