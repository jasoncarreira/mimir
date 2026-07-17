# Configuration reference

Every configuration environment variable mimir reads, with its type, default,
and what it does. **This is the complete list**, enforced by
`tests/test_config_docs_complete.py` — an AST scan of the core runtime asserts
every env read is documented here (or on the explicit exclusion allowlist), so
the list can't silently drift out of date. The scan resolves string-literal
reads *and* reads via a module-level string constant (e.g.
`os.environ.get(_WEBHOOK_ENV)`); the only unenforced shape is a name computed at
runtime from a non-constant value (a helper parameter, a cross-module import, an
f-string), which is plumbing rather than an operator flag.
[`.env.example`](../.env.example) is a copy-paste starter that covers the common
ones; this file is the exhaustive reference.

**Not included** (owned/documented elsewhere, and on the test's allowlist):
standard OS vars (`HOME`); environment injected by the harness into poller/tool
subprocesses (`STATE_DIR`, `POLLER_NAME`, `ROOT_DIR`); and locators for external
CLIs mimir shells out to, defined by those tools (`CODEX_HOME`,
`CLAUDE_CONFIG_DIR`, `CHAINLINK_BIN`, `WORKLINK_RUN_BIN`, `OSV_SCANNER`).
Optional-skill poller variables are listed below for convenience but live in
their own skill subprocesses.

## How configuration is loaded

- **Process environment wins.** Anything exported into the process (your shell,
  a Docker `compose.env`, a systemd unit) takes precedence.
- **`<MIMIR_HOME>/.env` supplies defaults** for anything not already in the
  process environment. It's loaded once at startup; the process env overrides it.
- **Unset optional flags fall back to the defaults below.**

To confirm what a running agent actually resolved, read `Config.from_env()` or
the startup banner — not the `.env` file, since the process env can override it.

Almost everything here is optional. The only things a minimal deployment needs
are an auth path (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / a gateway) and,
for anything non-loopback, `MIMIR_WEB_HOST` + `MIMIR_API_KEY`.

## Feature flags that ship off by default

These gate real, already-built capabilities that are **disabled unless you set
the flag**. If you're about to propose or build one of these, it likely already
exists — turn it on here first:

| Flag | What it turns on |
|---|---|
| `MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS` | Deliver a turn's captured final text to the triggering channel even when the model never called `send_message`. Recommended for tool-shy models (e.g. Minimax M3) that write a reply but don't reliably fire the tool. `*` = all interactive channels. |
| `MIMIR_RESEND_NUDGE_CHANNELS` | Re-prompt a turn **once**, in-band, to call `send_message` when it produced text but delivered nothing. (Superseded by auto-deliver on channels where both are set.) |
| `MIMIR_ACTIVITY_PANEL_CHANNELS` | A passive, live-updating "working…" panel posted to the channel that accumulates the turn's steps and edits itself in place (Slack `chat.update` / Discord message edit). |
| `MIMIR_MIDTURN_INJECTION_CHANNELS` | Fold an inbound user message into the currently-running turn instead of queuing it for the next one. |
| `MIMIR_CHAT_SKILLS_ENABLED` | Chat slash-skill discovery + invocation from a channel. |
| `MIMIR_FACTORY_EPICS_ENABLED` | Feature-factory epic dispatch in the chainlink-orchestrator poller (for `worklink:epic` issues). |

All channel-list flags take a comma-separated prefix allow-list (e.g.
`discord-,slack-`); `*` means all interactive channels; empty means off.

---

## Model & providers

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_MODEL_SPEC` | str | `claude-code:claude-sonnet-4-6` | Active model selector, `provider:model`. `claude-code:*` = Max-OAuth subprocess; `anthropic:*` / `openai:*` = langchain `init_chat_model`. Anthropic-compat gateways (Minimax/Kimi) use `anthropic:` + `ANTHROPIC_BASE_URL`. |
| `MIMIR_MODEL` | str | `claude-opus-4-7` | Legacy model-name field tagged onto records; the operative selector is `MIMIR_MODEL_SPEC`. |
| `MIMIR_MODEL_MAX_RETRIES` | int | `6` | Per-call transient (429/5xx) retry budget for non-`claude-code` providers. The `claude-code` path ignores it. |
| `MIMIR_MODEL_MAX_TOKENS` | int | `0` | Per-call **output** token cap for non-`claude-code` providers. `0` = provider default. Raise for thinking-via-Anthropic-compat models whose reasoning counts against output. |
| `MIMIR_MODEL_REASONING_EFFORT` | str | `""` | Reasoning effort forwarded to Codex-Plus / OpenAI reasoning models. `""` = provider default. Anthropic / Minimax / claude-code ignore it. |
| `MIMIR_EFFORT` | str | `high` | Effort level recorded on the config. |
| `MIMIR_EMBED_MODEL` | str | `BAAI/bge-small-en-v1.5` | Embedding model id. |
| `MIMIR_CONTEXT_1M` | bool | `true` | Opt into Anthropic's 1M-context beta header for Claude 4.x. Disable for accounts/models without the beta. |
| `MIMIR_USE_RESPONSES_API` | bool (tri-state) | auto | Force OpenAI Responses API on/off. Unset → derived from `OPENAI_BASE_URL`. |
| `MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS` | int | `3` | Max attempts for Codex-Plus transient connection-error retries (floor 1). |
| `MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY` | float | `0.5` | Base backoff (s) for Codex-Plus transient retries (floor 0.0). |
| `MIMIR_LLM_RETRY_MAX_ATTEMPTS` | int | `3` | Max attempts in the shared provider-agnostic LLM retry layer (backoff + jitter on transient errors). |
| `MIMIR_LLM_RETRY_BASE_DELAY` | float | `0.5` | Base backoff (s) for the shared LLM retry layer. |
| `MIMIR_LLM_RETRY_MAX_DELAY` | float | `30.0` | Max backoff (s) cap for the shared LLM retry layer. |
| `MIMIR_CLAUDE_OAUTH_CREDENTIALS` | path | `$MIMIR_HOME/.claude/.credentials.json` | Anthropic OAuth credentials for the usage poller. Empty disables; auto-disabled on a non-Anthropic `ANTHROPIC_BASE_URL`. |
| `MIMIR_BILLING_MODE` | enum | auto-detected | Override billing mode: `quota` (demotes cost-rate spikes to advisory) or `pay-as-you-go`. |

## Delivery & recovery

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS` | csv-list | `""` (off) | See [feature flags](#feature-flags-that-ship-off-by-default). Prefix allow-list; `*` = all interactive channels. |
| `MIMIR_RESEND_NUDGE_CHANNELS` | csv-list | `""` (off) | One in-band re-prompt to `send_message` when a turn produced text but delivered nothing. |
| `MIMIR_ACTIVITY_PANEL_CHANNELS` | csv-list | `""` (off) | Enable the live activity panel on matching channels. |
| `MIMIR_ACTIVITY_PANEL_DETAIL` | csv map | `""` (coarse) | Panel detail: `detailed`/`coarse` globally, or `prefix:level` pairs (e.g. `discord-:detailed`). Detailed surfaces the current step's redacted preview — trusted channels only. |
| `MIMIR_MIDTURN_INJECTION_CHANNELS` | csv-list | `""` (off) | Fold inbound `user_message` events into the running turn. Pollers / scheduled ticks excluded. |
| `MIMIR_CHAT_SKILLS_ENABLED` | bool | `false` | Chat slash-skill discovery/invocation (chainlink #783). |
| `MIMIR_CHAT_SKILL_ALLOWLIST` | csv-list | `""` | Skill slugs allowed as chat slash-skills (companion to the flag above). |

## Concurrency, queue & timeouts

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_MAX_CONCURRENT_TURNS` | int | `10` | Dispatcher cap on concurrently-running turns. |
| `MIMIR_MAX_CHANNEL_QUEUE` | int | `100` | Per-channel event queue bound. |
| `MIMIR_WORKER_IDLE_TIMEOUT_S` | int | `60` | Idle seconds before a channel worker is torn down. |
| `MIMIR_MAX_CONCURRENT_POLLERS` | int | `8` | Semaphore cap on concurrent poller subprocesses (floor 1). |
| `MIMIR_TURN_TIMEOUT_SECONDS` | int | `3600` | Per-turn wall-clock timeout on the model stream. `0` = no timeout. |
| `MIMIR_POST_TURN_TIMEOUT_SECONDS` | int | `180` | Ceiling for post-model-loop awaits (finalize hooks, end-of-turn send). |
| `MIMIR_DRAIN_TIMEOUT_SECONDS` | int | `30` | Graceful-drain bound on SIGTERM for in-flight turns. `0` = unbounded. Keep your supervisor's stop timeout ≥ this. |
| `MIMIR_TOOL_CALL_BUDGET` | int | `200` | Per-turn tool-call budget; caps panic-search loops. `0` disables. |
| `MIMIR_MAX_TURN_ITERATIONS` | int | `200` | Per-turn model-iteration ceiling; nudges at 75%/90%, hard-stops at 100%. `0` disables. |
| `MIMIR_SEND_LOOP_SOFT_LIMIT` | int | `5` | `send_message` circuit-breaker soft limit. |
| `MIMIR_SEND_LOOP_HARD_LIMIT` | int | `10` | `send_message` circuit-breaker hard limit. |
| `MIMIR_SEND_LOOP_SIMILARITY` | float | `0.9` | Similarity threshold for send-loop duplicate detection. |
| `MIMIR_CHAT_STREAM_MAX_SUBSCRIBERS` | int | `8` | Max concurrent SSE subscribers per web-chat stream. |
| `MIMIR_LIVE_EVENTS_MAX_STREAMS` | int | `8` | Max concurrent live-events dashboard streams. |

## History & recent-activity context

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_HISTORY_GLOBAL_MAX` | int | `500` | Global in-memory message-buffer cap. |
| `MIMIR_HISTORY_PER_CHANNEL_MAX` | int | `250` | Per-channel message-buffer cap. |
| `MIMIR_RECENT_PER_CHANNEL` | int | `10` | Recent-activity messages rendered from the active channel. |
| `MIMIR_RECENT_AUTHOR_CROSS` | int | `10` | Cross-channel recent messages anchored to the initiating user. |
| `MIMIR_RECENT_CROSS_HOURS` | int | `24` | Lookback window (hours) for cross-channel recent activity. |
| `MIMIR_RECENT_SOURCES` | csv-list | `slack,discord,bluesky,web,stdin` | Allowlist of `Message.source` values in Recent activity. `*`/`all` = allow all; `""` = none. |
| `MIMIR_RECENT_MESSAGE_CHARS` | int | `4096` | Per-message render cap (chars) in Recent activity. `0` = no cap. |
| `MIMIR_RECENT_BOUNDARIES` | int | `3` | Recent session boundaries rendered under "Recent session summaries". `0` disables. |
| `MIMIR_UNFINISHED_STALE_AGE_HOURS` | int | `2` | Age (h) at which an Unfinished summary gets the `[verify before quoting]` suffix. |
| `MIMIR_UNFINISHED_STALE_TURNS` | int | `5` | Turns-since-boundary at which the staleness suffix fires. |
| `MIMIR_FEEDBACK_WINDOW_HOURS` | int | `24` | Window for the Recent-feedback prompt section. |
| `MIMIR_FEEDBACK_LIMIT` | int | `5` | Per-polarity cap on rendered feedback items. `0` disables the section. |
| `MIMIR_MAX_TURNS` | int | `5000` (clamp ≤ `50000`) | On-disk cap for `turns.jsonl`. |
| `MIMIR_MAX_EVENTS` | int | `75000` (clamp ≤ `750000`) | On-disk cap for `events.jsonl`. |
| `MIMIR_TURNS_ARCHIVE_DIR` | path | unset | If set, directory where trimmed turn records are archived. |

## SAGA / memory

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_SAGA_SESSION_IDLE_MINUTES` | int | `10` | Idle minutes before a SAGA session boundary. |
| `MIMIR_SAGA_SESSION_MAX_TURNS` | int | `10` | Max turns per SAGA session before a boundary. |
| `MIMIR_SAGA_CONSOLIDATE_CRON` | cron | `0 4 * * *` | SAGA consolidation cron. |
| `MIMIR_SAGA_PRE_MSG_MIN_TIER` | str | `""` | Confidence floor for the pre-message auto-fetch hook. `""` defers to SAGA config; override `low`/`medium`/`high`. |
| `MIMIR_SAGA_SQL_ENABLED` | bool (`1`) | off | Enable the `/api/saga/sql` read-only SQL console (`=1` only). |
| `MIMIR_SAGA_SQL_TIMEOUT_S` | float | `5.0` | Wall-clock budget per SQL-console statement. |
| `MIMIR_SAGA_SQL_MAX_VALUE_BYTES` | int | `10000000` | Caps any single string/blob via `SQLITE_LIMIT_LENGTH`. |
| `SAGA_ENDPOINT` | str | unset | Only if running SAGA as a separate HTTP server (default is in-process). |
| `SAGA_API_KEY` | str | unset | Key for the SAGA HTTP server. |
| `SAGA_CONFIG` | path | unset | Explicit path to `saga.toml` (highest-priority in the config search order). Set automatically to `<MIMIR_HOME>/saga.toml` when present. |
| `SAGA_DATA_DIR` | path | unset | Data directory searched for `saga.toml` (`$SAGA_DATA_DIR/saga.toml`). |
| `SAGA_QUIET_CONFIG` | bool (`1`) | off | Suppress the "no `saga.toml` found, using defaults" startup log. |
| `SAGA_PERSISTENT_CLAUDE_POOL_SIZE` | int (≥1) | SAGA default | Size of SAGA's persistent Claude-Code client pool (only relevant when SAGA's LLM `provider = "claude_code"`). |
| `SAGA_PERSISTENT_CLAUDE_RECYCLE` | int (≥1) | SAGA default | Recycle a pooled SAGA Claude-Code client after this many calls. |

> SAGA's substantive dials (retrieval, consolidation, embeddings, LLM provider)
> live in `<MIMIR_HOME>/saga.toml`. The variables above only locate that file and
> tune the (opt-in) persistent Claude-Code pool.

## Web server & auth

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_WEB_PORT` | int | `8080` | HTTP listen port. |
| `MIMIR_WEB_HOST` | str | `127.0.0.1` | HTTP bind address. **Non-loopback (`0.0.0.0`/IP) requires `MIMIR_API_KEY`** or startup refuses. |
| `MIMIR_API_KEY` | str | `""` | Server-side key for `/api/*`; requests need a matching `X-API-Key` or 401. Empty = no auth (loopback only). Auto-generated by `mimir setup`. |
| `MIMIR_ALLOW_UNAUTHENTICATED` | bool | `false` | Suppress the empty-`MIMIR_API_KEY` startup warning (dev/localhost only). |
| `MIMIR_ATTACHMENTS_MAX_BYTES` | int | `26214400` (25 MiB) | Per-file cap on inbound chat attachments downloaded to disk. |

## Cost & usage limits

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_USAGE_BLOCK` | bool | `true` | Enable the Usage block in the turn prompt. |
| `MIMIR_USAGE_5H_LIMIT_USD` | float | `0.0` | 5h-window dollar ceiling for the "% of budget" annotation. `0` = skip. |
| `MIMIR_USAGE_WEEKLY_LIMIT_USD` | float | `0.0` | Weekly dollar ceiling for the annotation. `0` = skip. |
| `MIMIR_COST_HOURLY_LIMIT_USD` | float | `0.0` | Absolute hourly cost ceiling for cost-rate alerts. `0` disables. |
| `MIMIR_COST_RATE_SPIKE_RATIO` | float | `3.0` | Multiplier of the rolling-week per-hour baseline that trips a spike alert. `0` disables. |
| `MIMIR_COST_RATE_SPIKE_FLOOR_USD` | float | `5.00` | `rate_now` floor below which the spike check is silenced. `0` disables. |
| `MIMIR_COST_ALERT_COOLDOWN_MINUTES` | int | `60` | Minimum interval between `cost_rate_alert` events. |
| `MIMIR_CAPTURE_RATE_LIMITS` | bool | `true` | Read per-response `rate_limits` (SDK partial messages) for the Plan-windows section. |

## Scheduler, pollers, usage/quota & health

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_SCHEDULER_TZ` | str | `UTC` | IANA timezone all cron expressions are interpreted in. Invalid → UTC + warning. |
| `MIMIR_COMMITMENTS_DUE_CHECK_CRON` | cron | `*/5 * * * *` | Commitments due-check sweep. Empty disables. |
| `MIMIR_COMMITMENTS_SNOOZE_PILEUP_THRESHOLD` | int | `3` | `snooze_count` at which `commitment_snooze_pileup` fires. |
| `MIMIR_INTROSPECTION_REPORT_CRON` | cron | `0 14 * * 5` | Weekly event-introspection report. Empty disables. |
| `MIMIR_INTROSPECTION_REPORT_DAYS` | int | `7` | Lookback days for the report. |
| `MIMIR_INTROSPECTION_HEALTH_THRESHOLD` | float | `0.80` | Health-score threshold in the report. |
| `MIMIR_INTROSPECTION_EMIT_ALGEDONIC` | bool | `true` | Whether the report emits algedonic events. |
| `MIMIR_OAUTH_USAGE_POLL_CRON` | cron | `*/3 * * * *` | Anthropic OAuth usage poller. Empty disables. |
| `MIMIR_OAUTH_REFRESH_WARN_DAYS` | int | `25` | Credential age (days) at which `oauth_refresh_token_age_warn` fires. |
| `MIMIR_MINIMAX_USAGE_POLL_CRON` | cron | `""` (off) | Minimax usage poller. Opt in with a cron + `MINIMAX_API_KEY`. |
| `MIMIR_MINIMAX_USAGE_MODEL` | str | `general` | Minimax `coding_plan/remains` bucket (`general` chat, `video`). |
| `MIMIR_HEALTH_PROBE_CRON` | cron | `* * * * *` | Bind-mount stale-inode health probe. Empty disables. |
| `MIMIR_HEALTH_PROBE_MAX_RESTARTS_PER_HOUR` | int | `3` | Guard: past N self-restarts/60min, stop and surface `bind_mount_stale_persistent`. |
| `MIMIR_LIVENESS_BEAT_SECONDS` | int | `60` | Interval to rewrite `state/liveness.json` for the watchdog. `0` disables. |
| `MIMIR_LOOP_STALL_ALERT_SECONDS` | float | `300` | Daemon-thread threshold for a direct ntfy/webhook alert. `0` disables. |
| `MIMIR_LOOP_STALL_SELF_TERMINATE` | bool | `false` | After alerting, signal PID 1 so the supervisor can restart the agent. |
| `MIMIR_IDENTITIES_POPULATE_CRON` | cron | `""` (off) | Identities populator (scrapes Discord/Slack into `state/identities.yaml`). Recommended `0 6 * * *`. |
| `MIMIR_QUOTA_RECHECK_SECONDS` | int | `180` (floor `30`) | Quota-pause recheck probe cadence. |
| `MIMIR_QUOTA_5H_BACKDERIVE_FACTOR` | float | `10.0` | Back-derive factor for the 5h quota-dollar estimator. |
| `MIMIR_QUOTA_7D_ANOMALY_CONFIRM_THRESHOLD` | int | `5` | Confirmations required before acting on a 7d quota anomaly. |
| `MIMIR_WATCHDOG_WEBHOOK_URL` | str | unset | Out-of-band webhook the watchdog POSTs `{"text": ...}` to on liveness down/recovered. |
| `NTFY_TOPIC` | str | unset | ntfy.sh topic for watchdog alerts (alternative sink). |
| `MIMIR_POLLER_ENV_ALLOWLIST` | csv-list | `""` | Extra env-var names (beyond the builtin allowlist) forwarded into poller subprocess environments. |

## Git, state, update & files

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_HOME` | path | cwd (with warning) | Agent home. `<home>/.env` is loaded as runtime defaults (process env wins). |
| `MIMIR_AGENT_ID` | str | `mimir` | Logical agent name tagged on every turn/event (multi-agent disambiguation). |
| `MIMIR_GIT_TRACKING_ENABLED` | bool | `true` | Post-turn git commit + debounced push of the home. Disable for CI/transient containers. |
| `MIMIR_STATE_REPO` | str | unset | Remote repo URL for home git bootstrap. Paired with `GITHUB_TOKEN`. |
| `MIMIR_SOURCE_REPO` | path | `/workspace/mimir` | Source checkout for the pre-push staleness gate; skipped if not a dir. |
| `MIMIR_PYPI_PACKAGE_NAME` | str | `mimir-agent` | Distribution name for update-on-start + daily version check (forks/pre-release). |
| `MIMIR_DEFAULTS_UPGRADE_AUTO_SUBMIT_CLEAN` | bool | `false` | Auto-submit a conflict-free defaults-upgrade proposal PR immediately. |
| `MIMIR_PROMPTS_DIR` | path | unset | Operator prompt-override directory. |
| `MIMIR_SYSTEM_PROMPT_OVERRIDE` | str | unset | Full system-prompt override (replaces the rendered prompt entirely). |
| `MIMIR_FOLDERS` | csv `name:mode` | built-in | Per-subdir write permissions under home (`state:rw,logs:ro,...`). Unknown modes → `ro`; unsafe names rejected. |
| `MIMIR_FILE_OP_ROOTS` | colon-list | `""` | Extra absolute roots (beyond home) the file-op tools may access (`:`-separated). |
| `MIMIR_FILE_TOOL_ROOTS` | csv `path[:ro\|:rw]` | `""` | External roots routed to the file tools via CompositeBackend; `/tmp` always added rw. Rejects traversal / system roots / home overlap. See [file-tool access](../README.md#file-tool-access-outside-the-home). |
| `MIMIR_FETCH_URL_DISABLED` | bool | off | Truthy disables the `fetch_url` tool on non-`claude-code` providers. |
| `MIMIR_MCP_SERVERS_JSON` | json | `""` | Inline MCP server config list (wins over `_PATH`). MCP is opt-in. |
| `MIMIR_MCP_SERVERS_PATH` | path | `""` | Path to a JSON MCP server config file. |

## Access control & authz

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_ACCESS_CONTROL_ENFORCED` | bool | `false` | Enforce the allow/deny policy (reject unknown/non-allowlisted authors); also gates the admin-sensitive tool path. Startup rejects this setting when `MIMIR_MODEL_SPEC` uses `claude-code:` because that subprocess provider cannot carry the server-created per-turn authorization context. Use `anthropic:`, `openai:`, or `codex-plus:`, or leave enforcement disabled. |
| `MIMIR_CROSS_PLATFORM_PULL` | bool | `true` | Identity reconciliation cross-platform pull. `false` = strict per-platform isolation. |
| `MIMIR_UNAUTHORIZED_USER_BEHAVIOR` | enum | `ignore` | Unauthorized bridge users: `ignore` (log only) or `prompt-to-pair`. |
| `MIMIR_OPERATOR_ALERT_CHANNEL` | str | `""` | Channel id for high-priority operator alerts. Empty = inactive. |
| `MIMIR_PAIRING_PENDING_MAX` | int | `100` | Max pending pairing requests retained. |
| `MIMIR_PAIRING_OPERATOR_DIGEST_DELAY_SECONDS` | float | `1.0` | Coalesce window for operator pairing-notification digests. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_ENABLED` | bool | `false` | Enable fixed-text DM auto-reply to unpaired users. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_INTERVAL_SECONDS` | float | `30.0` | Global rate limit between DM auto-replies. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_TEXT` | str | `Request forwarded to operator; no access until approved.` | The fixed DM auto-reply text. |

## Spawn (subagent) controls

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_SPAWN_MAX_CONCURRENT` | int | `3` (floor 1) | Max concurrent spawned CLI subprocesses. |
| `MIMIR_SPAWN_MAX_PER_HOUR` | int | `20` (floor 1) | Sliding-window per-hour spawn cap. |
| `MIMIR_SPAWN_MAX_DEPTH` | int | `2` (floor 1) | Recursion-depth cap on nested spawns (fork-bomb guard). |
| `MIMIR_CODEX_SPAWN_ARGS` | str (shell) | `""` | Extra flags appended to `codex exec` (e.g. `--full-auto`); shlex-split. |
| `MIMIR_OPENCODE_SPAWN_ARGS` | str (shell) | `""` | Extra flags appended to `opencode run` (e.g. `--format json`); shlex-split. |

> `MIMIR_SPAWN_DEPTH` is set by the harness on child subprocesses to track
> recursion depth — it is not an operator setting.

## Worklink / chainlink / factory

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_WORKLINK_REPO` | str | unset | Repo autonomous Worklink dispatch works in (back-compat alias of `WORKLINK_REPO`, which wins). |
| `MIMIR_WORKLINK_REAPER_CRON` | cron | `""` (off) | Stale-claim TTL reaper cron; empty registers no job (non-Worklink homes). |
| `MIMIR_SCRATCH_JANITOR_CRON` | cron | `13 4 * * *` (on) | Daily scratch-retention sweep of the home's ephemeral roots; empty disables. |
| `MIMIR_SCRATCH_TTL_DAYS` | int | `1` | Age (newest contained mtime, days) before a scratch entry is swept; the recency check keeps in-use checkouts. `<= 0` disables the janitor. |
| `MIMIR_SCRATCH_JANITOR_ROOTS` | list | `scratch` | Comma-separated home-relative roots to sweep (nested paths allowed, e.g. `state/worklink/transcripts`); absolute or `..` entries are rejected. |
| `MIMIR_CHAINLINK_AUTOINIT` | bool | `1` (on) | Auto-run `chainlink init` on boot if `.chainlink` absent and the CLI is present. |
| `MIMIR_FACTORY_EPICS_ENABLED` | bool | off | Feature-factory epic dispatch in the chainlink-orchestrator poller (`worklink:epic`). |
| `MIMIR_FACTORY_RUN_TIMEOUT_S` | float | `14400` (4h) | Wall-clock timeout for a feature-factory run before the orchestrator treats it as failed. |
| `MIMIR_FACTORY_STALE_HEARTBEAT_S` | float | `900` (15m) | Heartbeat age at which a factory run is considered stalled. |
| `MIMIR_FACTORY_PROBE_WINDOW_S` | float | `300` (floor 1) | Interval the orchestrator re-probes a running factory job's state. |
| `MIMIR_FACTORY_REVIEWER` | str | `mimir-carreira` | Reviewer the factory requests on the PR it opens (`--reviewer <name>`); empty omits the flag. |
| `MIMIR_SOURCE_DIR` | path | unset | Override for locating the source checkout in the chainlink-orchestrator poller. |

## Optional-skill pollers (gmail / social / github)

These are read by opt-in poller skills, not the core config. They only matter
once the corresponding skill is installed.

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_GMAIL_QUERY` | str | `in:inbox newer_than:1d` | Gmail poller search query. |
| `MIMIR_GMAIL_MAX_FETCH` | int | `50` (clamp 1–200) | Max messages per gmail poll. |
| `MIMIR_SOCIAL_PLATFORMS` | csv-list | `bsky,x` | Platforms the social-cli pollers sync. |
| `MIMIR_SOCIAL_LIMIT` | int | `50` (clamp 1–200) | Per-sync item limit for the mentions poller. |
| `MIMIR_SOCIAL_FEED_LIMIT` | int | `50` (clamp 1–200) | Per-sync item limit for the feed poller. |
| `MIMIR_SOCIAL_USERS_DIR` | path | unset | Directory of tracked social users. |
| `MIMIR_GITHUB_PRELOAD_REVIEW_SKILL` | bool | off | Preload the review-skill body into review-needed prompts. |
| `MIMIR_GITHUB_REVIEW_SKILL_PATH` | path | `""` | Path to the review-skill file preloaded when the above is on. |
| `MIMIR_GITHUB_SELF_LOGIN` | str | `""` | GitHub login to self-filter from poller events. |

## Bridges (credentials)

| Flag | Type | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | str | unset | Discord bot token (intents enabled in the developer portal). |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | str | unset | Slack socket-mode app (both required). |
| `BSKY_HANDLE` / `BSKY_APP_PASSWORD` | str | unset | Bluesky handle + app password (not the main password). |

## Auth (LLM)

| Flag | Type | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | str | unset | Pay-per-token Anthropic key. |
| `ANTHROPIC_AUTH_TOKEN` | str | unset | Anthropic Max-plan OAuth token (`claude setup-token`) or a gateway token. |
| `ANTHROPIC_BASE_URL` | str | unset | Gateway / Anthropic-compat base URL (LiteLLM, OpenRouter, Minimax, Kimi). |
| `ANTHROPIC_MODEL` | str | unset | Reader model override. |
| `ANTHROPIC_CUSTOM_MODEL_OPTION` | str | unset | Extra model option passed through to the gateway. |
| `CLAUDE_CODE_OAUTH_TOKEN` | str | unset | OAuth token for the Claude Code subprocess path (alternative to a `claude login` credentials file). |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | str | unset | Passed through to the Claude Code CLI to disable its experimental beta headers. |
| `OPENAI_API_KEY` | str | unset | Used for SAGA embeddings + consolidation; without it SAGA falls back to local fastembed. |
| `GITHUB_TOKEN` | str | unset | Token for home git push + GitHub-backed tools/pollers. Paired with `MIMIR_STATE_REPO`. |
| `MINIMAX_API_KEY` | str | unset | Enables the Minimax usage poller (with `MIMIR_MINIMAX_USAGE_POLL_CRON`). |

## Tool & skill integration keys

Optional keys that enable specific tools/skills; unset = the tool/skill is off.

| Flag | Type | Default | Description |
|---|---|---|---|
| `TAVILY_API_KEY` | str | unset | Enables the `web_search` tool (Tavily). Unset = `web_search` disabled. |
| `TAVILY_SEARCH_URL` | str | `https://api.tavily.com/search` | Override the Tavily search endpoint (SSRF-checked). |
| `OPENWEATHER_API_KEY` | str | unset | API key for the bundled `weather` skill. |

---

## Build- & scaffold-time variables

These are consumed by the Docker scaffold / build (`start.sh`, Dockerfiles,
`compose.env`), **not** by `config.py` at runtime.

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_GIT_URL` | str | `https://github.com/jasoncarreira/mimir.git` | `start.sh` clone URL for the runtime source (change for forks). |
| `MIMIR_DEFAULT_BRANCH` | str | `main` | Branch `start.sh` clones. |
| `MIMIR_ENABLE_CLAUDE_CODE` | bool (`0`/`1`) | `0` | Build arg: `1` installs the Claude Code CLI + adapter into the image. |
| `MIMIR_EXTRAS` | csv-list | `anthropic,discord,slack,mcp` | pip extras build arg (`mimir-agent[...]`) in the PyPI-mode Dockerfile. |

> `MIMIR_GIT_USER_NAME` / `MIMIR_GIT_USER_EMAIL` appear in scaffold comments as
> committer-identity overrides, but are **not currently wired to an env read** —
> the home-commit identity uses the built-in `mimir` / `noreply@mimir-agent.local`
> default. Track before relying on them.
