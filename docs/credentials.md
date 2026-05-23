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

## `mimir rotate` — automated rotation (Phase 3)

Run from the deployment directory (where `compose.env` + `compose.yml`
live). The CLI handles the snapshot-before-write rollback machinery,
the force-recreate, and the post-rotation verify automatically.

```
mimir rotate --env GITHUB_TOKEN                    # stdin (getpass'd if a TTY)
mimir rotate --env GITHUB_TOKEN --from-file new.txt
mimir rotate --env GITHUB_TOKEN --service agent    # multi-service deployments
mimir rotate --env GITHUB_TOKEN --no-recreate      # edit-only, skip docker
```

What it does, in order:

1. **Resolve config.** Locates `compose.env` + the compose file
   (`compose.yml` / `docker-compose.yml`) in the deployment dir.
   Auto-detects the service name when there's only one service;
   requires `--service` otherwise.
2. **Look up the credential.** Walks the merged probe registry to
   find which credential owns this env var (so the post-rotation
   verify runs the right probe). If the env var isn't registered,
   the CLI warns and still proceeds — verification is skipped, but
   the compose.env edit + audit trail still happen.
3. **Snapshot.** Copies `compose.env` → `compose.env.bak.<unix-ts>`
   BEFORE the write. Rollback always has a target.
4. **Atomic edit.** Writes the new value to a sibling tmp file,
   `fsync`s, renames over `compose.env`. Only the matching
   `<name>=...` line changes; surrounding lines, comments, and
   ordering are preserved verbatim.
5. **Audit start.** Appends `credential_rotation_started` to
   `./rotations.jsonl` with the env var name, the credential it
   belongs to, the credential's type (A/B/C/D), and SHA-256
   12-char prefixes of the old and new values (enough to
   distinguish without exposing the secret).
6. **Recreate.** `docker compose up -d --force-recreate <service>`.
   Per the §14 reload-semantics gotcha, plain `restart` doesn't
   reload env_file — the recreate is mandatory.
7. **Wait for ready.** Polls `docker compose ps --format json`
   until the service reports `State=running` (60s timeout).
8. **Verify in-container.** `docker compose exec -T <service> mimir
   verify-cred <cred-name>` — runs the probe with the freshly-
   rotated env value visible. Exit 0 = live.
9. **On success.** Appends `credential_rotation_completed` to
   `rotations.jsonl` with `duration_s` + the verify probe's detail
   line. Backup file is left in place; operator decides whether
   to clean up.
10. **On any failure** (recreate, wait, verify): restore
    `compose.env` from the backup, recreate the service again to
    bring the previous-good state back online, append
    `credential_rotation_failed` to `rotations.jsonl` with the
    failure stage + detail, exit non-zero. The backup file stays
    so the operator can compare what was attempted.

Audit-trail shape (`rotations.jsonl`):

```jsonl
{"timestamp": "...", "type": "credential_rotation_started", "env": "GITHUB_TOKEN", "cred": "GITHUB_TOKEN", "cred_type": "A", "old_value_hash": "sha256:abc123def456", "new_value_hash": "sha256:fed987cba321", "backup": "compose.env.bak.1716480000", "service": "agent"}
{"timestamp": "...", "type": "credential_rotation_completed", "env": "GITHUB_TOKEN", "duration_s": 18.4, "verify": "Logged in to github.com as mimir-carreira"}
{"timestamp": "...", "type": "credential_rotation_failed", "env": "GITHUB_TOKEN", "stage": "verify", "detail": "...", "rolled_back": true}
```

The audit lives in the deployment dir, not in the container's
`events.jsonl` — that's a deliberate scope choice for Phase 3
(write-from-host, where the rotation actually runs). A later phase
may cross-write into the container's event log if operators want
the rotation events alongside agent activity.

## What this doc doesn't cover yet

Future PRs in the credential-rotation series:

- **Multi-var bundle rotation** — `mimir rotate --cred X_OAUTH` to update all 4 X OAuth env vars atomically (single-env rotation is the 90% case; bundle is the 10%)
- **Drain mode** (later, opt-in) — pause new event dispatch while in-flight turns finish, then rotate. Useful for Type B (bridge) credentials where a reconnect window may drop inbound messages
- **Live Type B/C probes** — replace the `not_implemented` stubs in the registry with real verification (e.g., parse `events.jsonl` for `bridge_connected` post-recreate)
