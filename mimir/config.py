"""Env-based configuration. Defaults match SPEC §14."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .billing import BillingMode, detect_billing_mode


log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # chainlink #259: match _env_bool's posture — a typo
        # (MIMIR_WEB_PORT=808O) warns + falls back rather than crashing
        # boot with an opaque traceback.
        log.warning(
            "%s=%r is not a valid integer; using default %r",
            name, raw, default,
        )
        return default


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


# Per-directory write permissions under MIMIR_HOME. ``"rw"`` allows
# Write/Edit/upload via deepagents' filesystem tools; ``"ro"`` blocks
# them at the WriteGuardBackend layer (reads/grep/glob unrestricted).
# Anything not in this dict — e.g. ``.mimir/`` (saga db, metrics) — is
# implicitly blocked because it's not a writable root. Operators
# override via MIMIR_FOLDERS.
DEFAULT_FOLDERS: dict[str, str] = {
    "state": "rw",       # Agent state files (commitments, sessions, etc.)
    "memory": "rw",      # Long-form text journal
    "attachments": "rw", # Agent-generated artifacts
    "scratch": "rw",     # Ephemeral working area (drafts, throwaway clones,
                         # scratch notes). Gitignored — not tracked state. The
                         # blessed place for working files so the agent doesn't
                         # invent ad-hoc dirs the write-guard then blocks (#299).
    "skills": "rw",      # Operator-supplied skill bundles (agent can refine)
    "logs": "ro",        # System-managed event/turn logs
    "messages": "ro",    # Channel history (read-only — never rewrite)
    "prompts": "ro",     # Operator-managed prompt overrides
}


def _turns_cap() -> int:
    return min(_env_int("MIMIR_MAX_TURNS", _TURNS_CAP_DEFAULT), _TURNS_CAP_MAX)


def _events_cap() -> int:
    return min(_env_int("MIMIR_MAX_EVENTS", _EVENTS_CAP_DEFAULT), _EVENTS_CAP_MAX)


def _is_anthropic_oauth_deployment() -> bool:
    """True when this deployment talks to Anthropic's actual OAuth-
    backed API (or no remote endpoint at all). False when the operator
    routes ``ANTHROPIC_BASE_URL`` at a non-Anthropic compat endpoint
    (Minimax / Moonshot Kimi / a gateway / etc.) — in which case
    Anthropic's ``/api/oauth/usage`` endpoint is meaningless and
    polling it would emit ``oauth_usage_failed`` every cron tick.

    Heuristic: parse ``ANTHROPIC_BASE_URL``. Unset / empty / pointing
    at ``api.anthropic.com`` → real Anthropic. Anything else → a
    proxy / compat endpoint where the OAuth usage poller has no
    useful work to do."""
    raw = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not raw:
        return True
    try:
        # ``urlparse().hostname`` already lowercases the host, so no
        # extra ``.lower()`` needed (mimir-carreira review note on PR
        # #246).
        host = urlparse(raw).hostname or ""
    except (ValueError, AttributeError):
        return True  # malformed → assume Anthropic; the SDK will error elsewhere
    return host == "api.anthropic.com"


def _oauth_credentials_path() -> Path | None:
    """Resolve the OAuth credentials path. Empty string explicitly
    disables (useful in tests / non-OAuth deployments). Unset prefers
    ``$MIMIR_HOME/.claude/.credentials.json`` — co-located with the
    rest of mimir's persistent state so the credentials file rides
    whatever bind mount / volume the operator has set up for the
    homedir. Falls back to ``$HOME/.claude/.credentials.json`` (where
    ``claude /login`` writes by default) only if MIMIR_HOME is unset.

    Returns ``None`` when ``ANTHROPIC_BASE_URL`` points at a non-
    Anthropic compat endpoint (Minimax / Kimi / gateway). The OAuth
    usage endpoint (``/api/oauth/usage``) is Anthropic-specific; on
    a routed deployment it doesn't exist + would emit
    ``oauth_usage_failed`` every cron tick. The agent's chat model
    still works fine via the routed endpoint — only the usage poller
    is auto-disabled. Operators can force the poller back on with
    an explicit ``MIMIR_CLAUDE_OAUTH_CREDENTIALS=<path>`` env.

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
        # Explicit path wins, even on a routed deployment (operator
        # opt-in: "I know what I'm doing, poll Anthropic anyway").
        return Path(raw).expanduser().resolve()
    # No explicit env override. Auto-disable on non-Anthropic
    # deployments (Minimax / Kimi / etc.) — the poller has no useful
    # endpoint to hit and would just spam ``oauth_usage_failed``.
    if not _is_anthropic_oauth_deployment():
        return None
    mimir_home = os.environ.get("MIMIR_HOME")
    if mimir_home:
        return Path(mimir_home).expanduser().resolve() / ".claude" / ".credentials.json"
    home = os.environ.get("HOME") or ""
    if not home:
        return None
    return Path(home) / ".claude" / ".credentials.json"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a valid float; using default %r",
            name, raw, default,
        )
        return default


# chainlink #238: canonical env-bool truthy/falsy alphabets.
# Used by every Config bool field; pre-fix the file carried three
# different parsers with subtly-different truthy semantics
# (e.g. ``allow_unauthenticated`` rejected ``on`` while ``_env_bool``
# accepted it). Garbage values now warn instead of silently coercing
# to True (the prior inline pattern) or False (``allow_unauthenticated``).
_ENV_BOOL_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})
_ENV_BOOL_FALSY = frozenset({"0", "false", "no", "off", "n"})


def _env_bool(name: str, default: bool) -> bool:
    """Parse an env-var value as boolean.

    Truthy: ``{1, true, yes, on, y}`` (case-insensitive, whitespace-
    trimmed).  Falsy: ``{0, false, no, off, n}``.  Empty / unset
    returns *default*. Anything else emits ``log.warning`` and returns
    *default* so a typo doesn't silently flip a flag.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    norm = raw.strip().lower()
    if norm in _ENV_BOOL_TRUTHY:
        return True
    if norm in _ENV_BOOL_FALSY:
        return False
    log.warning(
        "%s=%r is not a recognised boolean (truthy=%s, falsy=%s); using default %r",
        name,
        raw,
        sorted(_ENV_BOOL_TRUTHY),
        sorted(_ENV_BOOL_FALSY),
        default,
    )
    return default


def _load_mcp_servers_from_env() -> list:
    """Load MCP server configs from MIMIR_MCP_SERVERS_JSON / _PATH env.

    Inline JSON wins over path. Returns an empty list if both unset
    (MCP is opt-in). Import is local so config doesn't pay the mcp/
    LangChain import cost when MCP isn't configured.
    """
    json_inline = os.environ.get("MIMIR_MCP_SERVERS_JSON", "").strip()
    json_path = os.environ.get("MIMIR_MCP_SERVERS_PATH", "").strip()
    if not json_inline and not json_path:
        return []
    from .mcp_client import load_mcp_server_configs
    return load_mcp_server_configs(
        json_inline=json_inline or None,
        json_path=json_path or None,
    )


def _parse_folders(raw: str) -> dict[str, str]:
    """Parse a ``MIMIR_FOLDERS`` env value into a name→mode dict.

    Format: ``name:mode`` pairs, comma-separated. Modes other than
    ``rw``/``ro`` are coerced to ``ro`` (fail safe). Empty/unset
    returns ``DEFAULT_FOLDERS``.

    Unsafe names — ``""``, ``"."``, ``".."``, anything containing
    traversal segments — are rejected with a warning. Pre-fix a bogus
    spec like ``.:rw`` would alias the root and make EVERY directory
    writable (including ``.mimir/db.sqlite``). Malformed input as a
    whole (no parseable pairs) also logs at warning level rather than
    silently restoring defaults — operator typo visibility matters
    more than backwards compat with the silent fallback.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    raw = (raw or "").strip()
    if not raw:
        return dict(DEFAULT_FOLDERS)
    folders: dict[str, str] = {}
    had_pairs = False
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        had_pairs = True
        name, mode = pair.split(":", 1)
        name = name.strip().strip("/")
        mode = mode.strip().lower()
        if not name or name in (".", ".."):
            _log.warning(
                "MIMIR_FOLDERS: ignoring unsafe folder name %r "
                "(empty or root-aliasing)", pair,
            )
            continue
        # Reject any pathlike with traversal — ``a/../b``, ``../x``, etc.
        if "/" in name or "\\" in name or name.startswith("."):
            _log.warning(
                "MIMIR_FOLDERS: ignoring suspicious folder name %r "
                "(path separators or leading dot)", pair,
            )
            continue
        if mode not in ("rw", "ro"):
            _log.warning(
                "MIMIR_FOLDERS: unknown mode %r for folder %r — "
                "coercing to 'ro' (fail safe)", mode, name,
            )
            mode = "ro"
        folders[name] = mode
    if not folders:
        if had_pairs:
            _log.warning(
                "MIMIR_FOLDERS=%r produced no valid folders — "
                "falling back to DEFAULT_FOLDERS", raw,
            )
        return dict(DEFAULT_FOLDERS)
    return folders


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
    # Logical agent name — tags every TurnRecord + event record so a
    # cross-process operator running two agents on the same
    # infrastructure can disambiguate output by agent without grepping
    # by MIMIR_HOME path. Default "mimir"; multi-agent deployments
    # override via MIMIR_AGENT_ID. Has no effect on a single-agent
    # deployment.
    agent_id: str
    model: str
    # Post-cutover model spec for the deepagents path:
    #   - ``claude-code:<model>``  → ChatClaudeCode (Max OAuth subprocess)
    #   - ``<provider>:<model>``   → init_chat_model via langchain
    #
    # Common forms:
    #   claude-code:claude-sonnet-4-6     (default; free under Max plan)
    #   anthropic:claude-haiku-4-5        (direct API, paid)
    #   openai:gpt-4.1-mini               (direct OpenAI)
    #
    # Non-Anthropic / non-OpenAI providers that expose an
    # Anthropic-compat endpoint (Minimax, Moonshot Kimi) ride the
    # ``anthropic:`` provider with ``ANTHROPIC_BASE_URL`` overridden:
    #   ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic
    #   ANTHROPIC_API_KEY=<minimax key>
    #   MIMIR_MODEL_SPEC=anthropic:MiniMax-M2.7
    # Anthropic-compat is preferred over OpenAI-compat for reasoning
    # models because the provider converts reasoning to proper
    # ``thinking`` content blocks server-side, vs. the OAI-compat
    # path's inline ``<think>...</think>`` tags in content.
    #
    # See ``mimir.agent._resolve_model``. Defaults to
    # ``claude-code:claude-sonnet-4-6`` (Max OAuth, no API-key billing).
    model_spec: str

    # Per-call retry budget for non-claude-code providers (anthropic,
    # openai, voyage, etc.). Threaded into ``init_chat_model`` via
    # ``max_retries=...``; provider SDKs use this for transient 429 /
    # 5xx backoff. claude-code path ignores this — the subprocess
    # handles its own retry semantics. Default 6 (matches open-strix).
    model_max_retries: int
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
    saga_session_max_turns: int
    saga_consolidate_cron: str

    # Timezone the scheduler interprets all cron expressions in
    # (scheduler.yaml jobs, saga-consolidate, introspection-report,
    # commitments-due-check, every poller). APScheduler ZoneInfo
    # string — e.g., "UTC", "America/New_York", "Europe/London".
    # Default UTC matches the pre-PR behavior (all cron expressions
    # treated as UTC); operators deploying in a non-UTC region set
    # this so they can author scheduler.yaml in local wall-clock time
    # without mentally subtracting hours twice a year for DST.
    # Invalid values fall back to UTC with a logged warning rather
    # than crashing the scheduler — wrong-but-offset is preferable to
    # agent-offline. ZoneInfo handles DST automatically via the
    # system's tzdata.
    scheduler_tz: str

    # Commitments Phase 2b — periodic due-check sweep emits
    # commitment_due / commitment_expired / commitment_snooze_pileup
    # algedonic events. Empty string disables the cron entirely.
    # 5-min default is fine-grained enough that an explicit "remind
    # me at 14:00" surfaces within 5 min of 14:00, coarse-grained
    # enough that the sweep cost (replay + 0–N writes) is negligible.
    commitments_due_check_cron: str
    # Pileup threshold: when a single commitment's snooze_count
    # reaches this, the poller fires commitment_snooze_pileup.
    # Default 3 — "you've punted this thing 3 times already, time
    # to commit or dismiss."
    commitments_snooze_pileup_threshold: int

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

    # Per-directory write permissions under ``home``. Subdir name →
    # ``"rw"`` or ``"ro"``. ``WriteGuardBackend`` enforces this at the
    # filesystem-tool layer (Write/Edit/upload blocked outside writable
    # roots; reads/grep/glob unrestricted). Operator-configurable via
    # ``MIMIR_FOLDERS`` (``name:mode`` pairs, comma-separated, e.g.
    # ``state:rw,memory:rw,logs:ro``); falls back to ``DEFAULT_FOLDERS``
    # when unset. Unknown modes are coerced to ``"ro"`` (fail safe).
    folders: dict[str, str]

    # MCP servers to spawn at startup. Each entry is an
    # ``MCPServerConfig`` (name, command, args, env). Tools exposed by
    # the servers are bridged as ``mcp_{server}_{tool}`` LangChain
    # tools and appended to the agent's tool surface. Operator-supplied
    # via ``MIMIR_MCP_SERVERS_JSON`` (inline JSON list) or
    # ``MIMIR_MCP_SERVERS_PATH`` (path to a JSON file). Both forms
    # accept the Claude-Code-style ``{"mcpServers": [...]}`` wrapper
    # or a bare list. Empty by default — MCP is opt-in.
    mcp_servers: list  # type: list[MCPServerConfig], avoiding the import

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
    # loopback (127.0.0.1, the default ``web_host``). Binding any
    # non-loopback interface (``0.0.0.0`` or a specific external IP)
    # requires a non-empty ``api_key``; the server refuses to start
    # otherwise. Without auth on a network-reachable port, an attacker
    # who can reach the port can steer the agent into the synthesis
    # path against an arbitrary session id (and call saga_end_session
    # etc.).
    api_key: str

    # HTTP bind address. Default ``127.0.0.1`` — loopback-only, the safe
    # posture for a process the operator interacts with locally.
    # ``MIMIR_WEB_HOST=0.0.0.0`` (or a specific IP) exposes the port to
    # the network and requires ``MIMIR_API_KEY`` to be set; startup
    # refuses the combination of a non-loopback bind + missing key.
    # Docker / k8s deployments typically set this to ``0.0.0.0`` and
    # also supply an API key via the orchestrator's secret store.
    web_host: str

    # Set MIMIR_ALLOW_UNAUTHENTICATED=true to suppress the startup
    # security warning when MIMIR_API_KEY is empty. For development /
    # localhost use only; production deployments should set MIMIR_API_KEY.
    allow_unauthenticated: bool

    # Per-turn wall-clock timeout in seconds. 0 = no timeout (bench/dev).
    # Default 1800 (30 min) catches indefinitely hung turns while
    # allowing legitimate long heartbeat or reflection work.
    turn_timeout_seconds: int

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

    # Minimax usage poller (mimir/minimax_usage_poller.py): hits
    # ``www.minimax.io/v1/api/openplatform/coding_plan/remains`` and
    # writes ``minimax_five_hour`` / ``minimax_seven_day`` snapshots
    # to ``RateLimitStore``. Independent of the Anthropic OAuth poller;
    # typical deployments use one or the other based on which gateway
    # the agent talks to. Disabled by default (empty cron) — operator
    # opts in by setting ``MIMIR_MINIMAX_USAGE_POLL_CRON`` (e.g.
    # ``*/3 * * * *`` to match the Anthropic poller cadence) and
    # providing ``MINIMAX_API_KEY``.
    minimax_usage_poll_cron: str
    minimax_usage_model_name: str

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

    # Per-call OUTPUT token cap for non-claude-code providers, threaded into
    # ``init_chat_model`` via ``max_tokens=...`` (see ``mimir.agent._resolve_model``).
    # 0 (default) leaves the provider default in place. RAISE IT for
    # thinking-via-Anthropic-compat models (Minimax / Kimi): their reasoning
    # blocks count against the output budget, so a small default (e.g. 4096)
    # can be consumed entirely by thinking — the turn hits ``max_tokens``
    # mid-reasoning and returns an empty response. Declared last with a
    # default so direct ``Config(...)`` constructions stay non-breaking.
    model_max_tokens: int = 0

    # Reasoning effort for providers that support it. Forwarded to Codex Plus
    # (default "none" — mimir's cheap-inference baseline) and to OpenAI
    # reasoning models; Anthropic/minimax/claude-code ignore it (they use a
    # thinking budget, not an effort level). "" leaves each provider's default.
    model_reasoning_effort: str = ""

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
            agent_id=_env("MIMIR_AGENT_ID", "mimir"),
            model=_env("MIMIR_MODEL", "claude-opus-4-7"),
            model_spec=_env("MIMIR_MODEL_SPEC", "claude-code:claude-sonnet-4-6"),
            model_max_retries=_env_int("MIMIR_MODEL_MAX_RETRIES", 6),
            model_max_tokens=_env_int("MIMIR_MODEL_MAX_TOKENS", 0),
            model_reasoning_effort=_env("MIMIR_MODEL_REASONING_EFFORT", ""),
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
            saga_session_max_turns=_env_int("MIMIR_SAGA_SESSION_MAX_TURNS", 10),
            saga_consolidate_cron=_env("MIMIR_SAGA_CONSOLIDATE_CRON", "0 4 * * *"),
            scheduler_tz=_env("MIMIR_SCHEDULER_TZ", "UTC"),
            commitments_due_check_cron=_env(
                "MIMIR_COMMITMENTS_DUE_CHECK_CRON", "*/5 * * * *",
            ),
            commitments_snooze_pileup_threshold=_env_int(
                "MIMIR_COMMITMENTS_SNOOZE_PILEUP_THRESHOLD", 3,
            ),
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
            folders=_parse_folders(_env("MIMIR_FOLDERS", "") or ""),
            mcp_servers=_load_mcp_servers_from_env(),

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

            cross_platform_pull=_env_bool("MIMIR_CROSS_PLATFORM_PULL", True),

            operator_alert_channel=_env("MIMIR_OPERATOR_ALERT_CHANNEL"),
            api_key=_env("MIMIR_API_KEY"),
            web_host=_env("MIMIR_WEB_HOST", "127.0.0.1"),
            allow_unauthenticated=_env_bool("MIMIR_ALLOW_UNAUTHENTICATED", False),
            turn_timeout_seconds=_env_int("MIMIR_TURN_TIMEOUT_SECONDS", 3600),

            feedback_window_hours=_env_int("MIMIR_FEEDBACK_WINDOW_HOURS", 24),
            feedback_limit_per_polarity=_env_int("MIMIR_FEEDBACK_LIMIT", 5),

            recent_boundaries=_env_int("MIMIR_RECENT_BOUNDARIES", 3),
            unfinished_stale_age_hours=_env_int("MIMIR_UNFINISHED_STALE_AGE_HOURS", 2),
            unfinished_stale_turns=_env_int("MIMIR_UNFINISHED_STALE_TURNS", 5),

            usage_block_enabled=_env_bool("MIMIR_USAGE_BLOCK", True),
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
                # chainlink #315: a codex-plus subscription spec is itself a
                # QUOTA signal, so a Codex-only install keeps its quota view
                # without relying on stale Anthropic creds.
                model_spec=_env("MIMIR_MODEL_SPEC", "claude-code:claude-sonnet-4-6"),
            ),

            capture_rate_limits=_env_bool("MIMIR_CAPTURE_RATE_LIMITS", True),

            context_1m=_env_bool("MIMIR_CONTEXT_1M", True),

            oauth_credentials_path=oauth_credentials_path,
            oauth_usage_poll_cron=_env(
                "MIMIR_OAUTH_USAGE_POLL_CRON", "*/3 * * * *",
            ),
            oauth_refresh_warn_days=_env_int(
                "MIMIR_OAUTH_REFRESH_WARN_DAYS", 25,
            ),

            # Default to empty (disabled). Minimax deployments opt in
            # by setting both this env + MINIMAX_API_KEY. The poller's
            # server-side registration in server.py double-checks the
            # API key is present before scheduling.
            minimax_usage_poll_cron=_env(
                "MIMIR_MINIMAX_USAGE_POLL_CRON", "",
            ),
            minimax_usage_model_name=_env(
                # Minimax's coding_plan/remains endpoint keys buckets by
                # category now ("general" for chat, "video"), not the old
                # "MiniMax-M*" model glob (Coding Plan → Token Plan, 2026-06).
                "MIMIR_MINIMAX_USAGE_MODEL", "general",
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

    @property
    def commitments_log(self) -> Path:
        """Append-only commitments JSONL. Lives under ``.mimir/`` (not
        ``state/``) so the indexer doesn't walk it as searchable
        knowledge — it's internal lifecycle state, not content."""
        return self.home / ".mimir" / "commitments.jsonl"

    @property
    def writable_dirs(self) -> list[str]:
        """Subdir names with ``"rw"`` mode in ``folders``. Passed to
        ``WriteGuardBackend`` so deepagents' Write/Edit/upload tools
        block paths outside these roots. Order preserves dict order
        (insertion order from ``_parse_folders``)."""
        return [name for name, mode in self.folders.items() if mode == "rw"]

    @property
    def all_dirs(self) -> list[str]:
        """Every subdir named in ``folders``, both rw and ro. Useful
        for surfacing the agent-visible workspace layout in the system
        prompt or for diagnostics."""
        return list(self.folders.keys())

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
