"""Env-based configuration. Defaults match SPEC §14."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .billing import BillingMode, detect_billing_mode


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


# Hard ceilings on the JSONL log caps. The reflection skill filters to the
# last 7 days, the algedonic prompt block bounds itself by hours+limit, and
# tail-streamed reads keep prompt-assembly cost O(window) regardless of
# total log size — so the practical limit is on-disk weight, not prompt
# cost. At ~400 bytes/event observed, 750k events ≈ 300 MB; that's the
# upper bound an operator should ever set without shipping logs to an
# external store.
#
# Events cap is 15× the turns cap — measured ~14 events/turn on mimir as
# of 2026-05 (turn_started/finished, event_queued bursts, saga_feedback_sent
# per batch, async-bash start/done pairs, tool_call_denied/retry events).
# Up from the original 5× target as tool-call density grew through 2026
# Q2; if events.jsonl outpaces turns.jsonl on retention again, raise again.
#
# Defaults at 5k turns / 75k events give roughly 30+ days of retention at
# current rates, which is the window most behavioral audits assume.
_TURNS_CAP_DEFAULT = 5_000
_TURNS_CAP_MAX = 50_000
_EVENTS_PER_TURN_RATIO = 15
_EVENTS_CAP_DEFAULT = _TURNS_CAP_DEFAULT * _EVENTS_PER_TURN_RATIO
_EVENTS_CAP_MAX = _TURNS_CAP_MAX * _EVENTS_PER_TURN_RATIO


def _turns_cap() -> int:
    return min(_env_int("MIMIR_MAX_TURNS", _TURNS_CAP_DEFAULT), _TURNS_CAP_MAX)


def _events_cap() -> int:
    return min(_env_int("MIMIR_MAX_EVENTS", _EVENTS_CAP_DEFAULT), _EVENTS_CAP_MAX)


def _oauth_credentials_path() -> Path | None:
    """Resolve the OAuth credentials path. Empty string explicitly
    disables (useful in tests / non-OAuth deployments). Unset prefers
    ``$MIMIR_HOME/.claude/.credentials.json`` — co-located with the
    rest of mimir's persistent state so the credentials file rides
    whatever bind mount / volume the operator has set up for the
    homedir. Falls back to ``$HOME/.claude/.credentials.json`` (where
    ``claude /login`` writes by default) only if MIMIR_HOME is unset.

    Containerized deployments without bind mounts otherwise see the
    refresh-token rotation get blown away on every container rebuild,
    forcing a re-login. Anchoring on MIMIR_HOME removes that footgun
    while still letting bare-metal operators keep credentials in their
    user home."""
    raw = os.environ.get("MIMIR_CLAUDE_OAUTH_CREDENTIALS")
    if raw is not None and not raw.strip():
        # Explicitly empty → disable.
        return None
    if raw is not None:
        return Path(raw).expanduser().resolve()
    mimir_home = os.environ.get("MIMIR_HOME")
    if mimir_home:
        return Path(mimir_home).expanduser().resolve() / ".claude" / ".credentials.json"
    home = os.environ.get("HOME") or ""
    if not home:
        return None
    return Path(home) / ".claude" / ".credentials.json"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_sources(raw: str) -> frozenset[str] | None:
    """Parse a comma-separated source allowlist for Recent activity.

    - ``""`` / unset → ``frozenset()`` (allow nothing — bench-friendly default
      when MIMIR_RECENT_SOURCES is explicitly set to empty)
    - ``"*"`` or ``"all"`` → ``None`` (allow every source, including legacy
      records with ``source=None``)
    - otherwise → ``frozenset`` of comma-split tokens after strip+lower
    """
    raw = (raw or "").strip()
    if raw in {"*", "all"}:
        return None
    if not raw:
        return frozenset()
    tokens = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    return frozenset(tokens)


@dataclass
class Config:
    home: Path
    model: str
    effort: str
    embed_model: str
    saga_endpoint: str
    saga_api_key: str
    web_port: int

    # Concurrency (§4.5)
    max_concurrent_turns: int
    max_channel_queue: int
    worker_idle_timeout_s: int

    # Message buffer (§5.4)
    history_global_max: int
    history_per_channel_max: int
    recent_per_channel: int
    recent_author_cross: int
    recent_cross_hours: int
    # Allowlist of Message.source values that participate in the Recent
    # activity prompt section (open-strix's exact filter — its allowlist is
    # ``{"discord","web","stdin"}`` hard-coded in app.py:734). Empty
    # frozenset means "allow nothing", None means "allow all sources".
    recent_sources: frozenset[str] | None
    # Per-message render cap for Recent activity (chars). 0 = no cap.
    recent_message_chars: int

    # SAGA session (§5.6)
    saga_session_idle_minutes: int
    saga_consolidate_cron: str

    # Weekly event-introspection report (FEEDBACK-LOOPS §4.7) + heartbeat
    # health monitor (§4.8). Empty string disables the cron. Default is
    # Friday 14:00 UTC so the report lands before reflection (Sun 06:00)
    # and operator review on Monday morning.
    introspection_report_cron: str
    introspection_report_days: int
    introspection_report_health_threshold: float
    introspection_report_emit_algedonic: bool

    # Per-atom confidence floor (post SAGA per-atom gating) for the
    # pre-message auto-fetch hook. Empty string (default) defers to SAGA's
    # ``[retrieval].default_min_confidence_tier`` config (today: "low").
    # Override with "medium"/"high" only when bench data shows weak hits
    # are net-negative; "low" is fine post SAGA commit 29efa38 which fixed
    # the bug where keyword-only-pathway atoms had ``_confidence_tier``
    # unset and got dropped at any floor ≥ low.
    saga_pre_message_min_tier: str

    # send_message circuit breaker (§7.2.4)
    send_loop_soft_limit: int
    send_loop_hard_limit: int
    send_loop_similarity: float

    # Per-turn tool-call budget — caps panic-search loops on probe retries.
    # 0 disables the cap entirely.
    tool_call_budget: int

    # Additional file-op roots beyond ``home``. File-op tools (Read,
    # Glob, Grep, Edit, Write, MultiEdit, NotebookEdit) accept paths
    # inside any of these. Useful for deployments where the agent
    # operates on sibling mounts: mimirbot reads/edits its own source
    # at ``/workspace/mimir`` and the bench harness at ``/benchmark``.
    # Configured via ``MIMIR_FILE_OP_ROOTS`` (colon-separated paths);
    # empty by default (just ``home``).
    file_op_extra_roots: list[Path]

    # SDK gateway (§14.1)
    anthropic_api_key: str
    anthropic_base_url: str
    anthropic_auth_token: str
    anthropic_model: str
    anthropic_custom_model_option: str
    disable_experimental_betas: str

    # Prompts override
    prompts_dir: Path | None

    # Channels
    discord_token: str
    slack_bot_token: str
    slack_app_token: str
    bsky_handle: str
    bsky_app_password: str

    # Identity reconciliation (FUTURE_WORK §6.1). Defaults true — operators
    # who want strict per-platform isolation (compliance, regulated workflows)
    # can set MIMIR_CROSS_PLATFORM_PULL=false to disable cross-platform pull
    # without removing state/identities.yaml.
    cross_platform_pull: bool

    # Operator alert channel (v0.4 §6) — channel_id the agent uses for
    # high-priority signals to the operator that don't fit the current
    # conversation (critical errors, urgent heartbeat findings, dispatch
    # failures). Empty default means feature inactive — the system prompt
    # omits the line entirely and the alert skill no-ops. Just a normal
    # channel id; the registered bridge dispatches by prefix.
    operator_alert_channel: str

    # Server-side API key for the public injection endpoint (POST /event).
    # When set, requests must carry a matching ``X-API-Key`` header or get
    # 401. Empty default means no auth — fine for development on
    # localhost, but the server binds to 0.0.0.0 so any production
    # deployment should set this. Without it, an attacker who can reach
    # the server can steer the agent into the synthesis path against an
    # arbitrary session id (and call saga_end_session etc.).
    api_key: str

    # Algedonic surfacing (v0.4 §2). Window for the Recent feedback
    # signals prompt section; per-polarity cap on rendered items. 0 for
    # the limit disables the section entirely. Tune small if the prompt
    # is getting noisy.
    feedback_window_hours: int
    feedback_limit_per_polarity: int

    # Session boundary surfacing (v0.4 §3). Number of recent session
    # boundaries to render in the prompt's "Recent session summaries"
    # block, scoped to the current channel. 0 disables the section.
    recent_boundaries: int
    # Staleness markers on the Unfinished sub-bullet (chainlink #63).
    # When a summary's age >= ``stale_age_hours`` OR turns-since-boundary
    # on the same channel >= ``stale_turns``, the Unfinished header gets
    # a ``[verify before quoting]`` suffix nudging the
    # verify-before-claim rule. Either signal alone is enough to fire.
    unfinished_stale_age_hours: int
    unfinished_stale_turns: int

    # Usage block in the turn prompt: enable/disable, plus optional
    # dollar budgets that gate the "% of budget" annotation. The 5h /
    # weekly windows match Anthropic's Max-plan rolling-window shape;
    # these are *operator-set dollar ceilings*, complementary to the
    # plan's unit budget that the SDK's RateLimitEvent stream reports
    # (captured separately in mimir/rate_limits.py and rendered as
    # "Plan windows" in the same prompt section). Leave blank to skip
    # the dollar-threshold annotation.
    usage_block_enabled: bool
    usage_5h_limit_usd: float
    usage_weekly_limit_usd: float

    # Cost-rate alert. Two thresholds, both optional:
    # - cost_hourly_limit_usd: absolute ceiling. 0 disables.
    # - cost_rate_spike_ratio: multiplier of the rolling-week per-hour
    #   baseline. Default 3.0; 0 disables. Adapts to your usual spend
    #   so a sleeper agent that wakes up briefly doesn't false-positive.
    # - cost_rate_spike_floor_usd: rate_now floor below which the spike
    #   check is silenced regardless of ratio. Default $5.00/hr — a
    #   second line of defense for "weird shape, not yet at ceiling"
    #   that lets normal working sessions (a few dollars/hour) pass
    #   without tripping. 0 disables (revert to baseline-only gating).
    # cost_alert_cooldown_minutes: minimum interval between
    #   ``cost_rate_alert`` events landing in events.jsonl. The
    #   algedonic surfacing keeps showing the most recent alert until
    #   it ages out of the window, so re-emitting per turn adds no
    #   information. Default 60.
    cost_hourly_limit_usd: float
    cost_rate_spike_ratio: float
    cost_rate_spike_floor_usd: float
    cost_alert_cooldown_minutes: int

    # Billing-mode-aware suppression (chainlink #13). ``quota`` mode
    # treats plan-window on-pace projection as the binding constraint
    # (zero marginal cost up to the cap) and demotes cost-rate spikes
    # to advisory; ``pay-as-you-go`` keeps the existing spike_ratio
    # path. Auto-detected by default — see ``mimir.billing.detect_billing_mode``.
    # Override via ``MIMIR_BILLING_MODE=quota|pay-as-you-go``.
    billing_mode: BillingMode

    # OAuth usage poller (mimir/oauth_usage_poller.py): hits Anthropic's
    # /api/oauth/usage to populate plan-window utilization% in the
    # RateLimitStore. Requires credentials minted by ``claude /login``
    # (which grants user:profile scope) — the headless setup-token
    # flow's user:inference-only scope can't read this endpoint.
    # ``oauth_credentials_path`` is the path to credentials.json;
    # default is $HOME/.claude/.credentials.json. Empty string disables
    # the poller. ``oauth_usage_poll_cron`` is the cron expression;
    # leave blank to disable. ``oauth_refresh_warn_days`` is the soft
    # heuristic threshold (days since first observed credentials) at
    # which we emit ``oauth_refresh_token_age_warn`` so the operator
    # knows to consider re-/login before the refresh token expires.
    oauth_credentials_path: Path | None
    oauth_usage_poll_cron: str
    oauth_refresh_warn_days: int

    # Bind-mount health probe (mimir/health_probe.py): detects the
    # VirtioFS stale-inode failure mode and self-restarts via SIGTERM
    # to PID 1 so Docker's restart-unless-stopped policy re-mounts
    # cleanly. ``health_probe_cron`` is the cron expression; default
    # is every minute. Empty disables. ``health_probe_max_restarts_per_hour``
    # is the sliding-window guard threshold — past N restarts in 60min
    # we stop self-restarting and surface ``bind_mount_stale_persistent``
    # for operator action.
    health_probe_cron: str
    health_probe_max_restarts_per_hour: int

    # Identities populator (mimir/identities_populator.py): scrapes
    # connected Discord guilds + Slack workspaces into
    # ``state/identities.yaml`` so the registry stays current without
    # operator hand-curation. ``identities_populate_cron`` is the cron
    # expression; default empty (disabled) — operator opt-in via
    # ``MIMIR_IDENTITIES_POPULATE_CRON`` because bridge scrapes are
    # platform-API hits that shouldn't fire by default in environments
    # where it'd be surprising. Recommended value: ``0 6 * * *`` (daily
    # at 06:00 UTC). The populator is idempotent — rerun → zero deltas,
    # operator-set fields preserved, atomic writeback.
    identities_populate_cron: str

    # Per-response rate-limit capture (default on). Enabling this turns
    # on the SDK's include_partial_messages so StreamEvent messages
    # carry the raw Anthropic streaming events; we filter for
    # ``message_start`` and read its ``rate_limits`` block. Without
    # this, mimir only sees rate-limit data when the SDK emits a
    # transition event (allowed → allowed_warning → rejected) — fine
    # for "scale back when warned" but the Plan windows section is
    # empty most of the time. Cost: extra streaming chunks parsed per
    # turn (cheap; local IPC, dict lookups). Disable to skip the
    # streaming overhead at the cost of less-current plan-window data.
    capture_rate_limits: bool

    # Opt into Anthropic's 1M-context-window beta for Claude 4.x
    # Opus / Sonnet (header ``context-1m-2025-08-07``). Default on:
    # mimir's typical prompt size (300-600k tokens with full memory +
    # session summaries + recent activity + SAGA hits) is well past
    # the 200k bare-model cap, so without this the API silently
    # truncates or rejects oversize prompts. Set
    # ``MIMIR_CONTEXT_1M=false`` to disable (e.g. when running against
    # an account / model variant that doesn't support the beta).
    context_1m: bool

    # PR 4a (MIMIR_HOME_GIT_TRACKING): post-turn git commit + debounced
    # push for /mimir-home. PR 4b ships the allowlist gitignore +
    # pre-commit secret-scan hook + ``git_bootstrap.bootstrap_git_repo``
    # called from ``mimir setup`` and from ``server._on_startup``, and
    # flips this default to True. Operators that want to opt out (e.g.
    # standalone CI runs, transient containers) can still set
    # ``MIMIR_GIT_TRACKING_ENABLED=false`` in the env.
    git_tracking_enabled: bool
    # PR 4b: optional remote-bootstrap inputs. When both are set, the
    # bootstrap function clones the remote into an empty home (or
    # configures origin + pulls --ff-only on an existing repo). When
    # either is missing, the bootstrap falls back to ``git init`` +
    # bootstrap commit (no remote, no auto-push). Token is URL-encoded
    # into the embedded HTTPS URL — never logged in cleartext.
    git_state_repo: str | None
    git_state_token: str | None

    # Logging — JSONL caps clamped to [1, _LOG_CAP_MAX]. Default 1000.
    # Both files are tail-streamed at read time, so the cap is mostly
    # about cumulative on-disk size; the trim logic uses 10% hysteresis
    # to amortize rewrite cost.
    max_turns_kept: int
    max_events_kept: int
    turns_archive_dir: Path | None

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(_env("MIMIR_HOME") or Path.cwd()).resolve()
        prompts_override = _env("MIMIR_PROMPTS_DIR")
        archive_dir = _env("MIMIR_TURNS_ARCHIVE_DIR")
        # Resolve once — used by both ``billing_mode`` (to detect QUOTA
        # vs API_KEY billing) and ``oauth_credentials_path`` (the field
        # itself). Computing it twice was redundant and could in theory
        # diverge if env state shifted between the two calls.
        oauth_credentials_path = _oauth_credentials_path()

        return cls(
            home=home,
            model=_env("MIMIR_MODEL", "claude-opus-4-7"),
            effort=_env("MIMIR_EFFORT", "high"),
            embed_model=_env("MIMIR_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
            saga_endpoint=_env("SAGA_ENDPOINT", "http://localhost:3002"),
            saga_api_key=_env("SAGA_API_KEY"),
            web_port=_env_int("MIMIR_WEB_PORT", 8080),

            max_concurrent_turns=_env_int("MIMIR_MAX_CONCURRENT_TURNS", 10),
            max_channel_queue=_env_int("MIMIR_MAX_CHANNEL_QUEUE", 100),
            worker_idle_timeout_s=_env_int("MIMIR_WORKER_IDLE_TIMEOUT_S", 60),

            history_global_max=_env_int("MIMIR_HISTORY_GLOBAL_MAX", 500),
            history_per_channel_max=_env_int("MIMIR_HISTORY_PER_CHANNEL_MAX", 250),
            recent_per_channel=_env_int("MIMIR_RECENT_PER_CHANNEL", 10),
            recent_author_cross=_env_int("MIMIR_RECENT_AUTHOR_CROSS", 10),
            recent_cross_hours=_env_int("MIMIR_RECENT_CROSS_HOURS", 24),
            recent_sources=_parse_sources(
                _env("MIMIR_RECENT_SOURCES", "slack,discord,bluesky,web,stdin")
            ),
            recent_message_chars=_env_int("MIMIR_RECENT_MESSAGE_CHARS", 4096),

            saga_session_idle_minutes=_env_int("MIMIR_SAGA_SESSION_IDLE_MINUTES", 10),
            saga_consolidate_cron=_env("MIMIR_SAGA_CONSOLIDATE_CRON", "0 4 * * *"),
            introspection_report_cron=_env(
                "MIMIR_INTROSPECTION_REPORT_CRON", "0 14 * * 5",
            ),
            introspection_report_days=_env_int(
                "MIMIR_INTROSPECTION_REPORT_DAYS", 7,
            ),
            introspection_report_health_threshold=_env_float(
                "MIMIR_INTROSPECTION_HEALTH_THRESHOLD", 0.80,
            ),
            introspection_report_emit_algedonic=_env_bool(
                "MIMIR_INTROSPECTION_EMIT_ALGEDONIC", True,
            ),
            saga_pre_message_min_tier=_env("MIMIR_SAGA_PRE_MSG_MIN_TIER", ""),

            send_loop_soft_limit=_env_int("MIMIR_SEND_LOOP_SOFT_LIMIT", 5),
            send_loop_hard_limit=_env_int("MIMIR_SEND_LOOP_HARD_LIMIT", 10),
            send_loop_similarity=_env_float("MIMIR_SEND_LOOP_SIMILARITY", 0.9),
            tool_call_budget=_env_int("MIMIR_TOOL_CALL_BUDGET", 120),
            file_op_extra_roots=[
                Path(p)
                for p in (_env("MIMIR_FILE_OP_ROOTS", "") or "").split(":")
                if p.strip()
            ],

            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_base_url=_env("ANTHROPIC_BASE_URL"),
            anthropic_auth_token=_env("ANTHROPIC_AUTH_TOKEN"),
            anthropic_model=_env("ANTHROPIC_MODEL"),
            anthropic_custom_model_option=_env("ANTHROPIC_CUSTOM_MODEL_OPTION"),
            disable_experimental_betas=_env("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"),

            prompts_dir=Path(prompts_override).resolve() if prompts_override else None,

            discord_token=_env("DISCORD_TOKEN"),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_app_token=_env("SLACK_APP_TOKEN"),
            bsky_handle=_env("BSKY_HANDLE"),
            bsky_app_password=_env("BSKY_APP_PASSWORD"),

            cross_platform_pull=_env("MIMIR_CROSS_PLATFORM_PULL", "true").lower()
                not in {"false", "0", "no"},

            operator_alert_channel=_env("MIMIR_OPERATOR_ALERT_CHANNEL"),
            api_key=_env("MIMIR_API_KEY"),

            feedback_window_hours=_env_int("MIMIR_FEEDBACK_WINDOW_HOURS", 24),
            feedback_limit_per_polarity=_env_int("MIMIR_FEEDBACK_LIMIT", 5),

            recent_boundaries=_env_int("MIMIR_RECENT_BOUNDARIES", 3),
            unfinished_stale_age_hours=_env_int("MIMIR_UNFINISHED_STALE_AGE_HOURS", 2),
            unfinished_stale_turns=_env_int("MIMIR_UNFINISHED_STALE_TURNS", 5),

            usage_block_enabled=_env("MIMIR_USAGE_BLOCK", "true").lower()
                not in {"false", "0", "no", "off"},
            usage_5h_limit_usd=_env_float("MIMIR_USAGE_5H_LIMIT_USD", 0.0),
            usage_weekly_limit_usd=_env_float("MIMIR_USAGE_WEEKLY_LIMIT_USD", 0.0),

            cost_hourly_limit_usd=_env_float("MIMIR_COST_HOURLY_LIMIT_USD", 0.0),
            cost_rate_spike_ratio=_env_float("MIMIR_COST_RATE_SPIKE_RATIO", 3.0),
            cost_rate_spike_floor_usd=_env_float(
                "MIMIR_COST_RATE_SPIKE_FLOOR_USD", 5.00,
            ),
            cost_alert_cooldown_minutes=_env_int(
                "MIMIR_COST_ALERT_COOLDOWN_MINUTES", 60,
            ),

            billing_mode=detect_billing_mode(
                explicit=_env("MIMIR_BILLING_MODE") or None,
                oauth_credentials_path=oauth_credentials_path,
            ),

            capture_rate_limits=_env("MIMIR_CAPTURE_RATE_LIMITS", "true").lower()
                not in {"false", "0", "no", "off"},

            context_1m=_env("MIMIR_CONTEXT_1M", "true").lower()
                not in {"false", "0", "no", "off"},

            oauth_credentials_path=oauth_credentials_path,
            oauth_usage_poll_cron=_env(
                "MIMIR_OAUTH_USAGE_POLL_CRON", "*/3 * * * *",
            ),
            oauth_refresh_warn_days=_env_int(
                "MIMIR_OAUTH_REFRESH_WARN_DAYS", 25,
            ),

            health_probe_cron=_env(
                "MIMIR_HEALTH_PROBE_CRON", "* * * * *",
            ),
            health_probe_max_restarts_per_hour=_env_int(
                "MIMIR_HEALTH_PROBE_MAX_RESTARTS_PER_HOUR", 3,
            ),

            identities_populate_cron=_env(
                "MIMIR_IDENTITIES_POPULATE_CRON", "",
            ),

            git_tracking_enabled=_env_bool("MIMIR_GIT_TRACKING_ENABLED", True),
            git_state_repo=_env("MIMIR_STATE_REPO"),
            git_state_token=_env("GITHUB_TOKEN"),

            max_turns_kept=_turns_cap(),
            max_events_kept=_events_cap(),
            turns_archive_dir=Path(archive_dir).resolve() if archive_dir else None,
        )

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def turns_log(self) -> Path:
        return self.logs_dir / "turns.jsonl"

    @property
    def events_log(self) -> Path:
        return self.logs_dir / "events.jsonl"

    def sdk_env_overrides(self) -> dict[str, str]:
        """Env vars to forward via ClaudeAgentOptions.env (§14.1)."""
        out = {}
        for key, val in [
            ("ANTHROPIC_BASE_URL", self.anthropic_base_url),
            ("ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token),
            ("ANTHROPIC_MODEL", self.anthropic_model),
            ("ANTHROPIC_CUSTOM_MODEL_OPTION", self.anthropic_custom_model_option),
            ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", self.disable_experimental_betas),
        ]:
            if val:
                out[key] = val
        return out
