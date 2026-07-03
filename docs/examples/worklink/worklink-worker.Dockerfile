# Worklink docker-sibling WORKER image (template) — chainlink #539/#540.
#
# The broker launches this image as:
#   docker run --rm --network <policy> <image> mimir worklink worker --payload-json <payload>
# NO server, NO s6 supervision, NO baked secrets — the broker injects the
# env_allowlist vars (e.g. GITHUB_TOKEN) at run time, mounts file creds read-only
# via the policy's creds_mounts (e.g. ~/.codex/auth.json), and overrides the
# command. Everything below was validated end-to-end by the first real
# docker-sibling smoke (#540); each piece is here because the smoke proved a
# worker needs it.
#
# Build (tag must match allowed_images in the broker policy + `image` in worklink.yaml):
#   docker build -f docs/examples/worklink/worklink-worker.Dockerfile \
#     --build-arg MIMIR_VERSION=<release-with-#538> -t mimir-worklink:latest .
#
# Smoke after build (compare against the controller's version; no restart is
# needed — the broker launches workers from the tag on the next job):
#   docker run --rm --entrypoint python mimir-worklink:latest \
#     -c "import importlib.metadata as m; from mimir.worklink import worker; print(m.version('mimir-agent'))"
#
# DEPLOY INVARIANT (chainlink #814): rebuild this image whenever
# mimir/worklink/worker.py — or anything the worker imports — changes in the
# deployed mimir. Updating the CONTROLLER (git pull / pip upgrade) never
# updates workers; a stale image silently nullifies worker-side fixes (this
# bit epic #783 runs 7-8: a 2-week-old image ran without the fixes the
# controller assumed). Chainlink #818 adds a fail-fast skew check; until then
# the rebuild discipline is the guard.
#
# DUAL-ROLE NOTE: some deployments run the docker-sibling BROKER from this
# same image (compose service + workers). That variant additionally needs
# docker-ce-cli installed — if yours does, do NOT rebuild the shared tag from
# this worker-only template; keep one canonical Dockerfile per deployment.
#
# OPERATOR DECISIONS — edit before building:
#   1. Pin MIMIR_VERSION to a release that INCLUDES #538 (`test_only` worker /
#      `fold_remote_test_evidence`) AND #540 (worker honors `spec.backend_config`
#      + fresh-clone `_prepare_repo`). An older mimir silently mis-runs.
#   2. Install the backend CLI(s) your worklink.yaml routes select (codex shown).
#   3. Pin `allowed_images` to a digest in production rather than `:latest`.

FROM python:3.11-slim

# Empty MIMIR_VERSION installs the latest published mimir-agent — PIN IT in prod.
ARG MIMIR_VERSION=
ARG MIMIR_EXTRAS=codex-plus
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg build-essential \
 # Node for the codex CLI (drop if your backend isn't Node-based).
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# --- Backend CLI (REQUIRED, operator-specific) -------------------------------
# The worker shells out to the backend the leaf is routed to; it MUST be present.
# Pin the backend CLI. The default tracks the framework's canonical pin
# (mimir/worklink/tool_pins.py); override the ARG to match YOUR main agent
# image so worker and controller behavior stay comparable (chainlink #814).
ARG CODEX_VERSION=0.142.4
RUN npm install -g @openai/codex@${CODEX_VERSION}
# -----------------------------------------------------------------------------

# uv — worker test commands like `uv run pytest …` auto-create the venv + sync
# the cloned repo's deps from its lockfile on first run. Without uv those
# commands fail and the run is never review-ready.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && mv /root/.local/bin/uv /usr/local/bin/uv \
 && mv /root/.local/bin/uvx /usr/local/bin/uvx

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]${MIMIR_VERSION:+==${MIMIR_VERSION}}"

# Git provisioning (system-wide so the non-root worker user inherits it):
#   - credential helper: clone/push over HTTPS using $GITHUB_TOKEN (injected by
#     the broker). Assumes an https:// remote.
#   - commit identity: the worker COMMITS the backend's changes; a fresh
#     container has none, so `git commit` fails ("Please tell me who you are").
RUN git config --system credential.helper '!f() { echo username=x-access-token; echo "password=${GITHUB_TOKEN}"; }; f' \
 && git config --system user.email "worklink@example.invalid" \
 && git config --system user.name "mimir-worklink"

# Non-root worker. /work is the DockerSibling backend's hardcoded worker root
# (repo/evidence/transcripts under /work/...), so it must exist and be writable.
# CODEX_HOME is where the broker mounts auth.json (creds_mounts target).
RUN useradd --create-home --uid 1000 worker \
 && mkdir -p /home/worker/.codex /work \
 && chown -R worker:worker /home/worker/.codex /work
ENV CODEX_HOME=/home/worker/.codex
# The broker controls the command, so no ENTRYPOINT/CMD.
USER worker
WORKDIR /home/worker
