# mimir agent + saga (in-process) deployment image.
#
# Single-process build: mimir's pre-message hook calls saga.core directly
# (workspace dep), so there's no separate saga sidecar. claude-agent-sdk
# spawns Claude Code as a subprocess per turn, so we install the Claude
# Code CLI alongside mimir.
#
# ── Auth: pick ONE of these at runtime via -e or compose env: ─────────
#   A. Max plan (free with subscription):
#        Run `claude setup-token` ON THE HOST first, mount the credential
#        file into the container at runtime. macOS hosts: keychain isn't
#        portable; copy the token blob via `security find-generic-password`
#        and pass through CLAUDE_CODE_OAUTH_TOKEN. Linux hosts: mount
#        ~/.claude/credentials into the container at /home/mimir/.claude/.
#   B. Anthropic API key:
#        -e ANTHROPIC_API_KEY=sk-ant-...
#   C. Gateway (LiteLLM, OpenRouter, internal proxy):
#        -e ANTHROPIC_BASE_URL=https://your-gateway/
#        -e ANTHROPIC_AUTH_TOKEN=...
#        -e ANTHROPIC_MODEL=claude-haiku-4-5  (or gateway-equivalent name)
# ─────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# Claude Code CLI (the SDK transport) ships via npm; install Node + the
# CLI globally. Pin the major to keep image rebuilds deterministic;
# dependabot can bump the minor on its own cadence.
#
# Also installs PDF-ingest tooling used by mimir's reading-queue
# pipeline:
#   - poppler-utils: pdftotext / pdftoppm for PDFs with a text layer
#   - tesseract-ocr: OCR fallback for scanned (image-only) PDFs via
#     `pdftoppm -r 300 file.pdf out && tesseract out-*.ppm out`
#   - tesseract-ocr-eng: English language pack. tesseract-ocr's
#     control file declares `Depends: tesseract-ocr-eng |
#     tesseract-ocr-osd` — an OR-relationship that APT can satisfy
#     with `osd` alone (orientation detection only, no text output)
#     under `--no-install-recommends`. Pinning eng explicitly removes
#     that ambiguity so a clean rebuild always produces an English-
#     capable OCR install.
ENV NODE_VERSION=20
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        poppler-utils tesseract-ocr tesseract-ocr-eng \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uv: fast package manager, used to install mimir + saga workspace.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Non-root user. claude-agent-sdk needs HOME writable (Claude Code stores
# state under $HOME/.claude/). Volume mounts go to /home/mimir/.
RUN useradd -m -u 1001 -s /bin/bash mimir
USER mimir
WORKDIR /home/mimir/app

# Build the workspace. Copy pyproject + uv.lock first so the layer caches
# unless deps change. Then copy the source.
COPY --chown=mimir:mimir pyproject.toml uv.lock ./
COPY --chown=mimir:mimir saga/pyproject.toml ./saga/pyproject.toml
RUN uv sync --frozen --no-dev

COPY --chown=mimir:mimir mimir/ ./mimir/
COPY --chown=mimir:mimir saga/saga/ ./saga/saga/
COPY --chown=mimir:mimir benchmarks/ ./benchmarks/

# Pre-warm the fastembed cache so the first request doesn't pay the
# ~80MB download. Skipped silently if offline at build time.
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

# Default agent home — overridden by volume mount typically.
ENV MIMIR_HOME=/home/mimir/agent
RUN mkdir -p /home/mimir/agent && uv run mimir setup --home /home/mimir/agent || true

# Web UI + /event endpoint.
ENV MIMIR_WEB_PORT=8080
EXPOSE 8080

# Persistent volumes:
#   /home/mimir/agent      — agent home (memory/, state/, logs/, .mimir/saga.db)
#   /home/mimir/.claude    — Claude Code session credential (Max plan path)
#   /home/mimir/.cache     — fastembed model cache
VOLUME ["/home/mimir/agent", "/home/mimir/.claude", "/home/mimir/.cache"]

CMD ["uv", "run", "mimir", "run", "--home", "/home/mimir/agent"]
