# Credentials — classification, rotation cost, verification

This document inventories every credential mimir consumes, classifies
each by its consumer shape (which dictates rotation cost), and lists
the verification probe that confirms a credential is live after
rotation.

It exists to close the first half of SPEC §16 item 14 (credential
rotation protocol). Subsequent PRs ship the actual rotation machinery
(`mimir verify-cred`, `mimir rotate`). Until those land, this doc
serves as the manual runbook.

Scope: single-install rotation. mimir is deliberately not multi-tenant;
each deployment (mimirbot, muninn-mimir) rotates its own credentials
independently.

## Classification

Rotation cost is dictated by where the credential's value gets read:

| Type | Consumer shape | Rotation cost | Recovery from stale |
|---|---|---|---|
| **A. Subprocess re-spawn** | The tool re-reads the env var on every invocation (CLI subprocess, ephemeral HTTP client) | Cheap — update `compose.env`, recreate container, next call uses new value | Next call after rotation succeeds |
| **B. Long-lived client** | Bridge holds an open WebSocket / HTTP session that was authenticated at startup | Costly — needs full reconnect; missed messages during the gap (typically 5–30s) | Recreate forces a reconnect |
| **C. OAuth refresh dance** | The library holds a refresh token, auto-rotating access tokens behind the scenes; "rotation" means producing a new refresh token via a login flow, not swapping an env value | Different shape entirely — see per-cred notes | Login flow re-establishes |
| **D. Static API key** | Cred captured at startup or per-call; behaves like A from a rotation standpoint but the consumer is the agent itself, not a subprocess | Same as A | Same as A |

All A/B/D rotations share the same compose flow:

```bash
# 1. Edit compose.env in place (atomic write recommended)
# 2. Force-recreate the container
docker compose up -d --force-recreate
# 3. Verify (see Verification probes below)
```

A plain `docker compose restart` is **not** sufficient — Compose only
re-reads `env_file` on container recreation, not restart. This is the
single most common rotation footgun and is fingerprinted in operator
memory under `feedback_mimirbot_env_reload`.

## Inventory

### Type A — Subprocess re-spawn

| Env var(s) | Used by | Upstream regen | Verification probe |
|---|---|---|---|
| `GITHUB_TOKEN` | `gh` CLI, git push to state repo | https://github.com/settings/tokens (PAT) or `gh auth refresh` | `gh auth status` (returns the authenticated login) |
| `ACLI_TOKEN` (with `ACLI_EMAIL`, `ACLI_SITE`) | Atlassian CLI (Jira) | https://id.atlassian.com/manage-profile/security/api-tokens | `acli auth status` |
| `GOG_KEYRING_PASSWORD` | `gog` keyring decryption | Operator-set; rotates with the keyring itself | `gog list` succeeds without prompting |
| `OPENWEATHER_API_KEY` | `weather` skill / curl scripts | https://home.openweathermap.org/api_keys | `curl -fsSL "...?appid=$KEY"` returns 200 |
| `OP_SERVICE_ACCOUNT_TOKEN` | `op` CLI (1Password) | 1Password admin — Service Accounts | `op whoami` |
| `NTFY_TOPIC` | `ntfy` push notifications | Pick a new topic name (ntfy.sh has no revoke surface — this is a rename, not a token rotation) | Send a test message; check ntfy.sh subscription |

### Type B — Long-lived bridge clients

| Env var(s) | Used by | Upstream regen | Verification probe |
|---|---|---|---|
| `DISCORD_TOKEN` | `discord.py` client (Discord bridge) | https://discord.com/developers — Bot → Reset Token | `events.jsonl` shows `bridge_connected` for `discord` post-recreate; sending a test `send_message` to the operator channel returns success |
| `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` | Slack bolt async client | https://api.slack.com/apps — OAuth & Permissions / Socket Mode | `events.jsonl` shows `bridge_connected` for `slack`; `slack_search_users` returns the bot's identity |

Important: Type B credentials cause **observable inbound message
loss** during the reconnect window. Schedule rotation during low-
traffic windows when possible.

### Type C — OAuth refresh dance

| Env var / storage | Used by | Upstream regen | Verification probe |
|---|---|---|---|
| `$MIMIR_HOME/.claude/.credentials.json` (path resolved by `MIMIR_CLAUDE_OAUTH_CREDENTIALS`; falls back to `$HOME/.claude/.credentials.json` only if MIMIR_HOME is unset) | Claude Max OAuth subprocess for `ANTHROPIC_BASE_URL`-routed deployments | `claude /login` from a terminal with access to the host browser | `mimir oauth-usage-check` (already exists); a non-failing `oauth_usage_polled` event in `events.jsonl` |
| Gmail OAuth refresh tokens (gog keyring) | `gog` CLI for Gmail / Calendar | `gog auth login <account>` (interactive) | `gog gmail search 'newer_than:1h' --max 1 --account <name>` succeeds |
| Google Workspace OAuth (gogcli) | Google Calendar, Drive | Same as above | `gog calendar list --account <name>` |

Critical Type C constraint: the storage path **must** live on the
bind-mounted home or the refresh tokens get wiped on every container
rebuild. mimir's `_oauth_credentials_path()` (config.py) already
anchors Claude OAuth to `$MIMIR_HOME/.claude/.credentials.json`
explicitly for this reason — see the docstring there. The
`git-credential-store-erase-on-auth-failure.md` memory issue is the
analogue for git credentials.

### Type D — Static API key (agent-internal)

| Env var | Used by | Upstream regen | Verification probe |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | langchain-anthropic / ChatClaudeCode | https://console.anthropic.com/settings/keys | `mimir verify-cred ANTHROPIC_API_KEY` (TBD — Phase 2); for now a one-turn test message |
| `MINIMAX_API_KEY`, `OPENROUTER_API_KEY` | Gateway routing (when `ANTHROPIC_BASE_URL` points there) | Provider console | Same — turn test |
| `VOYAGE_API_KEY` | Voyage embedding provider (saga calibration) | https://dashboard.voyageai.com/api-keys | `mimir saga calibrate --dry-run` |
| `OPENAI_API_KEY` | Alternate embedding provider | https://platform.openai.com/api-keys | `mimir saga calibrate --dry-run` |
| `NVIDIA_API_KEY` / `NVIDIA_NIM_API_KEY` | NVIDIA NIM embedder (alternate) | NVIDIA NGC console | `mimir saga calibrate --dry-run` |
| `TAVILY_API_KEY` | `web_search` / `fetch_url` tools | https://tavily.com/dashboard | One-turn `web_search` test |
| `MIMIR_API_KEY` | mimir's own HTTP server's auth gate | Operator-chosen | `curl -H "X-Mimir-Api-Key: $KEY" http://localhost:8080/event` returns 200 (or 401 if wrong) |
| `MOLTBOOK_API_KEY`, `THREADBORN_API_KEY` | muninn-only external services | Per-service operator | Service-specific probe (TBD per service) |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | `social-cli` X provider (OAuth 1.0a) | https://developer.x.com/portal — Keys and tokens | `social-cli whoami -p x` |
| `ATPROTO_APP_PASSWORD` (a.k.a. `BSKY_APP_PASSWORD` in some docs) | `social-cli` Bluesky provider | Bluesky app → Settings → App passwords | `social-cli whoami -p bsky` |

## Multi-credential rotation (X, Bluesky)

X requires four env vars to rotate **atomically** — partial updates
leave the OAuth 1.0a signature broken in a way that doesn't return
a clean 401 from `social-cli whoami`. Treat the four as a single
unit:

```bash
# Update all four in compose.env, then:
docker compose up -d --force-recreate
docker exec <agent> social-cli whoami -p x   # confirms all four
```

Bluesky (`ATPROTO_APP_PASSWORD`) is single-value; rotation is
single-var.

## Per-type rotation checklist

### A or D — single-var rotation

1. Get new value from upstream
2. Snapshot the current file *before* touching it:
   ```bash
   cp compose.env compose.env.bak.$(date +%s)
   ```
3. Atomic edit of `compose.env` (write to a sibling tmp, fsync, rename
   over `compose.env` — the rename is the commit). The snapshot in
   step 2 is what step 6 restores from; the tmp from step 3 is gone
   after the rename
4. `docker compose up -d --force-recreate`
5. Run the verification probe; confirm success
6. If probe fails: `mv compose.env.bak.<ts> compose.env`; `docker compose up -d --force-recreate` again to restore the prior working state

### B — bridge reconnect rotation

Same as A, plus:
- Schedule during a low-traffic window
- Watch `events.jsonl` for the `bridge_connected` event post-recreate
- If the bridge fails to reconnect (token invalid, network), the bridge worker logs `bridge_connect_failed`; agent continues running but that channel is dead until fixed

### C — OAuth login rotation

1. Run the upstream login flow (`claude /login`, `gog auth login`, etc.) interactively
2. Verify the new refresh token landed on the bind-mounted path
3. No container recreate needed if the storage path is correctly anchored — the next access-token-refresh cycle picks up the new refresh token
4. If the storage path is misconfigured (file goes to ephemeral container fs), refresh tokens are lost on every restart — fix the path anchoring, then re-login

## Known gotchas

| Gotcha | Fingerprinted at |
|---|---|
| `docker compose restart` doesn't reload `env_file` — must be `up -d --force-recreate` | memory `feedback_mimirbot_env_reload` |
| GitHub token without `repo` scope returns 404 (not 403) for private repos | This session's GITHUB_TOKEN swap |
| Claude Max OAuth refresh tokens get blown away on container rebuild if `MIMIR_CLAUDE_OAUTH_CREDENTIALS` resolves outside `MIMIR_HOME` | `_oauth_credentials_path()` docstring in `config.py` |
| Git credential helper truncates `.git-credentials` on auth failure | memory `git-credential-store-erase-on-auth-failure.md` |
| X OAuth 1.0a requires all four keys consistent — partial rotation breaks signing | Inferred from upstream OAuth 1.0a spec |
| Slack bolt's WebSocket reconnect can take 5–30s; inbound DMs land but aren't surfaced until reconnect | Observed during prior token rotations |

## Per-skill credential manifests (Phase 2.5)

The probe definitions in the table above are **not** hardcoded in
the framework. Each skill that needs a credential ships a
``credentials.yaml`` next to its ``SKILL.md``; mimir's discovery
walker (mirroring the dual-skills-dir architecture from PR #272)
loads all of them at startup and merges into a single registry.

Roots, in shadow order (later wins):

1. ``mimir/credentials.yaml`` (package) — mimir-core creds: the
   model provider, mimir's own HTTP gate, bridge tokens, the state-
   repo PAT.
2. ``<home>/.mimir_builtin_skills/<skill>/credentials.yaml`` —
   bundled optional skills.
3. ``<home>/skills/<skill>/credentials.yaml`` — operator skills.

Manifest schema:

```yaml
credentials:
  - name: ACLI_TOKEN            # registry key
    cred_type: A                # A / B / C / D — see classification above
    env_vars: [ACLI_TOKEN, ACLI_EMAIL, ACLI_SITE]
    description: "..."
    probe:
      kind: subprocess          # one of: subprocess, format, all_env_set,
                                # not_implemented, python
      ...                       # kind-specific keys (see below)
```

Probe kinds:

- **`subprocess`** — run a command; exit 0 = live.
  Keys: ``binary`` (short-circuits to ``unavailable`` if not on PATH),
  ``cmd``, optional ``success_detail``.
- **`format`** — env present + (optional) prefix / length / charset
  / disallowed-prefix check.
  Keys: ``env`` (the env var to check), ``prefix``, ``min_len``,
  ``length``, ``charset`` (``"hex"``), ``disallowed_prefix``.
- **`all_env_set`** — every name in ``env_vars`` must be non-empty.
  Used for multi-var bundles (X OAuth quartet, ACLI's three vars)
  where partial updates break signing.
  Keys: optional ``note`` appended to success detail.
- **`not_implemented`** — explicit Phase-3 stub. Reports the cred's
  ``cred_type`` so the gap is grep-able.
- **`python`** — escape hatch. Loads ``script`` (path relative to
  ``credentials.yaml``) via importlib and calls ``function`` (default
  ``"probe"``). The callable takes no args and returns
  ``(ok: bool, detail: str)``. See ``mimir/optional-skills/social-cli/
  probe_bsky_password.py`` for an example.

If a skill has no ``credentials.yaml``, no credentials are registered
on its behalf. Removing a skill (or never installing it) means
``mimir verify-creds`` won't list its credentials — which is the
right behavior: a deployment that doesn't use jira has nothing to
verify about ACLI_TOKEN.

## What this doc doesn't cover yet

Next PR in the credential-rotation series:

- **`mimir rotate --cred <name>`** — atomic compose.env edit + recreate + verify, with audit events (`credential_rotation_started`, `credential_rotation_completed`, with old/new hashes for grep-by-rotation-event)
- **Drain mode** (later, opt-in) — pause new event dispatch while in-flight turns finish, then rotate

Until those land, this doc is the manual runbook. Treat the "verification probe" column as the source of truth for what "rotation succeeded" means.
