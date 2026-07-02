# Running mimir outside Docker — setup & troubleshooting

mimir is normally run in a container, where the Python version, system tools,
filesystem layout, and a fixed `MIMIR_HOME=/mimir-home` are all guaranteed.
Running it directly on a host is fully supported, but a few things the container
sets up for you have to be installed/configured by hand. This guide covers the
install, the host packages mimir shells out to, the configuration that bites
off-Docker, and how to run it as a long-lived service.

Most of the confusing *symptoms* trace back to a handful of root causes: (1) the
agent home not being explicit, (2) the embedding provider silently failing over
to a local model, and (3) a host tool the agent expects (e.g. `ripgrep`) not
being installed. Fix those and the rest mostly clears.

---

## 1. Install — the package, Python, and system tools

### Python + the package

mimir requires **Python 3.11+** (CI runs 3.11 and 3.12). Install from PyPI with
the model-provider extra(s) you'll use:

```bash
pip install "mimir-agent[anthropic]"        # or [openai], [codex-plus]; add [discord,slack,mcp] as needed
mimir setup --home ~/mimir-home             # seed dirs, skills, scheduler.yaml, .env, API key
mimir run --home ~/mimir-home
```

See the [README Quickstart](../README.md#quickstart) for the full extras table,
the Claude Max (OAuth subprocess) path, and alternative providers
(Minimax/Kimi). For a development checkout, `uv sync --extra dev`.

### System tools mimir uses

The container image installs these for you; off-Docker you install them with
your OS package manager. The agent's tools and pollers **shell out** to some of
them, so a missing tool shows up as an autonomous "command not found" error or
(for `ripgrep`) a silent performance cliff.

| Tool | Needed? | What mimir uses it for |
|---|---|---|
| **`ripgrep`** (`rg`) | **Strongly recommended** | Backs the agent's file-search / `grep` tool — fast, GIL-free, and `.gitignore`-aware. Without it the tool falls back to a pure-Python directory walk that, on a large tree, runs for *minutes* and can starve the event loop into an unhealthy restart. Effectively required if you set `MIMIR_FILE_TOOL_ROOTS` (§4) to a big repo. |
| **`git`** | Recommended | Git-backed skills/pollers (chainlink, worklink, github) and the `memory/core/*` + `prompts/*` proposal-PR flow. |
| **`jq`** | Recommended | JSON/JSONL parsing in pollers, skill bodies, and operational/debug shell workflows. |
| **`poppler-utils`** | Optional | PDF text extraction in the reading-queue / ingest pipeline. |
| **`tesseract-ocr`** + `tesseract-ocr-eng` | Optional | OCR for scanned/image PDFs in the same pipeline. |
| **Node.js 20+** + `npm` | Optional (source installs only) | Building the React web console (`npm run build`). PyPI and Docker installs ship the bundle **prebuilt** — see §5. |
| coding-agent CLIs (`@openai/codex`, `@anthropic-ai/claude-code`) | Optional | Only for the codex / claude-code providers and worklink workers. Install `mimir-agent[claude-code]` (pulls `langchain-claude-code-mimir>=0.1.2,<0.2`) for the Python adapter and `@anthropic-ai/claude-code` for the CLI. |

Install the common set:

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y ripgrep git jq poppler-utils tesseract-ocr tesseract-ocr-eng

# macOS (Homebrew)
brew install ripgrep git jq poppler tesseract
# add: brew install node   # only if you build the React app from source
```

> `ca-certificates`, `curl`, `gnupg`, and `xz-utils` appear in the Dockerfile
> only as **container bootstrap** deps (adding the NodeSource repo, fetching the
> `uv` installer, unpacking the s6-overlay tarball). A normal host already has
> them; they aren't mimir runtime dependencies off-Docker.

---

## 2. Set `MIMIR_HOME` explicitly — to a LOCAL path

Always start mimir with an explicit home:

```bash
export MIMIR_HOME=/absolute/path/to/agent-home   # or: mimir run --home /abs/path
mimir run
```

Why it matters:
- Current builds **refuse to start without an explicit home** rather than
  guessing the current working directory (which used to scatter `state/`,
  `skills/`, `memory/`, and the saga DB wherever you launched from). Set it.
- **The path must be on a LOCAL filesystem.** Do **not** put `MIMIR_HOME` under
  iCloud Drive, Dropbox, OneDrive, or a network mount (NFS/SMB) — see §6.

### Configuration: `<home>/.env` is loaded as defaults

`mimir setup` writes a `<home>/.env` scaffold. As of **0.6.5** the runtime loads
it: `Config.from_env()` reads `<home>/.env` as **defaults**, and real
**process-environment values still win** over it. So you can keep config in
`<home>/.env` for a local/systemd run, or `export` vars (or use a systemd
`EnvironmentFile=`) to override. An absent `.env` is a no-op. Copy
[`.env.example`](../.env.example) for the full list of variables mimir reads.

### First contact — the agent onboards itself

On a **brand-new** home, `mimir setup` seeds an `init` block into core memory
that points the agent at its **onboarding** skill. You don't hand-configure the
agent — start talking to it on whatever bridge you've enabled and it runs
onboarding: conversational setup that writes its own persona / communication /
schedule blocks. It deletes the `init` block when done (and `setup` won't
re-seed it), so onboarding doesn't re-trigger.

If a fresh agent feels "blank," check that `memory/core/01-init.md` exists and
that you've actually messaged it — onboarding is conversation-driven, not a
background job. On a home created by a pre-onboarding build, just ask the agent
to onboard.

---

## 3. Embeddings — the biggest source of weird errors

Symptoms: `pack expected 1024 items for packing (got 384)`,
`saga_record_skill_learning` failing, "voyage-4-lite not supported."

**What's happening:** if your configured embedding provider's API call fails (no
key, a key without access to the model, etc.), saga can fall back to a local
ONNX model (BGE, 384-dim). A mismatch between the configured dimension and what
the fallback produces is what surfaces as `pack expected 1024 got 384`. The
dimension-mislabel bug itself is fixed in current releases, but a *broken*
provider config still degrades quietly to the local model, so don't leave one
half-configured.

> There is **no `voyageai` SDK involved** — mimir/saga hit the embeddings REST
> API directly. "Bump the voyageai SDK pin" is a red herring. The model name
> goes straight to the API.

Pick one:

**Option A — a hosted provider (best quality).** Set the provider's key (e.g.
`VOYAGE_API_KEY`, or `OPENAI_API_KEY` for the OpenAI default), then verify the
key actually has access to the configured model with a raw `curl` before
trusting it. If the model is rejected, set one your account supports in
`<MIMIR_HOME>/saga.toml` under `[embedding] model = "..."`.

**Option B — fully local (no API, simplest).** In `<MIMIR_HOME>/saga.toml`:
```toml
[embedding]
provider = "onnx"
```
A local fastembed/BGE model — no key, no network.

### After switching providers — RE-INDEX (atoms *and* the file index)

Required, not optional. When you change the embedding model/dimension, every
vector already stored — in saga's atom DB **and** in mimir's `file_search` index
— was computed by the *old* model at the *old* dimension and is incompatible
with new queries, so recall on existing content silently degrades until you
re-embed. New content embeds correctly on its own; only the backlog needs fixing.

```bash
# Preview (dry-run by default — shows what would re-embed + a cost estimate):
mimir reindex --home "$MIMIR_HOME" --target both
# Apply for real:
mimir reindex --home "$MIMIR_HOME" --target both --apply
```

- `--target both` = saga atoms **and** the file_search index (`atoms`/`files` for one).
- **Dry-run by default**; **resumable** (skips rows already at the new model/dim,
  writes atomically per row — safe to interrupt and re-run).
- Confirm the provider works first — otherwise `reindex` just re-embeds
  everything under the fallback again.

---

## 4. File-tool access outside the home (`MIMIR_FILE_TOOL_ROOTS`)

By default the agent's file tools (`read_file` / `ls` / `glob` / `edit_file`)
are confined to `<MIMIR_HOME>`. An absolute path **outside** the home used to be
silently remapped under the home → a false "not found" (while `shell_exec` could
see the real file). **`MIMIR_FILE_TOOL_ROOTS`** (added in #650) grants the file
tools *real* access to directories outside the home — e.g. a source checkout the
agent develops, or a work codebase on a PyPI-wheel deployment.

**Format:** comma-separated entries, each `path` or `path:ro` / `path:rw`. A
bare `path` defaults to **`rw`**. `:ro` makes a root read-only (writes/edits/
uploads are blocked). The home stays the default backend; roots are added as
routes.

```bash
# read/write a repo, read-only reference tree
MIMIR_FILE_TOOL_ROOTS="/home/me/code/myrepo:rw,/srv/reference:ro"
```

- **`/tmp` is always added `rw`** when it exists — even with the variable unset,
  every deployment gets `/tmp` file-tool access by default.
- **Validation** (invalid entries are logged and skipped — never fatal): must be
  **absolute** and an **existing directory**; symlinks are resolved; rejected are
  `~`, `..`, `/`, `/etc`, and any root that **is** the home or **overlaps** it in
  either direction (which would shadow the home's write-guard).
- An absolute path that names a real file in **no** configured root now returns
  an actionable error — *"outside the file-tool root … use `shell_exec`, or add
  its directory to `MIMIR_FILE_TOOL_ROOTS`"* — instead of a silent false
  not-found.

**Non-Docker:** the values are just host paths; set the variable in
`<home>/.env`, your shell, or the systemd `EnvironmentFile`.

**Docker:** the container only sees what you bind-mount, so you need **both**
steps — mount the host path in, *and* point `MIMIR_FILE_TOOL_ROOTS` at the
**in-container** path:

```yaml
services:
  mimir:
    environment:
      MIMIR_FILE_TOOL_ROOTS: "/workspace/myrepo:rw,/data/reference:ro"
    volumes:
      - /home/me/code/myrepo:/workspace/myrepo          # rw bind
      - /srv/reference:/data/reference:ro               # ro bind (defense-in-depth)
```

> Security: an `rw` root is fully writable by the agent's file tools. Prefer
> `:ro` for anything the agent only needs to read, and for Docker make the bind
> mount `:ro` too so the kernel enforces it independently of the app. (Note that
> `shell_exec` can already reach anything the process can — file-tool roots scope
> the *file tools*, not the shell.)

---

## 5. The React web console — build step for source installs

mimir serves an operator web UI at `/app` (default port `8080`). The React app
lives under `frontend/` and builds into `mimir/react_app/dist`.

- **PyPI / Docker installs ship the bundle prebuilt** (the wheel force-includes
  `react_app/dist` as of 0.6.1), so `/app` works out of the box.
- **Source / clone runs must build it once**, or `/app` reports "React app build
  not found". Run these from the **repo root** (`package.json` and
  `vite.config.ts` live there; Vite is configured with `root: "frontend"`):

  ```bash
  npm ci
  npm run build      # writes mimir/react_app/dist; npm run dev for live frontend work
  ```

The bundle is served per request, so rebuilding it does **not** require an agent
restart. Expose the port publicly only with `MIMIR_API_KEY` set; per-user web
keys + RBAC are managed in **Admin → Users** (see
[`docs/web-ui-auth.md`](./web-ui-auth.md)).

---

## 6. SAGA "disk I/O error" on reads

Symptom: `memory_get` / `memory_query` return `disk I/O error` while session
writes succeed.

This is **not** a missing database (writes succeeding means it's found). The saga
DB (`<MIMIR_HOME>/.mimir/saga.db`) runs in **SQLite WAL mode**, which needs
proper file locking + shared-memory sidecars (`.db-wal`, `.db-shm`). WAL is
broken on **network/synced filesystems** — iCloud/Dropbox/OneDrive/NFS/SMB.

Fix: keep `MIMIR_HOME` (and thus the saga DB) on a **local disk**.
- Find the DB at `<MIMIR_HOME>/.mimir/saga.db`.
- Confirm it's not under a synced/cloud/network path.
- Sanity-check: `sqlite3 <path>/saga.db "PRAGMA integrity_check;"`

---

## 7. Channel routing — "responds in the wrong channel / DMs / never responds"

mimir channel IDs are **platform-prefixed**: `discord-<id>`, `slack-<id>`,
`web-<id>`. If a poller (or any config) is given a **bare numeric** Discord
channel ID (what Discord's "Copy ID" gives you) instead of `discord-<id>`, the
router can't match it and the reply is dropped (`UnknownChannelError`).

Make sure any configured target channel (e.g. `TARGET_CHANNEL_ID` in a poller's
env) is `discord-<numeric>`, **not** the bare number.

---

## 8. MCP servers (optional)

The MCP SDK is an **optional extra** — not installed by default (the source of a
"MCP loader / missing mcp module" warning):

```bash
pip install 'mimir-agent[mcp]'
```

Then point mimir at your servers via **one** of `MIMIR_MCP_SERVERS_JSON` (inline
JSON) or `MIMIR_MCP_SERVERS_PATH` (file). Fully opt-in; ignore the warning if you
don't need it.

---

## 9. Pollers & optional skills need their tools + config present

The container ships the CLIs pollers shell out to (`chainlink`, coding-agent
CLIs, etc.) and provides their config. Off-Docker, **each optional skill/poller
you install must have its tools on `PATH` and its required env set**, or it fires
on its trigger and errors ("file/command not found") autonomously.

Rule of thumb: only install optional skills (worklink/chainlink/github/gmail/…)
once you've installed their dependencies and set their env. A poller missing its
deps is a steady source of autonomous-trigger errors. (For chainlink
specifically, `build_app` auto-runs `chainlink init` when the CLI is present;
disable with `MIMIR_CHAINLINK_AUTOINIT=0`.)

---

## 10. Running as a long-lived service (the container's job, by hand)

Off-Docker you replace what the container's supervisor + healthcheck did. mimir
ships the pieces:

- **systemd** — `mimir run` under a unit with `Restart=on-failure` and an
  `OnFailure=` hook that runs `mimir notify-restart` (a self-contained
  ntfy/webhook push needing no live agent). Templates in `deploy/systemd/`;
  full runbook (incl. the macOS/launchd note) in
  [`docs/systemd.md`](./systemd.md).
- **Graceful drain on restart** — on `SIGTERM`/`SIGINT` the dispatcher stops
  accepting new events and waits up to `MIMIR_DRAIN_TIMEOUT_SECONDS` (default
  **30**) for in-flight turns to finish, so a deploy/restart doesn't kill a live
  turn. Make sure your supervisor's stop timeout exceeds the drain (systemd's
  `TimeoutStopSec` covers it by default). Runbook:
  [`docs/graceful-restart.md`](./graceful-restart.md).
- **Liveness watchdog** — the agent writes a beat to `.mimir/liveness.json`
  every `MIMIR_LIVENESS_BEAT_SECONDS` (default **60**; an event-loop task, so it
  also stops on a *wedge*). Run `mimir watchdog` as a host cron or a second
  service to alert out-of-band when the beat goes stale, and it can
  `--restart-on-stale` to recover a wedged process. Sinks: `NTFY_TOPIC` and/or
  `MIMIR_WATCHDOG_WEBHOOK_URL`. The agent also writes a clean-shutdown marker and
  emits a `liveness_unclean_restart` notice if the previous run was killed/OOM'd.
  Runbook: [`docs/watchdog.md`](./watchdog.md).
- **Runaway-turn ceiling** — `MIMIR_MAX_TURN_ITERATIONS` (default **200**, `0`
  disables) hard-stops a turn that spins past the cap so a loop can't burn quota
  unbounded.

---

## 11. Quick diagnostics

- **Logs:** `<MIMIR_HOME>/logs/events.jsonl` (structured events) and
  `<MIMIR_HOME>/logs/turns.jsonl` (per-turn records).
- **"Never responded" — classify it:** find the inbound message in
  `events.jsonl`, then check for a matching turn record and a
  `send_message_sent`. No turn → dropped before processing (queue/payload);
  turn but no send → it ran but didn't reply or the send failed. (For web chat,
  the resend-nudge re-prompts a tool-shy model once to actually call
  `send_message`; `web-` channels get it by default, extend to others via
  `MIMIR_RESEND_NUDGE_CHANNELS`.)
- **Embedding provider in use:** check `<MIMIR_HOME>/saga.toml` `[embedding]`;
  the startup logs note a fall-back to ONNX.
- **File-tool "not found" on a real file:** confirm the path is under the home or
  a `MIMIR_FILE_TOOL_ROOTS` root (§4); the error message names the fix.
- **Poller errors:** grep `events.jsonl` (or stderr) for `poller_invalid_*` /
  `*_misconfigured` events.

---

### TL;DR
1. **Python 3.11+**; `pip install "mimir-agent[anthropic]"`. Install the host
   tools you'll use — at minimum **`ripgrep`** (file search), plus `git`/`jq`,
   and `poppler-utils`/`tesseract-ocr` for PDFs.
2. `export MIMIR_HOME=/local/abs/path` (not a synced/network folder). Config can
   live in `<home>/.env` (loaded as defaults; real env wins).
3. Use a working embedding provider **or** `[embedding] provider = "onnx"` in
   `saga.toml` — don't leave one half-configured. **After any switch, re-index:**
   `mimir reindex --home "$MIMIR_HOME" --target both --apply`.
4. To let the file tools reach repos outside the home, set
   `MIMIR_FILE_TOOL_ROOTS=/abs/path[:ro|:rw],…` (`/tmp` is always `rw`). In
   Docker, also bind-mount the path and use its in-container path.
5. Source/clone install? Build the web console once from the repo root:
   `npm ci && npm run build` (PyPI/Docker ship it prebuilt).
6. Keep the saga DB on a local disk; use `discord-<id>` (not bare numeric) for
   channel targets; `pip install 'mimir-agent[mcp]'` only if you want MCP; only
   install pollers/skills whose tools + env you've set up.
7. For an always-on host: run under **systemd** (`deploy/systemd/`,
   [`docs/systemd.md`](./systemd.md)) and add the **watchdog**
   ([`docs/watchdog.md`](./watchdog.md)).
