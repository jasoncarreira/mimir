# mimir agent deployment image — installs ``mimir-agent`` from PyPI.
#
# Source-build path (workspace ``uv sync`` + local saga copy) is gone.
# Operators who want to run mimir from a checkout can still do so by
# editing the install step below, but the default + supported flow is
# PyPI.
#
# ── Auth: pick ONE of these at runtime via -e or compose env: ─────────
#   A. Max plan (free with subscription, requires the ``claude-code``
#      extra — build with ``--build-arg MIMIR_EXTRAS=claude-code,...``):
#        Run ``claude setup-token`` ON THE HOST first, mount the
#        credential file into the container at runtime. macOS hosts:
#        keychain isn't portable; copy the token blob via
#        ``security find-generic-password`` and pass through
#        ``CLAUDE_CODE_OAUTH_TOKEN``. Linux hosts: mount
#        ``~/.claude/credentials`` into the container at
#        ``/home/mimir/.claude/``.
#   B. Anthropic API key (default build path — uses the ``anthropic``
#      extra):
#        -e ANTHROPIC_API_KEY=sk-ant-...
#   C. Gateway (LiteLLM, OpenRouter, internal proxy):
#        -e ANTHROPIC_BASE_URL=https://your-gateway/
#        -e ANTHROPIC_AUTH_TOKEN=...
#        -e ANTHROPIC_MODEL=claude-haiku-4-5  (or gateway-equivalent name)
# ─────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# OS deps:
#   - ca-certificates curl gnupg: prereqs for adding NodeSource +
#     fetching uv installer
#   - git: required when the ``claude-code`` extra is selected —
#     ``langchain-claude-code`` is pinned to a git ref until upstream
#     PRs land (see chainlink #268). Without git in the image,
#     ``pip install mimir-agent[claude-code]`` fails at the git
#     clone step.
#   - nodejs + npm + @anthropic-ai/claude-code: the Claude Code CLI
#     binary, needed when the deployment routes through the
#     subprocess provider (Max OAuth path).
#   - poppler-utils, tesseract-ocr, tesseract-ocr-eng: PDF-ingest
#     toolchain used by mimir's reading-queue pipeline. Tesseract's
#     control file declares ``Depends: tesseract-ocr-eng |
#     tesseract-ocr-osd`` (an OR-relation APT can satisfy with osd
#     alone — orientation detection only). Pinning ``eng`` explicitly
#     removes that ambiguity.
ENV NODE_VERSION=20
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
        poppler-utils tesseract-ocr tesseract-ocr-eng \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. The mimir process needs to own its venv so the
# pending-update flag flow (``mimir/update_on_start.py``) can
# ``pip install --upgrade mimir-agent`` from inside the container
# without escalating to root. ``/home/mimir/`` is also where the
# Claude Code CLI keeps its OAuth credential under ``.claude/``.
RUN useradd -m -u 1001 -s /bin/bash mimir
USER mimir

# Install mimir-agent into a user-owned venv at ``/home/mimir/venv``.
# This venv is what the pending-update flow targets — the
# ``request_mimir_update`` tool writes a flag; on next restart the
# pre-flight in ``server.main`` runs ``pip install --upgrade
# mimir-agent`` against THIS venv, replaces the old wheel, then
# ``os.execv``'s onto the new code. The user-owned venv is what makes
# that work without root.
ENV VIRTUAL_ENV=/home/mimir/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Extras to install with the package. Default targets the common
# multi-bridge deployment (Anthropic API, Discord, Slack, MCP).
# Override at build time:
#
#   docker build --build-arg MIMIR_EXTRAS=claude-code,anthropic,discord .
#
# Available extras (see pyproject.toml):
#   anthropic, claude-code, openai, codex-plus  (model providers)
#   discord, slack                              (bridges)
#   mcp                                         (Model Context Protocol)
ARG MIMIR_EXTRAS="anthropic,discord,slack,mcp"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"

# Pre-warm the fastembed cache so the first request doesn't pay the
# ~80MB download. Skipped silently if offline at build time.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

# Default agent home — overridden by volume mount typically.
# ``mimir setup`` seeds the directory structure (skills, memory
# scaffolds, scheduler.yaml). Idempotent — re-runs on container
# recreate are no-ops.
ENV MIMIR_HOME=/home/mimir/agent
RUN mkdir -p /home/mimir/agent && mimir setup --home /home/mimir/agent || true

# Auto-update behavior on this image:
#
#   - Daily PyPI poll runs automatically at 08:00 UTC inside the
#     container — emits ``mimir_update_available`` algedonic when
#     a newer mimir-agent version is on PyPI. Surfaces in the
#     per-turn feedback block + the /ops dashboard.
#   - Operator approves an update → agent calls
#     ``request_mimir_update`` tool → flag written to
#     ``/home/mimir/agent/.mimir/pending-update.flag``.
#   - On next ``docker compose restart``, the pre-flight in
#     ``server.main`` runs ``pip install --upgrade`` against the
#     user-owned venv above, then ``os.execv``'s onto the new code.
#   - The daily PyPI poll cron is hardcoded to ``0 8 * * *`` (08:00
#     UTC). To suppress notifications without code changes, override
#     the ``update-check`` entry in ``<MIMIR_HOME>/scheduler.yaml``
#     (the operator-managed schedule file ``mimir setup`` seeds).
#     The auto-install path stays available regardless — only fires
#     when the flag is present, which only the agent writes after
#     operator approval.
#
# See ``mimir/update_on_start.py`` for the full flow rationale.

# Web UI + /event endpoint.
ENV MIMIR_WEB_PORT=8080
EXPOSE 8080

# Persistent volumes:
#   /home/mimir/agent   — agent home (memory/, state/, logs/, .mimir/saga.db,
#                         .mimir/pending-update.flag when set)
#   /home/mimir/.claude — Claude Code session credential (Max plan path)
#   /home/mimir/.cache  — fastembed model cache
VOLUME ["/home/mimir/agent", "/home/mimir/.claude", "/home/mimir/.cache"]

CMD ["mimir", "run", "--home", "/home/mimir/agent"]
