# Configuration reference

Every configuration environment variable mimir reads, with its type, default,
and what it does. **This is the complete list**, enforced by
`tests/test_config_docs_complete.py`. The gate scans exact `MIMIR_*` string
literals in core Python code, so new names must gain a reference entry. Prefixes
and regular expressions do not match the exact-name shape and are excluded by
construction rather than by an ignore list. A second AST scan covers non-Mimir
environment reads and explicitly excludes variables owned by the OS or an
external process.
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
| `MIMIR_CODING_ENABLED` | Expose the `spawn_open_code` coding-assistant tool. Requires the `opencode` CLI on `PATH`; startup fails if enabled without it. |
| `MIMIR_FACTORY_EPICS_ENABLED` | Feature-factory epic dispatch in the chainlink-orchestrator poller (for `worklink:epic` issues). |

All channel-list flags take a comma-separated prefix allow-list (e.g.
`discord-,slack-`); `*` means all interactive channels; empty means off.

---

## Model & providers

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_MODEL_SPEC` | str | `codex-plus:gpt-5.6-luna` | Active model selector, `provider:model`. `codex-plus:*` = Codex subscription OAuth; `claude-code:*` = Max-OAuth subprocess; `anthropic:*` / `openai:*` = langchain `init_chat_model`. Anthropic-compat gateways (Minimax/Kimi) use `anthropic:` + `ANTHROPIC_BASE_URL`. Each provider needs its extra installed (`pip install 'mimir-agent[codex-plus]'` for the default). |
| `MIMIR_MODEL` | str | `claude-opus-4-7` | Legacy model-name field tagged onto records; the operative selector is `MIMIR_MODEL_SPEC`. |
| `MIMIR_MODEL_MAX_RETRIES` | int | `6` | Per-call transient (429/5xx) retry budget for non-`claude-code` providers. The `claude-code` path ignores it. |
| `MIMIR_MODEL_MAX_TOKENS` | int | `0` | Per-call **output** token cap for non-`claude-code` providers. `0` = provider default. Raise for thinking-via-Anthropic-compat models whose reasoning counts against output. |
| `MIMIR_MODEL_REASONING_EFFORT` | str | `""` | Reasoning effort forwarded to Codex-Plus / OpenAI reasoning models. `""` = provider default. Anthropic / Minimax / claude-code ignore it. |
| `OPENCODE_CONFIG` | path | `$XDG_CONFIG_HOME/opencode/opencode.jsonc` or `~/.config/opencode/opencode.jsonc` | OpenCode's native operator-owned JSON/JSONC. Its `model`, `provider`, plugin, proxy, and `{env:NAME}` settings are used unchanged by both `spawn_open_code` and the Worklink backend. If `model` is absent, the live `MIMIR_MODEL_SPEC` is translated to `provider/model`; an explicit spawn `model` wins over both. |
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
| `MIMIR_MIDTURN_INJECTION_CHANNELS` | csv-list | `""` (off) | Fold inbound `user_message` events into the running turn. Pollers / scheduled ticks excluded. |
| `MIMIR_CHAT_SKILLS_ENABLED` | bool | `false` | Chat slash-skill discovery/invocation (chainlink #783). |
| `MIMIR_CHAT_SKILL_ALLOWLIST` | csv-list | `""` | Skill slugs allowed as chat slash-skills (companion to the flag above). |
| `MIMIR_CODING_ENABLED` | bool | `false` | Expose `spawn_open_code` to the agent. Enabling requires the `opencode` CLI on `PATH`; an unavailable CLI fails startup. |

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
| `MIMIR_RECENT_SOURCES` | csv-list | `slack,discord,bluesky,web,stdin,acp` | Allowlist of `Message.source` values in Recent activity. `*`/`all` = allow all; `""` = none. |
| `MIMIR_ACP_JOURNAL_TTL_DAYS` | positive int | `7` | Days to retain replayable ACP session journals before expiry. |
| `MIMIR_ACP_ENABLED` | bool | enabled on POSIX with verifiable peer credentials | Start the owner-only Unix ACP daemon with `mimir run`. An explicit false value (`0`, `false`, `no`, `off`, or `n`, case-insensitive) prevents all ACP daemon construction; explicit enable fails on unsupported platforms. |
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
| `MIMIR_CODEX_USAGE_POLL_CRON` | cron | `*/3 * * * *` | Non-generative Codex account quota refresh. Empty disables. |
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
| `MIMIR_FILE_OP_ROOTS` | retired tripwire | unset | Retired and ignored. If still present, startup warns to migrate every required root to `MIMIR_FILE_TOOL_ROOTS` and remove the old deployment setting; it grants no access. |
| `MIMIR_FILE_TOOL_ROOTS` | csv `/absolute/path[:ro\|:rw]` | `""` | Legacy projection of `repositories.yaml` repository and allowed roots. When the YAML inventory is declared, an omitted value is derived and a disagreeing value is a startup error. Without that inventory, it retains the legacy behavior. `/tmp` is always derived as `rw`. See [file-tool access](../README.md#file-tool-access-outside-the-home). |
| `MIMIR_PR_CHECKOUT_LEASE_ROOT` | absolute path | unset | Root for atomic, scope-bound PR checkout leases. GitHub activity receives write authority only for its active lease path, not this root generally or the live source checkout. |
| `MIMIR_PR_CHECKOUT_LEASE_REAPER_CRON` | cron | `*/15 * * * *` | Expired PR checkout lease reclamation cadence. Each lease's recorded `expires_at` determines eligibility; an empty value disables the scheduled sweep. |
| `MIMIR_FETCH_URL_DISABLED` | bool | off | Truthy disables the `fetch_url` tool on non-`claude-code` providers. |
| `MIMIR_MCP_SERVERS_JSON` | json | `""` | Inline MCP server config list (wins over `_PATH`). MCP is opt-in. |
| `MIMIR_MCP_SERVERS_PATH` | path | `""` | Path to a JSON MCP server config file. |

## Access control & authz

See the [authorization reference](authorization.md) for the requester-resource
model, trusted-service matrix, resource and IFC layers, extension obligations,
and the shadow-first enablement runbook. Human roles are canonical-level policy
in `<MIMIR_HOME>/state/identities.yaml`: `user` admits normal inbound use and
`admin` admits admin-required operations. Generic `/event` API credentials
authenticate transport only; they do not create a named requester.

| Flag | Type | Default | Description |
|---|---|---|---|
| `MIMIR_ACCESS_CONTROL_ENFORCED` | bool | `false` | Enforce the allow/deny policy (reject unknown/non-allowlisted authors); also gates the admin-sensitive tool path. Compatible with the default `MIMIR_MODEL_SPEC`. Claude Code subprocess hooks receive the exact per-invocation authorization context through a server-owned carrier, so `claude-code:*` is also enforcement-compatible. |
| `MIMIR_EGRESS_APPROVED_URLS` | URL or JSON array | `""` | Exact URLs approved for application network egress. Configure multiple URLs as a JSON array, for example `["https://hooks.example/a", "https://hooks.example/b"]`; comma-separated URL lists are not supported. |
| `MIMIR_HEARTBEAT_APPROVED_URLS` | URL or JSON array | `""` | Exact URLs the heartbeat service may fetch. Configure multiple URLs as a JSON array; comma-separated URL lists are not supported. |
| `MIMIR_PROJECT_TEST_COMMAND` | JSON object | unset | Operator-owned project test invocation for trusted-service turns: `{"argv":["/absolute/root-owned/test-runner","fixed","arguments"],"cwd":"/configured/project/root"}`. The executable must be an absolute, executable, non-symlink file outside service-writable roots; `cwd` must be within `MIMIR_FILE_TOOL_ROOTS`. The model may append only bounded relative test paths/selectors, never options or a different command. Interpreter commands are rejected. Unset preserves the ordinary shell-profile refusals. |
| `MIMIR_CROSS_PLATFORM_PULL` | bool | `true` | Cross-platform recent-context pull. `false` stops canonical cross-platform history matching, but does not isolate authorization roles: aliases still share their canonical identity's access metadata. |
| `MIMIR_UNAUTHORIZED_USER_BEHAVIOR` | enum | `ignore` | Controls the extra `inbound_pairing_prompted` event for enforced public/shared-channel denials: `ignore` or `prompt-to-pair`. All enforced denials may still create a pending pairing and notify the operator; this setting sends no public reply. |
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
| `MIMIR_OPENCODE_SPAWN_ARGS` | str (shell) | `""` | Extra flags appended to `opencode run` (e.g. `--format json`); shlex-split. |

> `MIMIR_SPAWN_DEPTH` is set by the harness on child subprocesses to track
> recursion depth — it is not an operator setting.

## Worklink / chainlink / factory

Worklink reads deployment settings from `<MIMIR_HOME>/worklink.yaml`. The test
gate and OpenCode executor shell policy must agree. When
`backends.opencode.bash_allowlist` is omitted, Worklink conservatively derives
the effective default as `git *` plus only the approved build runner named by
`defaults.test_command`. The built-in Python-oriented configuration is therefore
`test_command: "uv run pytest -q"` with `bash_allowlist: ["git *", "uv *"]`.
Supported derived runners are `uv`, `npm`, `pnpm`, `yarn`, `bun`, `mvn`, Maven
Wrapper, `gradle`, Gradle Wrapper, `cargo`, and `go`. General launchers such as
`bash`, `sh`, `env`, `make`, and language interpreters are not derived.

An operator-set allowlist replaces the derived default and must admit the
configured test command. Empty lists deny all executor shell commands and thus
fail configuration reconciliation for a non-empty test command. The catch-all
`"*"` is always rejected. Startup logs the effective list; a refused command
reports `backends.opencode.bash_allowlist` and those effective patterns.

Node deployment:

```yaml
defaults:
  test_command: "npm test"
backends:
  opencode:
    bash_allowlist: ["git *", "npm *"]
```

Java deployment using Gradle Wrapper:

```yaml
defaults:
  test_command: "./gradlew test"
backends:
  opencode:
    bash_allowlist: ["git *", "./gradlew *"]
```

Both examples use explicit lists for auditability; omitting `bash_allowlist`
derives the same two patterns. The setting is operator configuration only:
repository files and model-generated values never add permission entries.

| Flag | Type | Default | Description |
|---|---|---|---|
| `GITHUB_REPOS` | csv `owner/repository` | unset | Legacy projection of `repositories.yaml` `repositories[].slug`. When the repository inventory is declared, an omitted value is derived and a disagreeing value is a startup error. Without the inventory, it retains the legacy repository allowlist behavior. |
| `MIMIR_WORKLINK_REPO` | str | unset | Repo autonomous Worklink dispatch works in (back-compat alias of `WORKLINK_REPO`, which wins). |
| `MIMIR_WORKLINK_AGENT_ID` | str | process-generated | Internal process-scoped owner inherited by detached Worklink controllers; the server sets this automatically. |
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
| `MIMIR_WORKLINK_MAX_STDOUT_BYTES` | positive int | `67108864` (64 MiB) | Maximum stdout retained from a Worklink backend subprocess. Invalid or non-positive values use the default; exceeding the cap terminates the subprocess. |
| `MIMIR_WORKLINK_MAX_STDERR_BYTES` | positive int | `16777216` (16 MiB) | Maximum stderr retained from a Worklink backend subprocess. Invalid or non-positive values use the default; exceeding the cap terminates the subprocess. |

## Worklink YAML

Worklink reads `<MIMIR_HOME>/worklink.yaml`. This is separate from `.env`: YAML
selects Worklink execution behavior and backends, while the environment table
above configures the Mimir process. Omitted keys use the defaults below.

| Top-level key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `defaults` | mapping | `{}` | Holds routing, execution, autonomy, and compatibility defaults described below. | `defaults: {backend: opencode}` |
| `repository` | `owner/repository` | unset | Names Worklink's target from the neutral `repositories.yaml` inventory. | `repository: owner/service` |
| `routes` | list[mapping] | `[]` | Selects tool/compute backends using first-match-wins rules. | `routes: [{label: worklink:epic, backend: feature_factory}]` |
| `backends` | mapping | `{}` | Configures the shipping tool-backend adapters. | `backends: {opencode: {bin: opencode}}` |
| `compute_backends` | mapping | `{}` | Configures compute substrates; the sole shipping substrate accepts an empty block only. | `compute_backends: {local_subprocess: {}}` |
| `tool_pins` | list[mapping] | `[]` | Records operator-owned external-tool pins for drift and bump issue generation. | `tool_pins: [{name: opencode, category: coding-cli, pin: "1.18.9", smoke: "opencode --version"}]` |

### Repository Inventory

The neutral repository and file-root inventory lives in
`<MIMIR_HOME>/repositories.yaml`, separate from Worklink's execution settings:

```yaml
repositories:
  - slug: owner/service
    root: /workspace/service
    mode: rw
    origin: https://github.com/owner/service.git
    base_branch: trunk
    test_command: npm test
allowed_roots:
  - root: /benchmark
    mode: rw
  - root: /mimir-results
    mode: ro
```

Worklink names its target in its own file:

```yaml
# worklink.yaml
repository: owner/service
```

Declaring either inventory key enables the new source. Startup derives omitted
`GITHUB_REPOS`, `MIMIR_FILE_TOOL_ROOTS`, and, when Worklink names a target,
`WORKLINK_REPO` values; if a legacy value is still present, its effective value
must agree exactly or startup fails with both values. This permits
one-variable-at-a-time migration without widening file scope.

Every repository root must exist, be the checkout top level, and have a local
`remote.origin.url` exactly equal to `origin`. Duplicate slugs or roots and
an overlap between repository and allowed roots are fatal. A Worklink target
that is absent from the neutral inventory is also fatal. Run
`MIMIR_HOME=/path/to/home mimir run` after editing either file; the preflight
runs before application construction or socket binding and names the repository,
expected origin, and observed origin on failure.

`repo_test` uses a repository `test_command` when present and otherwise falls
back to `defaults.test_command`. It POSIX-splits the selected value into fixed
arguments and runs it without a shell. Results include the resolved command and
whether it came from `repository` or `deployment` configuration.

### Defaults

| Key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `defaults.backend` | backend name | `opencode` | Selects the tool backend when no route or category override matches. | `backend: opencode` |
| `defaults.timeout_s` | int | `1800` | Maximum seconds allowed for the backend/compute run. | `timeout_s: 3600` |
| `defaults.priority` | str | `normal` | Priority supplied to the autonomous arbiter. | `priority: low` |
| `defaults.test_command` | command string | `uv run pytest -q` | Command Worklink uses for observed test evidence. Granting the typed `repo_test` capability lets a remediation turn run this same configured command, shell-free, in its authorized PR checkout lease. | `test_command: "/usr/bin/npm test"` |
| `defaults.backend_by_category` | mapping | `{}` | Selects a backend by tool category after no route matches. | `backend_by_category: {coding-cli: opencode}` |
| `defaults.category_defaults` | mapping | `{}` | Compatibility alias for `backend_by_category`; ignored when that key is non-empty. | `category_defaults: {coding-cli: opencode}` |
| `defaults.compute_backend` | compute name | `local_subprocess` | Selects where the tool backend runs. The shipping value is unsandboxed. | `compute_backend: local_subprocess` |
| `defaults.compute` | compute name | `local_subprocess` | Compatibility alias for `compute_backend`; ignored when that key is present. | `compute: local-subprocess` |
| `defaults.base_branch` | str | `main` | Branch attempt checkouts are based on and leaf PRs target. | `base_branch: release/0.7` |
| `defaults.base_fetch` | bool | `true` | Refreshes `origin/<base_branch>` before creating an attempt checkout without moving the source checkout. | `base_fetch: false` |
| `defaults.max_concurrent` | positive int | `2` | Caps claims across autonomous poller/tool dispatch; the operator CLI is uncapped. | `max_concurrent: 4` |
| `defaults.reaper_ttl_s` | positive int | `7200` | Age in seconds after which the reaper may recover a claim or retained checkout with no heartbeat. Keep it above twice `timeout_s`. | `reaper_ttl_s: 10800` |
| `defaults.allow_autonomous_local_subprocess` | bool | `false` | Allows autonomous use of `local_subprocess`, which has shared filesystem access and no network isolation. This accepts that blast radius; the operator CLI is unaffected. | `allow_autonomous_local_subprocess: true` |
| `defaults.epic_branch_prefix` | str | `epic/` | Compatibility-only field retained after integrated epic execution was removed; no 0.7.0 runtime consumes it. | `epic_branch_prefix: "epic/"` |
| `defaults.max_review_retries` | positive int | `3` | Compatibility-only parsed field; no 0.7.0 runtime consumes it. | `max_review_retries: 3` |
| `defaults.max_claim_attempts` | positive int | `3` | Compatibility-only parsed field; no 0.7.0 runtime consumes it. | `max_claim_attempts: 5` |
| `defaults.reviewer_backend` | backend name | value of `defaults.backend` | Compatibility-only parsed field from integrated epic review; no 0.7.0 runtime consumes it. | `reviewer_backend: opencode` |
| `defaults.tiered_review` | mapping | framework defaults | Compatibility-only parsed review classifier; its child keys are described below. | `tiered_review: {multi_vote_reviewer_count: 5}` |

`defaults.trusted_test_retries` is **retired**, not an operator setting in
0.7.0. It belonged to the removed distributed trusted-test runner. A value in
YAML has no effect and must not be used as a retry guarantee; for example,
remove `trusted_test_retries: 1` from an older deployment file.

The remediation parser for `defaults.test_command` uses POSIX argument splitting
and a fixed system `PATH`; it does not run a shell. Use an executable available
there or an absolute path. Pipelines, redirects, glob expansion, `&&`, and
environment assignments are not interpreted. A leading `env -u NAME` (or
`env --unset NAME`) is supported, but `env NAME=value` is not. Thus this is a
valid non-Python configuration:

```yaml
defaults:
  test_command: "env -u NODE_OPTIONS /usr/bin/npm test -- --runInBand"
```

### Tiered review compatibility fields

These keys are parsed for old deployment files but are currently inert. If a
list is supplied, it replaces rather than extends the framework default.

| Key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `defaults.tiered_review.high_risk_scope_patterns` | list[str] | migration/schema, auth/credential, generated/lockfile, workflow/Docker/Terraform globs | Retains the old high-risk path patterns for config compatibility; no current reviewer consumes them. | `high_risk_scope_patterns: ["src/auth/**"]` |
| `defaults.tiered_review.high_risk_labels` | list[str] | `risk:high`, `security`, `auth`, `migration`, `prod-data`, `generated-code`, `hotspot` | Retains the old high-risk labels for config compatibility; no current reviewer consumes them. | `high_risk_labels: ["security"]` |
| `defaults.tiered_review.multi_vote_reviewer_count` | positive int | `3` | Retains the old reviewer count for config compatibility; no current reviewer consumes it. | `multi_vote_reviewer_count: 5` |

### Routes

`routes` defaults to `[]`; entries are evaluated in order and the first match
wins. Each route needs `backend` and at least one selector to be useful.

| Key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `routes[].backend` | backend name | required | Tool backend selected by this route. | `backend: feature_factory` |
| `routes[].label` | str | unset | Matches when the issue has this label. | `label: worklink:epic` |
| `routes[].repo` | str | unset | Matches this `owner/repo`. | `repo: acme/service` |
| `routes[].tool_category` | str | unset | Matches the requested backend tool category. | `tool_category: coding-cli` |
| `routes[].compute_backend` | compute name | `defaults.compute_backend` | Overrides the compute backend for this route. | `compute_backend: local_subprocess` |

Example:

```yaml
routes:
  - label: worklink:epic
    backend: feature_factory
    compute_backend: local_subprocess
```

### Backend blocks

`backends` defaults to `{}`. Only `opencode` and `feature_factory` ship in
0.7.0. An unknown referenced backend fails configuration loading; stale settings
for an unreferenced backend are warned and dropped.

| Key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `backends.opencode.bin` | str | `opencode` | Executable used for `opencode run`. | `bin: /usr/local/bin/opencode` |
| `backends.opencode.args` | list[str] | `[]` | Adds arguments not owned by Worklink. `-m`/`--model`, `--dir`, and `--` are rejected because Worklink supplies them. | `args: ["--format", "json"]` |
| `backends.opencode.bash_allowlist` | list[str] | `["git *", "uv *"]` | Replaces the deny-first shell command grants sent through `OPENCODE_PERMISSION`. Empty denies all shell commands; catch-all `*` is rejected. This is not a process sandbox. | `bash_allowlist: ["git *", "npm test*"]` |
| `backends.feature_factory.bin` | command string | `feature-factory` | Factory executable; unlike the OpenCode binary, this may contain multiple shell-split tokens. | `bin: "node /opt/factory/cli.js"` |
| `backends.feature_factory.args` | list[str] | `[]` | Extra arguments appended to the factory invocation. | `args: ["--verbose"]` |
| `backends.feature_factory.ready` | bool | `true` | Adds `--ready` when true. Use a YAML boolean; quoted `"false"` is truthy to the current parser. | `ready: false` |
| `backends.feature_factory.reviewer` | str | `MIMIR_FACTORY_REVIEWER` or `mimir-carreira` | Adds `--reviewer`; an empty string omits it. | `reviewer: release-team` |

`compute_backends` defaults to `{}`. The sole shipping block is
`compute_backends.local_subprocess` (hyphenated `local-subprocess` is normalized
to the same name), it defaults to `{}`, and it accepts no child settings.
Example: `compute_backends: {local_subprocess: {}}`.

### Tool pins

`tool_pins` defaults to `[]`. It is operator inventory for drift/bump tooling,
not an installer. Every item needs `name`, `category`, `pin`, and `smoke`; the
remaining fields are optional and default to unset.

| Key | Type | Default | Effect | Example |
|---|---|---|---|---|
| `tool_pins[].name` | str | required | Stable local tool name. | `name: opencode` |
| `tool_pins[].category` | str | required | Tool class used in maintenance output. | `category: coding-cli` |
| `tool_pins[].pin` | str | required | Expected version, tag, or SHA. | `pin: "1.18.9"` |
| `tool_pins[].smoke` | str | required | Command recorded as bump evidence; issue rendering does not execute it. | `smoke: "opencode --version"` |
| `tool_pins[].source` | str | unset | Upstream lookup strategy. | `source: npm` |
| `tool_pins[].package` | str | unset | Upstream package identifier. | `package: opencode-ai` |
| `tool_pins[].repo` | str | unset | Upstream repository identifier. | `repo: anomalyco/opencode` |
| `tool_pins[].install` | str | unset | Human-readable install surface. | `install: scaffold Dockerfile` |
| `tool_pins[].risk` | str | unset | Human-readable upgrade risk. | `risk: high` |

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
| `MIMIR_ENABLE_CLAUDE_CODE` | bool (`0`/`1`) | `0` | Build arg: `1` installs the Claude Code model adapter; the CLI is operator-provided. |
| `MIMIR_ENABLE_OPENCODE` | bool (`0`/`1`) | `0` | Scaffold/build arg: `1` installs and configures the OpenCode runtime and bundled plugins. It is not read by the Python runtime. |
| `MIMIR_EXTRAS` | csv-list | `anthropic,discord,slack,mcp` | pip extras build arg (`mimir-agent[...]`) in the PyPI-mode Dockerfile. |
| `MIMIR_QUOTA_POLL_ENABLED` | bool (`0`/`1`) | unset | Setup-generated compatibility marker for subscription routes. Provider-side polling is not implemented for all routes, so do not treat this marker alone as evidence that quota polling is active. |

The following code-visible names are explicitly **internal, not operator
environment variables**:

| Name | Classification |
|---|---|
| `MIMIR_HOME_GIT_TRACKING` | Historical internal design-document/implementation label; runtime configuration is `MIMIR_GIT_TRACKING_ENABLED`. |
| `MIMIR_COMPATIBILITY` | Optional model-adapter module attribute inspected with `getattr`; it is not read from the environment. |
| `MIMIR_CONFIG` | Reserved future alias mentioned by the internal SAGA loader; it is not implemented. Use `SAGA_CONFIG`. |
| `MIMIR_SPAWN_DEPTH` | Harness-owned recursion-depth marker set on child processes; it is not an operator setting. |

> `MIMIR_GIT_USER_NAME` / `MIMIR_GIT_USER_EMAIL` appear in scaffold comments as
> committer-identity overrides, but are **not currently wired to an env read** —
> the home-commit identity uses the built-in `mimir` / `noreply@mimir-agent.local`
> default. Track before relying on them.
