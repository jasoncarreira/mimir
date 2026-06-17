# Worklink docker-sibling WORKER image (template) — chainlink #539.
#
# The broker launches this image as:
#   docker run --rm --network <policy> <image> mimir worklink worker --payload-json <payload>
# so the image only needs `mimir` + `git` + a backend CLI on PATH. NO server, NO
# s6 supervision, NO volumes, NO baked secrets — the broker injects the
# env_allowlist vars (e.g. GITHUB_TOKEN) at run time and overrides the command.
#
# Build (tag must match allowed_images in the broker policy + `image` in worklink.yaml):
#   docker build -f docs/examples/worklink/worklink-worker.Dockerfile \
#     --build-arg MIMIR_VERSION=<release-with-#538> -t mimir-worklink:latest .
#
# OPERATOR DECISIONS — edit before building:
#   1. Pin MIMIR_VERSION to a release that INCLUDES the #538 `test_only` worker
#      (`fold_remote_test_evidence` + `worker._run_test_only`). An older mimir
#      will silently run a full implementation pass instead of the test job.
#   2. Install the backend CLI(s) your worklink.yaml routes actually select
#      (codex and/or claude). The placeholder below is intentionally inert.
#   3. Pin `allowed_images` to a digest in production rather than `:latest`.

FROM python:3.11-slim

# Empty MIMIR_VERSION installs the latest published mimir-agent — PIN IT in prod.
ARG MIMIR_VERSION=
ARG MIMIR_EXTRAS=codex-plus

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# --- Backend CLI (REQUIRED, operator-specific) -------------------------------
# The worker shells out to the backend the leaf is routed to; it MUST be present.
# Example (Node-based CLI): uncomment + add `nodejs npm` to the apt line above.
#   RUN npm install -g @openai/codex@<pin>
# Or COPY/RUN your codex/claude install here. Left inert on purpose.
# -----------------------------------------------------------------------------

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]${MIMIR_VERSION:+==${MIMIR_VERSION}}"

# Run as a non-root user; the broker controls the command, so no ENTRYPOINT/CMD.
RUN useradd --create-home --uid 1000 worker
USER worker
WORKDIR /home/worker
