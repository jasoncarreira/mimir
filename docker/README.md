# Docker deployment

Single-image, single-process deployment of mimir + saga. saga is an
in-process workspace dep (v0.5 §2), so there's no sidecar to supervise.

## Build

```sh
docker build -t mimir:latest .
```

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
