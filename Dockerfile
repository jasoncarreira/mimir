# mimir agent deployment image — installs ``mimir-agent`` from PyPI.
#
# Source-build path (workspace ``uv sync`` + local saga copy) is gone.
# Operators who want to run mimir from a checkout can still do so by
# editing the install step below, but the default + supported flow is
# PyPI.
#
# ── Auth: pick ONE of these at runtime via -e or compose env: ─────────
#   A. Max plan (subscription, requires the ``claude-code`` extra and
#      an operator-provided Claude Code CLI):
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

FROM python:3.11-slim AS provenance-validation

# Every canonical image must identify one remote ref and the exact commit it is
# expected to resolve. Keep this validation in its own early stage so an
# argument-less build fails quickly and with an actionable diagnostic.
ARG MIMIR_GIT_REF
ARG MIMIR_CONTROLLER_COMMIT
ARG MIMIR_EXECUTOR_COMMIT
RUN set -eu; \
    if [ -z "$MIMIR_GIT_REF" ]; then echo >&2 "MIMIR_GIT_REF is required"; exit 2; fi; \
    case "$MIMIR_GIT_REF" in refs/*) ;; *) \
        echo >&2 "MIMIR_GIT_REF must be a fully qualified refs/* name"; exit 2;; \
    esac; \
    if ! printf '%s\n' "$MIMIR_CONTROLLER_COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then \
        echo >&2 "MIMIR_CONTROLLER_COMMIT must be a full lowercase Git SHA"; exit 2; \
    fi; \
    if ! printf '%s\n' "$MIMIR_EXECUTOR_COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then \
        echo >&2 "MIMIR_EXECUTOR_COMMIT must be a full lowercase Git SHA"; exit 2; \
    fi; \
    if ! test "$MIMIR_EXECUTOR_COMMIT" = "$MIMIR_CONTROLLER_COMMIT"; then \
        echo >&2 "MIMIR_EXECUTOR_COMMIT must equal MIMIR_CONTROLLER_COMMIT"; exit 2; \
    fi

FROM provenance-validation AS base

# OS deps:
#   - ca-certificates curl gnupg: prereqs for adding NodeSource +
#     fetching uv installer
#   - git: source-control tooling for skills and operator workflows.
#   - jq: JSON/JSONL parsing relied on by pollers, skill bodies, and
#     operational/debugging shell workflows. Kept in parity with the
#     scaffold-generated image (scaffold_docker.py), which already ships
#     jq, so clean rebuilds of this image keep the same capability (#560).
#   - nodejs + npm: Node runtime/tooling for optional coding tools.
#   - poppler-utils, tesseract-ocr, tesseract-ocr-eng: PDF-ingest
#     toolchain used by mimir's reading-queue pipeline. Tesseract's
#     control file declares ``Depends: tesseract-ocr-eng |
#     tesseract-ocr-osd`` (an OR-relation APT can satisfy with osd
#     alone — orientation detection only). Pinning ``eng`` explicitly
#     removes that ambiguity.
ENV NODE_VERSION=22
ENV MIMIR_FACTORY_ENTRYPOINT=/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js
ENV PATH="/opt/mimir-opencode/bin:${PATH}"
ARG MIMIR_ENABLE_OPENCODE=0
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq procps ripgrep xz-utils \
        poppler-utils tesseract-ocr tesseract-ocr-eng \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then \
        npm install --global --prefix /opt/mimir-opencode \
            opencode-ai@1.18.21 \
            feature-factory@0.7.4 \
            opencode-feature-factory@0.7.4 \
            opencode-project-memory@0.1.0 \
            opencode-openai-codex-auth@4.4.0 \
            opencode-anthropic-auth@0.0.13 ; \
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
    curl_args="--fail --silent --show-error --location --retry 5 --retry-all-errors --retry-delay 2"; \
    curl ${curl_args} "${base}/s6-overlay-noarch.tar.xz"      -o /tmp/s6-noarch.tar.xz; \
    curl ${curl_args} "${base}/s6-overlay-${S6_ARCH}.tar.xz"  -o /tmp/s6-arch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-arch.tar.xz; \
    rm -f /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz

# Non-root user. The mimir process needs to own its venv so the
# pending-update flag flow (``mimir/update_on_start.py``) can
# ``pip install --upgrade mimir-agent`` from inside the container
# without escalating to root. ``/home/mimir/`` is also where the
# Claude Code CLI keeps its OAuth credential under ``.claude/``.
RUN groupadd --gid 1001 mimir \
    && groupadd --gid 1002 worklink \
    && useradd --create-home --uid 1001 --gid mimir --groups worklink --shell /bin/bash mimir \
    && useradd --no-create-home --uid 1002 --gid worklink --home-dir /nonexistent --shell /usr/sbin/nologin worklink \
    && chmod 0700 /home/mimir \
    && install -d -o root -g root -m 0711 /var/lib/mimir-worklink \
    && install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/checkouts \
    && install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/repo-test-checkouts \
    && install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/opencode-checkouts \
    && install -d -o root -g worklink -m 0710 /var/lib/mimir-worklink/homes
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
#   docker build \
#     --build-arg MIMIR_GIT_REF="$MIMIR_GIT_REF" \
#     --build-arg MIMIR_CONTROLLER_COMMIT="$MIMIR_COMMIT" \
#     --build-arg MIMIR_EXECUTOR_COMMIT="$MIMIR_COMMIT" \
#     --build-arg MIMIR_EXTRAS=anthropic,discord .
#
# Available extras (see pyproject.toml):
#   anthropic, claude-code, openai, codex-plus  (model providers)
#   discord, slack                              (bridges)
#   mcp                                         (Model Context Protocol)
#
ARG MIMIR_EXTRAS="anthropic,discord,slack,mcp"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"

USER root
ARG MIMIR_GIT_URL=https://github.com/jasoncarreira/mimir.git
LABEL org.opencontainers.image.revision="${MIMIR_EXECUTOR_COMMIT}"
# The root executor may run unreleased code, but only from an immutable full SHA.
# Fetching the caller-named ref makes GitHub's synthetic refs/pull/*/merge commit
# reachable without broadening the clone or trusting the mutable ref as identity.
# The full executor SHA remains authoritative and must match FETCH_HEAD exactly.
# Fetch the immutable SHA FIRST. Fetching only the caller's ref races every merge: the
# ref moves between build start and fetch, FETCH_HEAD resolves to a newer commit, and the
# assertion below fails on a commit whose own checks were green (run 33010517969 -- main
# went red on 19d7c517 because cab16d11 landed ~1 minute later). The ref remains as a
# fallback for commits a bare-SHA fetch cannot reach, and the SHA assertion below is
# unchanged either way, so provenance is identical: a mismatch still fails the build.
RUN git check-ref-format "$MIMIR_GIT_REF" \
    && git init /opt/mimir-worklink/source \
    && git -C /opt/mimir-worklink/source remote add origin "$MIMIR_GIT_URL" \
    && { \
        git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 \
            origin "$MIMIR_EXECUTOR_COMMIT" \
        || git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 \
            origin "$MIMIR_GIT_REF"; \
    } \
    && test "$(git -C /opt/mimir-worklink/source rev-parse FETCH_HEAD)" = "$MIMIR_EXECUTOR_COMMIT" \
    && git -C /opt/mimir-worklink/source checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/mimir-worklink/source rev-parse HEAD)" = "$MIMIR_EXECUTOR_COMMIT" \
    && test -z "$(git -C /opt/mimir-worklink/source status --porcelain=v1)" \
    && printf '%s\n' "$MIMIR_EXECUTOR_COMMIT" > /opt/mimir-worklink/executor-source-commit \
    && rm -rf /opt/mimir-worklink/source/.git \
    && python3 -m venv /opt/mimir-worklink/venv \
    && /opt/mimir-worklink/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/mimir-worklink/venv/bin/pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"
# The published package above supplies the worker venv's dependency set, while
# the no-deps source overlay keeps the image proof on the pinned commit's code. A
# newly introduced runtime dependency is not in the previously published wheel,
# so install it explicitly before importing the overlay during this PR's proof.
RUN /opt/mimir-worklink/venv/bin/pip install --no-cache-dir "pypdf>=6.16" \
    && /opt/mimir-worklink/venv/bin/pip install --no-cache-dir --no-deps /opt/mimir-worklink/source \
    && /opt/mimir-worklink/venv/bin/pip install --no-cache-dir uv \
    && ln -s /opt/mimir-worklink/venv/bin/uv /usr/local/bin/uv \
    && UV_CACHE_DIR=/opt/mimir-worklink/uv-cache uv sync --directory /opt/mimir-worklink/source \
        --locked --extra dev --extra bench --no-install-workspace \
    && rm -rf /opt/mimir-worklink/source/.venv \
    && rm -rf /opt/mimir-worklink/source \
    && chown -R root:root /opt/mimir-worklink \
    && chmod -R go-w /opt/mimir-worklink
USER mimir

# Optional: install the Claude Code model-provider adapter. The CLI remains an
# operator-provided runtime dependency and is not bundled in this image.
ARG MIMIR_ENABLE_CLAUDE_CODE=0
RUN if [ "$MIMIR_ENABLE_CLAUDE_CODE" = "1" ]; then \
        pip install --no-cache-dir "mimir-agent[claude-code]" ; \
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
# OpenCode reads this XDG-global config before walking project-local
# opencode.json files, so the registrations apply in outer repos and nested
# Worklink worktrees alike. The bootstrap is a preserving, idempotent merge.
RUN if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then \
        mimir opencode-bootstrap --home /home/mimir ; \
    fi

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
RUN install -d -o root -g root -m 0755 /usr/local/libexec \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/mimir-worklink/venv/bin/python -m mimir.worklink.worker_exec "$@"' > /usr/local/libexec/worklink-execd \
    && chmod 0755 /usr/local/libexec/worklink-execd \
    && chmod +x /etc/s6-overlay/s6-rc.d/mimir/run /etc/s6-overlay/s6-rc.d/watchdog/run /etc/s6-overlay/s6-rc.d/worklink-execd/run
ENTRYPOINT ["/init"]
