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
#   - git: required when the build adds Claude Code subprocess
#     support — ``langchain-claude-code`` is pinned to a git ref
#     until upstream PRs land (see issue #268). The fork isn't a
#     mimir-agent extra (PyPI rejects direct git URLs), so it ships
#     as an EXTRA build step that builds add explicitly (see the
#     ``MIMIR_ENABLE_CLAUDE_CODE`` block below).
#   - jq: JSON/JSONL parsing relied on by pollers, skill bodies, and
#     operational/debugging shell workflows. Kept in parity with the
#     scaffold-generated image (scaffold_docker.py), which already ships
#     jq, so clean rebuilds of this image keep the same capability (#560).
#   - nodejs + npm: Node runtime/tooling. The Claude Code CLI is installed
#     only when ``MIMIR_ENABLE_CLAUDE_CODE=1`` (same gate as the Python
#     subprocess provider below).
#   - poppler-utils, tesseract-ocr, tesseract-ocr-eng: PDF-ingest
#     toolchain used by mimir's reading-queue pipeline. Tesseract's
#     control file declares ``Depends: tesseract-ocr-eng |
#     tesseract-ocr-osd`` (an OR-relation APT can satisfy with osd
#     alone — orientation detection only). Pinning ``eng`` explicitly
#     removes that ambiguity.
ENV NODE_VERSION=22
ARG MIMIR_ENABLE_CLAUDE_CODE=0
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq ripgrep xz-utils \
        poppler-utils tesseract-ocr tesseract-ocr-eng \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && if [ "$MIMIR_ENABLE_CLAUDE_CODE" = "1" ]; then \
        npm install -g @anthropic-ai/claude-code@2.1.185 ; \
    fi \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay (PID 1 + process supervisor). Supersedes tini — it does the same
# zombie-reaping + signal-forwarding AND supervises multiple services: here the
# agent plus an in-container liveness watcher (deploy/s6-overlay/,
# docs/watchdog.md). If the agent crashes or the watcher SIGKILLs a wedge, s6
# restarts that service in place — no full-container restart. TARGETARCH is set
# by BuildKit (amd64 / arm64 → s6's x86_64 / aarch64 tarball names).
ARG S6_OVERLAY_VERSION=3.2.0.2
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) S6_ARCH=x86_64 ;; \
        arm64) S6_ARCH=aarch64 ;; \
        *)     S6_ARCH=x86_64 ;; \
    esac; \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"; \
    curl -fsSL "${base}/s6-overlay-noarch.tar.xz"      -o /tmp/s6-noarch.tar.xz; \
    curl -fsSL "${base}/s6-overlay-${S6_ARCH}.tar.xz"  -o /tmp/s6-arch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-arch.tar.xz; \
    rm -f /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz

# Non-root user. The mimir process needs to own its venv so the
# pending-update flag flow (``mimir/update_on_start.py``) can
# ``pip install --upgrade mimir-agent`` from inside the container
# without escalating to root. ``/home/mimir/`` is also where the
# Claude Code CLI keeps its OAuth credential under ``.claude/``.
RUN useradd -m -u 1001 -s /bin/bash mimir
USER mimir
# Land ``docker exec -it <ctn> bash`` at a predictable home dir.
# Docker's default of ``/`` is technically fine but operators
# dropping into the container expect to be near the state. Per
# mimir-carreira review note on PR #331.
WORKDIR /home/mimir

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
#   docker build --build-arg MIMIR_EXTRAS=anthropic,discord .
#
# Available extras (see pyproject.toml):
#   anthropic, openai, codex-plus               (model providers)
#   discord, slack                              (bridges)
#   mcp                                         (Model Context Protocol)
#
# Note: there is NO ``claude-code`` extra. ``langchain-claude-code``
# is a git-pinned fork (PyPI rejects packages with direct URL deps),
# so it installs as an extra build step gated on
# ``MIMIR_ENABLE_CLAUDE_CODE``. See the block below.
ARG MIMIR_EXTRAS="anthropic,discord,slack,mcp"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"

# Optional: install the Claude Code subprocess provider. Set
# ``--build-arg MIMIR_ENABLE_CLAUDE_CODE=1`` to enable. Pinned to a
# specific fork SHA; bump when upstream patches merge (issue #268).
ARG LANGCHAIN_CLAUDE_CODE_REF=c723d702dfac1ff6e2b22b8bde661cb17a17b0de
RUN if [ "$MIMIR_ENABLE_CLAUDE_CODE" = "1" ]; then \
        pip install --no-cache-dir \
            "langchain-claude-code @ git+https://github.com/jasoncarreira/langchain-claude-code@${LANGCHAIN_CLAUDE_CODE_REF}" ; \
    fi

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

# Container liveness probe → the in-process /health endpoint, which returns
# {"ok": true} only while the event loop is responsive — so a *timeout* here
# catches a wedged loop, not just a dead process. /health is auth-exempt, so
# no API key is needed. start-period covers cold boot + fastembed warm-up.
#
# NOTE: `restart: unless-stopped` does NOT act on health status — Docker only
# restarts on process *exit*, never on `unhealthy`. To turn an `unhealthy`
# result into a restart, run an autoheal sidecar (e.g. willfarrell/autoheal)
# or a Swarm / k8s liveness probe. See docs/watchdog.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MIMIR_WEB_PORT:-8080}/health" || exit 1

# s6 service definitions: the agent + the in-container liveness watcher, both
# supervised by s6. COPY/chmod run as root (the prior mimir-user build steps are
# done); /init must start as root to set up /run, then each service drops to the
# `mimir` user via s6-setuidgid (see the run scripts). No CMD — the s6 ``user``
# bundle defines what runs.
USER root
COPY deploy/s6-overlay/s6-rc.d/ /etc/s6-overlay/s6-rc.d/
RUN chmod +x /etc/s6-overlay/s6-rc.d/mimir/run /etc/s6-overlay/s6-rc.d/watchdog/run
ENTRYPOINT ["/init"]
