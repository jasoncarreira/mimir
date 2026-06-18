"""Read-mostly admin config/model/env snapshot for the React dashboard."""

from __future__ import annotations

import dataclasses
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .providers import provider_for_quota
from .scheduler import load_jobs


SECRET_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
)

_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@",
    re.IGNORECASE,
)

ENV_CATEGORIES: dict[str, tuple[str, ...]] = {
    "core": (
        "MIMIR_HOME",
        "MIMIR_AGENT_ID",
        "MIMIR_MODEL_SPEC",
        "MIMIR_MODEL_MAX_TOKENS",
        "MIMIR_MODEL_REASONING_EFFORT",
        "MIMIR_WEB_HOST",
        "MIMIR_WEB_PORT",
        "MIMIR_SCHEDULER_TZ",
    ),
    "model_provider": (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "MINIMAX_API_KEY",
    ),
    "bridges": (
        "DISCORD_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "BSKY_HANDLE",
        "BSKY_APP_PASSWORD",
    ),
    "ops": (
        "MIMIR_API_KEY",
        "MIMIR_OAUTH_USAGE_POLL_CRON",
        "MIMIR_MINIMAX_USAGE_POLL_CRON",
        "MIMIR_HEALTH_PROBE_CRON",
        "MIMIR_IDENTITIES_POPULATE_CRON",
        "MIMIR_GIT_TRACKING_ENABLED",
        "GITHUB_TOKEN",
    ),
}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _split_model_spec(model_spec: str) -> tuple[str, str]:
    provider, sep, model = (model_spec or "").partition(":")
    if not sep:
        return "", model_spec
    return provider, model


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def _redacted_value(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if _is_secret_name(name):
        return "[REDACTED]"
    return _redact_url_userinfo(value)


def _env_row(category: str, name: str) -> dict[str, Any]:
    value = os.environ.get(name)
    return {
        "name": name,
        "category": category,
        "present": value is not None and value != "",
        "secret": _is_secret_name(name),
        "value": _redacted_value(name, value),
        "mutable": False,
    }


def _env_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, names in ENV_CATEGORIES.items():
        for name in names:
            rows.append(_env_row(category, name))
            seen.add(name)
    for name in sorted(
        key for key in os.environ
        if key.startswith("MIMIR_") and key not in seen
    ):
        rows.append(_env_row("mimir_extra", name))
    return rows


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _redact_url_userinfo(value: str) -> str:
    """Mask credentials embedded in URL userinfo components."""

    return _URL_USERINFO_RE.sub(r"\g<prefix>[REDACTED]@", value)


def _redact_config_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-looking config fields at any nesting depth."""

    if key is not None and _is_secret_name(key):
        return "[REDACTED]" if value else ""
    if isinstance(value, dict):
        return {
            str(child_key): _redact_config_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_config_value(item) for item in value]
    if isinstance(value, str):
        return _redact_url_userinfo(value)
    return value


def _redacted_config(config: Config) -> dict[str, Any]:
    raw = _json_safe(dataclasses.asdict(config))
    if not isinstance(raw, dict):
        return {}
    redacted = _redact_config_value(raw)
    return redacted if isinstance(redacted, dict) else {}


def _schema_sections() -> list[dict[str, Any]]:
    return [
        {
            "id": "model",
            "label": "Model",
            "mutable": False,
            "fields": [
                {"name": "model_spec", "type": "string", "mutable": False},
                {"name": "model_max_tokens", "type": "integer", "mutable": False},
                {"name": "model_reasoning_effort", "type": "string", "mutable": False},
                {"name": "context_1m", "type": "boolean", "mutable": False},
            ],
        },
        {
            "id": "runtime",
            "label": "Runtime",
            "mutable": False,
            "fields": [
                {"name": "scheduler_tz", "type": "string", "mutable": False},
                {"name": "max_concurrent_turns", "type": "integer", "mutable": False},
                {"name": "tool_call_budget", "type": "integer", "mutable": False},
                {"name": "max_turn_iterations", "type": "integer", "mutable": False},
            ],
        },
        {
            "id": "env",
            "label": "Environment",
            "mutable": False,
            "fields": [
                {"name": "present", "type": "boolean", "mutable": False},
                {"name": "value", "type": "redacted-string", "mutable": False},
            ],
        },
    ]


def _schedules(home: Path | None) -> list[dict[str, Any]]:
    if home is None:
        return []
    rows: list[dict[str, Any]] = []
    for job in load_jobs(home / "scheduler.yaml"):
        rows.append({
            "name": job.name,
            "kind": "callable" if job.callable_name else "prompt",
            "cron": job.cron,
            "time_of_day": job.time_of_day,
            "channel_id": job.channel_id,
            "deliver": job.deliver,
            "priority": job.priority,
            "mutable": False,
        })
    return rows


def _pollers(scheduler: Any, home: Path | None) -> list[dict[str, Any]]:
    if scheduler is not None and hasattr(scheduler, "registered_poller_details"):
        try:
            return [
                {**row, "mutable": False}
                for row in scheduler.registered_poller_details()
            ]
        except Exception:
            pass
    if home is None:
        return []
    try:
        from .pollers import discover_pollers

        return [
            {
                "name": poller.name,
                "cron": poller.cron,
                "priority": poller.priority,
                "batch_size": poller.batch_size,
                "recover_failed_turns": poller.recover_failed_turns,
                "mutable": False,
            }
            for poller in discover_pollers(
                home / "skills",
                state_root=home / "state" / "pollers",
                overrides_path=home / "pollers-overrides.yaml",
            )
        ]
    except Exception:
        return []


def build_admin_config_payload(
    *,
    config: Config | None,
    scheduler: Any = None,
    home: Path | None = None,
) -> dict[str, Any]:
    effective_home = home or getattr(config, "home", None)
    model_spec = str(getattr(config, "model_spec", os.environ.get("MIMIR_MODEL_SPEC", "")) or "")
    provider_prefix, model_name = _split_model_spec(model_spec)
    anthropic_base_url = str(getattr(config, "anthropic_base_url", os.environ.get("ANTHROPIC_BASE_URL", "")) or "")
    provider = provider_for_quota(model_spec, anthropic_base_url)
    context_1m = bool(getattr(config, "context_1m", False))
    model_max_tokens = int(getattr(config, "model_max_tokens", 0) or 0)

    return {
        "generated_at": _now_iso(),
        "model": {
            "model_spec": model_spec,
            "provider_prefix": provider_prefix,
            "model": model_name,
            "provider": provider.name,
            "anthropic_base_url_present": bool(anthropic_base_url),
            "context_window": "1m beta" if context_1m else "provider default",
            "context_1m_enabled": context_1m,
            "resource_window": {
                "billing_mode": str(getattr(config, "billing_mode", "")),
                "usage_block_enabled": bool(getattr(config, "usage_block_enabled", False)),
                "capture_rate_limits": bool(getattr(config, "capture_rate_limits", False)),
                "max_output_tokens": model_max_tokens or None,
            },
        },
        "schema_sections": _schema_sections(),
        "schedules": _schedules(effective_home),
        "pollers": _pollers(scheduler, effective_home),
        "env": _env_rows(),
        "raw_config": _redacted_config(config) if config is not None else {},
        "mutation_policy": {
            "mode": "read_only_v1",
            "mutable_fields": [],
            "reveal_secret_values": False,
            "reveal_path": None,
            "edit_path": None,
            "rate_limited": False,
        },
    }
