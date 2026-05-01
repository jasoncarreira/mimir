"""Env-based configuration. Defaults match SPEC §14."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


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
    msam_endpoint: str
    msam_api_key: str
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

    # MSAM session (§5.6)
    msam_session_idle_minutes: int
    msam_consolidate_cron: str
    # Per-atom confidence floor (post MSAM per-atom gating) for the
    # pre-message auto-fetch hook. Empty string (default) defers to MSAM's
    # ``[retrieval].default_min_confidence_tier`` config (today: "low").
    # Override with "medium"/"high" only when bench data shows weak hits
    # are net-negative; "low" is fine post MSAM commit 29efa38 which fixed
    # the bug where keyword-only-pathway atoms had ``_confidence_tier``
    # unset and got dropped at any floor ≥ low.
    msam_pre_message_min_tier: str

    # send_message circuit breaker (§7.2.4)
    send_loop_soft_limit: int
    send_loop_hard_limit: int
    send_loop_similarity: float

    # Per-turn tool-call budget — caps panic-search loops on probe retries.
    # 0 disables the cap entirely.
    tool_call_budget: int

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

    # Logging
    max_turns_kept: int
    max_events_kept: int | None
    turns_archive_dir: Path | None

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(_env("MIMIR_HOME") or Path.cwd()).resolve()
        prompts_override = _env("MIMIR_PROMPTS_DIR")
        archive_dir = _env("MIMIR_TURNS_ARCHIVE_DIR")
        max_events_raw = _env("MIMIR_MAX_EVENTS")

        return cls(
            home=home,
            model=_env("MIMIR_MODEL", "claude-opus-4-7"),
            effort=_env("MIMIR_EFFORT", "high"),
            embed_model=_env("MIMIR_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
            msam_endpoint=_env("MSAM_ENDPOINT", "http://localhost:3002"),
            msam_api_key=_env("MSAM_API_KEY"),
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

            msam_session_idle_minutes=_env_int("MIMIR_MSAM_SESSION_IDLE_MINUTES", 10),
            msam_consolidate_cron=_env("MIMIR_MSAM_CONSOLIDATE_CRON", "0 4 * * 0"),
            msam_pre_message_min_tier=_env("MIMIR_MSAM_PRE_MSG_MIN_TIER", ""),

            send_loop_soft_limit=_env_int("MIMIR_SEND_LOOP_SOFT_LIMIT", 5),
            send_loop_hard_limit=_env_int("MIMIR_SEND_LOOP_HARD_LIMIT", 10),
            send_loop_similarity=_env_float("MIMIR_SEND_LOOP_SIMILARITY", 0.9),
            tool_call_budget=_env_int("MIMIR_TOOL_CALL_BUDGET", 30),

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

            feedback_window_hours=_env_int("MIMIR_FEEDBACK_WINDOW_HOURS", 24),
            feedback_limit_per_polarity=_env_int("MIMIR_FEEDBACK_LIMIT", 5),

            recent_boundaries=_env_int("MIMIR_RECENT_BOUNDARIES", 3),

            max_turns_kept=_env_int("MIMIR_MAX_TURNS", 1000),
            max_events_kept=int(max_events_raw) if max_events_raw else None,
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
