# Running mimir outside Docker — setup & troubleshooting

mimir is normally run in a container, where the filesystem layout, bundled CLIs,
and a fixed `MIMIR_HOME=/mimir-home` are all guaranteed. Running it directly on a
host is supported, but a few things the container handles for you have to be set
up by hand. This covers the issues that bite off-Docker and how to fix them.

Most of the confusing symptoms below trace back to **two root causes**: (1) the
agent home not being explicit, and (2) the embedding provider not actually
working and silently falling back to a local model. Fix those two and the rest
mostly clears.

---

## 1. Set `MIMIR_HOME` explicitly — to a LOCAL path

Always start mimir with an explicit home:

```bash
export MIMIR_HOME=/absolute/path/to/agent-home   # or: mimir run --home /abs/path
mimir run
```

Why it matters:
- If `MIMIR_HOME` is unset, older builds silently fall back to **the current
  working directory** as the home. That scatters `state/`, `skills/`, `memory/`,
  and the saga DB wherever you launched from, and makes the agent reference
  paths that don't exist. (The next release refuses to start without an explicit
  home, for exactly this reason.)
- **The path must be on a LOCAL filesystem.** Do **not** put `MIMIR_HOME` under
  iCloud Drive, Dropbox, OneDrive, or a network mount (NFS/SMB) — see §3.

### First contact — the agent onboards itself

On a **brand-new** home, `mimir setup` seeds an `init` block into core memory
that points the agent at its **onboarding** skill. You don't hand-configure the
agent — just start talking to it on whatever bridge you've enabled, and it runs
onboarding: conversational setup that writes its own persona / communication /
schedule blocks from what it learns. It deletes the `init` block when done
(and `setup` won't re-seed it afterward), so onboarding doesn't re-trigger.

If a fresh agent feels "blank" — generic persona, not doing autonomous work —
check that `memory/core/01-init.md` exists (fresh home) and that you've actually
messaged it; onboarding is conversation-driven, not a background job. On a home
created by an **older** build (before this was seeded), there's no `init` block —
either create one (a short core block telling the agent to load the onboarding
skill) or just ask the agent to onboard.

---

## 2. Embeddings — the biggest source of weird errors

Symptoms: `pack expected 1024 items for packing (got 384)`,
`saga_record_skill_learning` failing, "voyage-4-lite not supported."

**What's actually happening:** mimir's default embedding provider is Voyage
(`voyage-4-lite`), called over Voyage's REST API. If that call fails — no API
key, a key without access to the model, etc. — mimir **silently falls back to a
local ONNX model (BGE, 384-dim)**. On the current release that fallback
mislabels the vector dimension (reports 1024 but produces 384), which is the
`pack expected 1024 got 384` crash. (Fixed in the next release.)

> Note: there is **no `voyageai` SDK involved** — mimir hits the REST API
> directly. "Bump the voyageai SDK pin" is a red herring; don't install or pin
> `voyageai`. The model name goes straight to the API.

Pick one:

**Option A — use Voyage (best quality):**
1. `export VOYAGE_API_KEY=...`
2. Verify the key actually has access to the model with a raw call:
   ```bash
   curl -s https://api.voyageai.com/v1/embeddings \
     -H "Authorization: Bearer $VOYAGE_API_KEY" -H "Content-Type: application/json" \
     -d '{"input":["hello"],"model":"voyage-4-lite"}' | head
   ```
   If that returns an error about the model, set a model your account supports in
   `<MIMIR_HOME>/saga.toml` under `[embedding] model = "..."`.

**Option B — go fully local (no API, simplest):**
In `<MIMIR_HOME>/saga.toml`:
```toml
[embedding]
provider = "onnx"
```
This uses a local fastembed/BGE model — no key, no network. (On the current
release, prefer this only after upgrading; pre-upgrade it can hit the dim bug.)

Either way: don't leave a *broken* Voyage config in place, because the silent
ONNX fallback is what produces the confusing downstream failures.

### After switching providers — RE-INDEX (atoms *and* the file index)

This is required, not optional. When you change the embedding model (e.g. you
were silently on ONNX/384-d and now switch to Voyage/1024-d), every vector
already stored — in saga's atom DB **and** in mimir's `file_search` index — was
computed by the *old* model at the *old* dimension. Those old vectors are
incompatible with new queries, so recall on existing content silently degrades
until you re-embed. New content embeds correctly on its own; only the backlog
needs fixing.

mimir ships a command for exactly this, and it covers **both** stores:

```bash
# 1. Preview (dry-run by default — shows what would re-embed + a cost estimate):
mimir reindex --home "$MIMIR_HOME" --target both

# 2. Apply it for real:
mimir reindex --home "$MIMIR_HOME" --target both --apply
```

- `--target both` = saga atoms **and** the file_search index. (Use `atoms` or
  `files` to do just one.)
- **Dry-run by default** — nothing is written without `--apply`.
- **Resumable**: a re-run skips rows already at the new model/dim and writes
  atomically per row, so it's safe to interrupt and re-run.
- Confirm Voyage actually works first (§2) — otherwise `reindex` will just
  re-embed everything under the ONNX fallback again.
- After the atom re-embed, saga's vector index rebuilds itself from the DB on the
  next query/restart; the file_search index is rewritten in place.

Run this once, right after you switch the provider and confirm it's working.

---

## 3. SAGA "disk I/O error" on reads

Symptom: `memory_get` / `memory_query` return `disk I/O error` while session
writes succeed.

This is **not** a missing database — writes succeeding means the DB is found.
The saga DB (`<MIMIR_HOME>/.mimir/saga.db`) runs in **SQLite WAL mode**, which
needs proper file locking + shared-memory sidecars (`.db-wal`, `.db-shm`). WAL
is broken on **network/synced filesystems** — iCloud/Dropbox/OneDrive/NFS/SMB.
Reads open their own short-lived connections that re-map those sidecars; if the
filesystem can't do the locking, reads throw `disk I/O error`.

Fix: make sure `MIMIR_HOME` (and thus the saga DB) is on a **local disk**, not a
synced/network folder. To check:
- Find the DB: `<MIMIR_HOME>/.mimir/saga.db` (or `<launch-cwd>/.mimir/saga.db` if
  `MIMIR_HOME` was unset — another reason to set it).
- Confirm it's not under a synced/cloud/network path.
- Sanity-check integrity: `sqlite3 <path>/saga.db "PRAGMA integrity_check;"`

---

## 4. Channel routing — "responds in the wrong channel / DMs / never responds"

mimir channel IDs are **platform-prefixed**: `discord-<id>`, `slack-<id>`,
`web-<id>`. If a poller (or any config) is given a **bare numeric** Discord
channel ID (what Discord's "Copy ID" gives you) instead of `discord-<id>`, the
router can't match it and the reply is dropped (`UnknownChannelError`).

If you've configured a poller with a target channel (e.g. `TARGET_CHANNEL_ID` or
similar in its `pollers.json` env), make sure the value is `discord-<numeric>`,
**not** the bare number.

---

## 5. MCP servers (optional)

If you want MCP tools, the MCP SDK is an **optional extra** — it isn't installed
by default, which is why you saw "MCP loader / missing mcp module":

```bash
pip install 'mimir-agent[mcp]'
```

Then point mimir at your servers via **one** of:
- `MIMIR_MCP_SERVERS_JSON='[ {...} ]'` (inline JSON), or
- `MIMIR_MCP_SERVERS_PATH=/path/to/servers.json`

MCP is fully opt-in — if you don't need it, ignore the warning; nothing else
depends on it.

---

## 6. Pollers & optional skills need their tools + config present

The container ships the CLIs pollers shell out to (`chainlink`, coding-agent
CLIs, etc.) and provides their config. Off-Docker, **each optional skill/poller
you install must have its tools on `PATH` and its required env set**, or it will
fire on its trigger and error ("file/command not found") autonomously.

Rule of thumb: only install optional skills (worklink/chainlink/github/gmail/
etc.) once you've installed their dependencies and set their env. A poller that's
missing its deps is a steady source of autonomous-trigger errors.

---

## 7. Upgrade when the next release lands

You're on `v0.3.5`. The next release (`v0.4.0`, coming shortly) fixes several
things you're hitting:
- the embedding dim-mismatch fallback (`pack expected 1024 got 384`),
- silently-dropped turns on certain inbound payloads (the "never responds"),
- a poller firing during a quota pause it should have been shed from,
- skill-install / skill-load edge cases,
- and the hard-coded container-path assumptions (it now requires an explicit
  `MIMIR_HOME` instead of guessing the cwd).

Upgrading + doing §1–§2 above should resolve the bulk of the inconsistency.

---

## 8. Quick diagnostics

- **Logs:** `<MIMIR_HOME>/logs/events.jsonl` (structured events) and
  `<MIMIR_HOME>/logs/turns.jsonl` (per-turn records).
- **"Never responded" — classify it:** find the inbound message in
  `events.jsonl`, then check whether there's a matching turn record and a
  `send_message_sent`. No turn → it was dropped before processing (queue/payload);
  turn but no send → it ran but didn't reply or the send failed.
- **Embedding provider in use:** check `<MIMIR_HOME>/saga.toml` `[embedding]`,
  and watch the startup logs — a fall-back to ONNX is logged.
- **Poller errors:** grep `events.jsonl` (or stderr) for `poller_invalid_*` /
  `*_misconfigured` events.

---

### TL;DR
1. `export MIMIR_HOME=/local/abs/path` (not a synced/network folder).
2. Either set a working `VOYAGE_API_KEY` (verify model access) **or** put
   `[embedding] provider = "onnx"` in `saga.toml`. Don't leave Voyage half-configured.
3. **After any provider switch, re-index:**
   `mimir reindex --home "$MIMIR_HOME" --target both --apply` (re-embeds saga
   atoms **and** the file_search index; dry-run without `--apply`).
4. Keep the saga DB on a local disk (it's under `$MIMIR_HOME`).
5. Use `discord-<id>` (not bare numeric) for any channel targets.
6. `pip install 'mimir-agent[mcp]'` only if you want MCP.
7. Only install pollers/skills whose tools + env you've set up.
8. Upgrade to v0.4.0 when it ships.
