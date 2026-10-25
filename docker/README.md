# Docker deployment

Single-image, single-process deployment of mimir + saga. saga is an
in-process workspace dep (v0.5 §2), so there's no sidecar to supervise.

## Build

```sh
# Set this to the fully qualified remote branch, tag, or pull ref that contains HEAD.
MIMIR_GIT_REF=refs/heads/main
MIMIR_COMMIT=$(git rev-parse HEAD)
test -z "$(git status --porcelain=v1)"
test "$(git ls-remote origin "$MIMIR_GIT_REF" | awk 'NR == 1 {print $1}')" = "$MIMIR_COMMIT"
docker build \
  --build-arg MIMIR_GIT_REF="$MIMIR_GIT_REF" \
  --build-arg MIMIR_CONTROLLER_COMMIT="$MIMIR_COMMIT" \
  --build-arg MIMIR_EXECUTOR_COMMIT="$MIMIR_COMMIT" \
  -t mimir:latest .
```

Add both `--build-arg MIMIR_PLUGIN_SELECTION=cultivate` and
`--build-arg CHAINLINK_ARTIFACT_STAGE=chainlink-build` (and usually
`--build-arg MIMIR_ENABLE_OPENCODE=1`) for a coding image. Cultivate is not
published to an index; the build installs core and the in-repo
`plugins/cultivate` member from the pinned commit into both venvs. Then run the
container with `MIMIR_PLUGINS=cultivate` in its environment. Without the build
argument the image is core-only and has no Worklink executor. See
[upgrading a coding deployment](../docs/plugins.md#upgrading-a-coding-deployment).

Cultivate also ships `gh` 2.103.2 (`GH_VERSION` in the root Dockerfile) for
GitHub skills, including `gh api --allow-escape-sequences` in github-ci-watch.
The official linux amd64/arm64 release tarballs are checked against the
Dockerfile's `GH_AMD64_SHA256` / `GH_ARM64_SHA256` pins before installing the
binary to `/usr/local/bin/gh`. To bump it, take both tarball SHA-256 digests
from the intended version's [official checksums](https://github.com/cli/cli/releases),
update all three Dockerfile defaults together (or pass all three build args),
and rebuild. The build runs `gh --version` as both `mimir` and `worklink`
and requires `gh api --help` to advertise `--allow-escape-sequences`, so an
incompatible override fails the build. The static version floor is 2.102.0
(the advisory-patched release); the capability check remains required.
Core-only images do not download or install gh. `mimir cultivate setup` passes
`MIMIR_EXTRAS=<provider>,mcp` to the root build, including its saved `build.sh`.

The Cultivate image also ships `/usr/local/bin/chainlink` for Worklink claims,
continuation, and the ready-queue poller. It is built from
`CHAINLINK_COMMIT=9909d7e0d5c95251f3555e0dd9d71f894cfc4883` with
`CHAINLINK_RUST_TOOLCHAIN=1.96.0` (both Docker build args). To bump it, resolve
the intended Chainlink release to a full 40-character Git commit SHA, set
`--build-arg CHAINLINK_COMMIT=<full-sha>` and, if needed, set
`--build-arg CHAINLINK_RUST_TOOLCHAIN=<version>`. Update the Dockerfile defaults
and rebuild the Cultivate image; its build runs `chainlink --version` in the
runtime stage. The builder's Rust toolchain and Cargo cache are not copied into
the image. `CHAINLINK_ARTIFACT_STAGE` defaults to `chainlink-none`, an empty
placeholder stage: core-only builds neither include the binary nor schedule its
Rust builder or fetch its source. Selecting Cultivate (including `all`) without
`CHAINLINK_ARTIFACT_STAGE=chainlink-build` fails the build with an actionable
error; a missing or broken installed CLI also fails `chainlink --version`.

With Cultivate selected, and only then, the image also creates the `worklink`
user and group (UID/GID 1002), the `/var/lib/mimir-worklink` checkout roots and
the `worklink-execd` service. A core-only image has none of them. The root-owned
Worklink executor is installed into the image under `/opt/mimir-worklink`; it is not updated by a bind-mounted controller checkout.
Its source is cloned from `MIMIR_GIT_URL` (the upstream repository by default)
and fetched from the required, fully qualified `MIMIR_GIT_REF`. The build requires
the fetched commit to equal the full `MIMIR_EXECUTOR_COMMIT` SHA, so the ref makes
GitHub pull-request merge commits reachable without becoming the source identity.
This supports commits on unreleased branches without accepting mutable
working-directory contents. `MIMIR_CONTROLLER_COMMIT` must be the same full SHA;
an argument-less build or any mismatch fails with a named diagnostic. The SHA is retained in the OCI revision label and in
`/opt/mimir-worklink/executor-source-commit`; `/health` reports it as
`worklink_executor_source_commit`.

Deployment wrappers must derive the controller SHA from the immutable source
they deploy, not accept an operator's unverified description of a mutable
checkout. In particular, do not use a local directory as a BuildKit additional
context for `/opt/mimir-worklink`. Pin a Git context by full SHA and pass that
same resolved SHA as both commit arguments. A dirty-tree executor build is not
supported.
After any change to `mimir/worklink/worker_exec.py` or its protocol, rebuild the
image and recreate/restart the container. The `/health` endpoint reports a stale
executor identity until the rebuilt image and root executor are running.

The build pre-warms the fastembed model cache (~80MB,
`BAAI/bge-small-en-v1.5`) so the first inbound doesn't pay the
download. If the build host is offline, the pre-warm is a no-op and
the cache fills on first request instead.

## LLM auth — pick one

mimir's agent uses claude-agent-sdk, which spawns Claude Code as a
subprocess per turn. There are three credential paths.

### A. Max plan (free with subscription)

`claude setup-token` produces a long-lived OAuth token tied to your
Max subscription. Generate it on a machine that has Claude Code
already authenticated (you've run `claude login` at some point), then
ship the credential into the container.

**macOS host → Linux container.** macOS stores the token in the login
keychain; the keychain blob isn't portable. Extract it and inject as
an env var:

```sh
TOKEN=$(security find-generic-password -s "Claude Code-credentials" -w)
docker run -d \
  -e CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
  -p 8080:8080 \
  -v mimir-home:/home/mimir/agent \
  -v mimir-cache:/home/mimir/.cache \
  mimir:latest
```

(`CLAUDE_CODE_OAUTH_TOKEN` is read by Claude Code's CLI at startup —
verify against the Claude Code release you're targeting; the env-var
name is the surface that's stable across keychain vs. file-based
credential layouts.)

**Linux host → Linux container.** Bind-mount the credentials file:

```sh
docker run -d \
  -v ~/.claude:/home/mimir/.claude:ro \
  -p 8080:8080 \
  -v mimir-home:/home/mimir/agent \
  -v mimir-cache:/home/mimir/.cache \
  mimir:latest
```

The first mount makes the host's logged-in session available to the
container's Claude Code subprocesses. Read-only is sufficient for
inbound traffic; flip to read-write only if you want the container
to be able to refresh the token.

### B. Anthropic Console API key (paid, simplest)

```sh
docker run -d \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -p 8080:8080 \
  -v mimir-home:/home/mimir/agent \
  -v mimir-cache:/home/mimir/.cache \
  mimir:latest
```

Bills against your Console credit, separate from any Max plan. No
keychain or volume mount needed. Easiest for stateless deployments.

### C. Gateway (LiteLLM, OpenRouter, internal proxy)

```sh
docker run -d \
  -e ANTHROPIC_BASE_URL="https://gateway.your-org.com/" \
  -e ANTHROPIC_AUTH_TOKEN="..." \
  -e ANTHROPIC_MODEL="claude-haiku-4-5" \
  -p 8080:8080 \
  -v mimir-home:/home/mimir/agent \
  -v mimir-cache:/home/mimir/.cache \
  mimir:latest
```

Useful when your org fronts Claude with a proxy, or when you want to
route to a non-Anthropic model that speaks anthropic-protocol.

## Saga embeddings

Saga's default `[embedding]` config is `provider = "openai"`. If you
set `OPENAI_API_KEY`, embeddings go through OpenAI's text-embedding-3-small
(better recall, slight cost). If you don't, saga's get_provider auto-
falls back to fastembed (`BAAI/bge-small-en-v1.5`, local CPU, no key,
~10x slower per batch but free). Both share the cache at
`/home/mimir/.cache/fastembed/` thanks to the v0.5 unification.

```sh
# OpenAI embeddings (matches the bench config):
-e OPENAI_API_KEY="sk-..."

# Local embeddings (no key, default fallback):
# (just don't set OPENAI_API_KEY)
```

## Persistent volumes

Three volumes the container expects:

| Volume | Path inside | What lives here |
|---|---|---|
| `mimir-home` | `/home/mimir/agent` | memory/, state/, logs/, .mimir/saga.db, .env |
| `mimir-cache` | `/home/mimir/.cache` | fastembed model (~80MB, regenerable) |
| `mimir-claude` | `/home/mimir/.claude` | Claude Code session (only for option A on Linux hosts) |

Lose `mimir-cache` and the next request re-downloads the embedding
model. Lose `mimir-home` and you've lost the agent's memory. Lose
`mimir-claude` and you'll need to re-authenticate Claude Code (option
A only).

## Web UI (port 8080)

The `-p 8080:8080` above exposes mimir's operator web UI (and the `/event`
ingest endpoint). There's no root landing page — start at a page route such
as `http://localhost:8080/turns`; the page's JS prompts for `MIMIR_API_KEY`
before it fetches data:

- **`/turns`** — live turn viewer (inbound trigger, tools run, replies; auto-refreshes).
- **`/ops`** — health + usage dashboard (token/cost rate, plan-window headroom,
  scheduled-tick activity, errors, pending updates).
- **`/saga`** — saga memory-atom viewer. **`/state`** — `memory/` + `state/` file browser.

The data/API routes (`/api/turns`, `/api/ops`, `/api/saga`, `/api/memory`,
`/event`) are auth-gated by `MIMIR_API_KEY`; the HTML page shells (`/turns`,
`/ops`, `/saga`, `/state`) and `/health` are exempt so the JS can load and
prompt for the key. Only publish port 8080 beyond localhost with that key set
(see Security notes below).

## docker-compose example

```yaml
services:
  mimir:
    image: mimir:latest
    ports: ["8080:8080"]
    environment:
      # Pick one auth flow (A / B / C from above)
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      MIMIR_API_KEY: ${MIMIR_API_KEY}        # required if exposing 8080 publicly
      MIMIR_MODEL: claude-haiku-4-5
    volumes:
      - mimir-home:/home/mimir/agent
      - mimir-cache:/home/mimir/.cache
    restart: unless-stopped

volumes:
  mimir-home:
  mimir-cache:
```

## First-run setup inside the container

The image runs `mimir setup --home /home/mimir/agent` at build time
(silently, with `|| true` so empty volumes don't fail the build).
On first container start the agent home is already populated. To
customize, mount a pre-prepared home directory or exec into the
container and edit `memory/core/identity.md` etc.

## Security notes

- `MIMIR_API_KEY` gates POST /event. **Required** if the container's
  port is reachable from anything beyond localhost — without it, anyone
  who can hit the port can inject events into the agent's queue.
- The container runs as UID 1001 (non-root user `mimir`).
- All outbound LLM traffic hits api.anthropic.com (or your gateway).
  Embeddings hit api.openai.com (provider=openai) or stay local
  (provider=onnx). No other network egress required.
