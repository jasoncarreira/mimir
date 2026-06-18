"""Read-only admin config/model/env payloads for the React UI."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from .config import Config
from .model_registry import provider_for_quota
from .pollers import discover_pollers
from .redaction import redact_payload
from .scheduler import load_jobs


SECRET_FIELD_HINTS = (
    "api_key",
    "token",
    "password",
    "secret",
    "credential",
)

ENV_CATEGORIES: dict[str, tuple[str, ...]] = {
    "model": (
        "MIMIR_MODEL",
        "MIMIR_MODEL_SPEC",
        "MIMIR_MODEL_MAX_RETRIES",
        "MIMIR_MODEL_MAX_TOKENS",
        "MIMIR_MODEL_REASONING_EFFORT",
        "MIMIR_CONTEXT_1M",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    ),
    "secrets": (
        "MIMIR_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "DISCORD_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "BSKY_APP_PASSWORD",
        "GITHUB_TOKEN",
        "MINIMAX_API_KEY",
    ),
    "schedules": (
        "MIMIR_SCHEDULER_TZ",
        "MIMIR_SAGA_CONSOLIDATE_CRON",
        "MIMIR_COMMITMENTS_DUE_CHECK_CRON",
        "MIMIR_INTROSPECTION_REPORT_CRON",
        "MIMIR_OAUTH_USAGE_POLL_CRON",
        "MIMIR_MINIMAX_USAGE_POLL_CRON",
        "MIMIR_HEALTH_PROBE_CRON",
        "MIMIR_IDENTITIES_POPULATE_CRON",
    ),
    "server": (
        "MIMIR_HOME",
        "MIMIR_AGENT_ID",
        "MIMIR_WEB_HOST",
        "MIMIR_WEB_PORT",
        "MIMIR_ALLOW_UNAUTHENTICATED",
        "MIMIR_ACCESS_CONTROL_ENFORCED",
    ),
    "limits": (
        "MIMIR_MAX_CONCURRENT_TURNS",
        "MIMIR_MAX_CHANNEL_QUEUE",
        "MIMIR_TOOL_CALL_BUDGET",
        "MIMIR_MAX_TURN_ITERATIONS",
        "MIMIR_TURN_TIMEOUT_SECONDS",
        "MIMIR_POST_TURN_TIMEOUT_SECONDS",
        "MIMIR_DRAIN_TIMEOUT_SECONDS",
        "MIMIR_MAX_TURNS",
        "MIMIR_MAX_EVENTS",
    ),
}


READ_ONLY_FIELDS = frozenset({
    "model_spec",
    "model",
    "provider",
    "context_1m",
    "scheduler_tz",
    "schedules",
    "pollers",
    "env",
    "raw_config",
})

MUTABLE_FIELDS: frozenset[str] = frozenset()


def _is_secret_name(name: str) -> bool:
    norm = name.lower()
    return any(hint in norm for hint in SECRET_FIELD_HINTS)


def _redacted_value(name: str, value: Any) -> Any:
    if _is_secret_name(name):
        return "[REDACTED]" if value else None
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _redact_config_dict(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _redacted_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redacted_value(name, item) for item in value]
    return redact_payload(value)


def _redact_config_dict(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: _redacted_value(key, value) for key, value in raw.items()}


def _schema_for_config() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for field in dataclasses.fields(Config):
        fields.append(
            {
                "name": field.name,
                "type": getattr(field.type, "__name__", str(field.type)),
                "mutable": field.name in MUTABLE_FIELDS,
                "secret": _is_secret_name(field.name),
            }
        )
    return fields


def _model_payload(config: Config) -> dict[str, Any]:
    spec = str(config.model_spec or "")
    provider_prefix, _, model_name = spec.partition(":")
    provider = provider_for_quota(spec)
    context_window_tokens = None
    context_window_note = "unknown"
    if spec.startswith("anthropic:") and "claude" in spec.lower():
        context_window_tokens = 1_000_000 if config.context_1m else 200_000
        context_window_note = "1M beta enabled" if config.context_1m else "base Claude window"
    elif spec.startswith("claude-code:"):
        context_window_tokens = 200_000
        context_window_note = "claude-code route; subprocess provider window"

    return {
        "model_spec": spec,
        "provider_prefix": provider_prefix or "unknown",
        "model_name": model_name or spec,
        "provider": provider.name,
        "subscription_provider": provider.subscription_provider,
        "billing_mode": str(config.billing_mode.value if hasattr(config.billing_mode, "value") else config.billing_mode),
        "context_window": {
            "tokens": context_window_tokens,
            "context_1m_enabled": bool(config.context_1m),
            "note": context_window_note,
        },
        "resource_window": {
            "tool_call_budget": config.tool_call_budget,
            "max_turn_iterations": config.max_turn_iterations,
            "turn_timeout_seconds": config.turn_timeout_seconds,
            "post_turn_timeout_seconds": config.post_turn_timeout_seconds,
            "max_concurrent_turns": config.max_concurrent_turns,
            "max_channel_queue": config.max_channel_queue,
            "model_max_tokens": config.model_max_tokens,
        },
    }


def _schedule_payload(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for job in load_jobs(home / "scheduler.yaml"):
        out.append(
            {
                "name": job.name,
                "kind": "callable" if job.callable_name else "prompt",
                "cron": job.cron,
                "time_of_day": job.time_of_day,
                "channel_id": job.channel_id,
                "deliver": job.deliver,
                "priority": job.priority,
                "misfire_grace_time": job.misfire_grace_time,
                "prompt_configured": bool(job.prompt or job.prompt_file),
                "prompt_file": job.prompt_file,
                "callable": job.callable_name,
            }
        )
    return out


def _poller_payload(home: Path) -> list[dict[str, Any]]:
    pollers = discover_pollers(
        home / "skills",
        state_root=home / "state" / "pollers",
        overrides_path=home / "pollers-overrides.yaml",
    )
    out: list[dict[str, Any]] = []
    for poller in pollers:
        out.append(
            {
                "name": poller.name,
                "cron": poller.cron,
                "channel_id": poller.channel_id(),
                "priority": poller.priority,
                "batch_size": poller.batch_size,
                "recover_failed_turns": poller.recover_failed_turns,
                "deliver": poller.deliver,
                "manifest_path": str(poller.manifest_path) if poller.manifest_path else None,
                "env_keys": sorted(poller.env),
                "pass_env": sorted(poller.pass_env),
                "env_required": sorted(poller.env_required),
            }
        )
    return out


def _env_payload() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, names in ENV_CATEGORIES.items():
        for name in names:
            seen.add(name)
            rows.append(_env_row(name, category))
    for name in sorted(k for k in os.environ if k.startswith("MIMIR_") and k not in seen):
        rows.append(_env_row(name, "other_mimir"))
    return rows


def _env_row(name: str, category: str) -> dict[str, Any]:
    present = name in os.environ and os.environ.get(name, "") != ""
    secret = _is_secret_name(name) or category == "secrets"
    return {
        "name": name,
        "category": category,
        "present": present,
        "secret": secret,
        "value": "[REDACTED]" if present and secret else (os.environ.get(name) if present else None),
    }


def build_admin_config_payload(config: Config) -> dict[str, Any]:
    """Return the v1 admin config payload. No secret values are exposed."""
    raw_config = _redact_config_dict(dataclasses.asdict(config))
    return {
        "model": _model_payload(config),
        "schema": {
            "sections": [
                {"id": "model", "title": "Model and provider", "mutable": False},
                {"id": "schedules", "title": "Scheduler jobs", "mutable": False},
                {"id": "pollers", "title": "Poller manifests", "mutable": False},
                {"id": "env", "title": "Environment", "mutable": False},
                {"id": "raw_config", "title": "Raw config", "mutable": False},
            ],
            "fields": _schema_for_config(),
        },
        "schedules": _schedule_payload(config.home),
        "pollers": _poller_payload(config.home),
        "env": _env_payload(),
        "raw_config": raw_config,
        "capabilities": {
            "read_only": sorted(READ_ONLY_FIELDS),
            "mutable": sorted(MUTABLE_FIELDS),
            "secret_reveal": {
                "available": False,
                "reason": "omitted in v1; no backend route returns secret values",
            },
            "edits": {
                "available": False,
                "reason": "omitted in v1 pending explicit field allowlist and rate limits",
            },
        },
    }
