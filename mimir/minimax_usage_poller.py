"""Minimax coding-plan usage poller.

Polls Minimax's coding-plan remains endpoint and writes
``RateLimitSnapshot`` entries to :class:`RateLimitStore` under the
keys :class:`mimir.billing.MinimaxQuotaProvider` reads:

* ``minimax_five_hour`` — interval window (5h, confirmed by the
  endpoint's ``start_time``/``end_time`` pair on a Plus-tier
  ``MiniMax-M*`` plan: 2026-05-21 14:00:00 UTC → 19:00:00 UTC = 5h).
* ``minimax_seven_day`` — weekly window (``weekly_start_time`` →
  ``weekly_end_time``).

Endpoint
--------
``GET https://www.minimax.io/v1/api/openplatform/coding_plan/remains``
with ``Authorization: Bearer <MINIMAX_API_KEY>``.

Notes:

* The endpoint lives on ``www.minimax.io``, NOT ``api.minimax.io``.
  ``api.minimax.io`` 404s every guessed billing path.
* A browser-shaped ``User-Agent`` is required — the default
  ``Python-urllib/3.11`` UA gets a 403 Forbidden from the gateway.
* Response shape (per response, milliseconds). The endpoint has shipped
  two variants:

  **Token plan (current, 2026-06+)** — buckets keyed by CATEGORY
  (``general`` for chat models, ``video``), quota reported as a remaining
  PERCENT; the request-count fields are zeroed:

  .. code-block:: json

      {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "model_remains": [
          {
            "model_name": "general",
            "start_time": 1780326000000, "end_time": 1780344000000,
            "current_interval_total_count": 0, "current_interval_usage_count": 0,
            "current_interval_remaining_percent": 83,
            "weekly_start_time": 1780272000000, "weekly_end_time": 1780876800000,
            "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
            "current_weekly_remaining_percent": 97
          },
          {"model_name": "video", "...": "..."}
        ]
      }

  **Coding plan (legacy)** — buckets keyed by model glob (``MiniMax-M*``),
  quota reported as request COUNTS (``current_interval_total_count`` /
  ``..._usage_count``, e.g. 4500 / 4152).

* Utilization: prefer ``current_interval_remaining_percent`` /
  ``current_weekly_remaining_percent`` (token-plan shape) →
  ``utilization = (100 - remaining_percent) / 100``. Fall back to the legacy
  count math when the percent field is absent. NOTE on the legacy shape:
  **``usage_count`` is REMAINING, not USED** (confirmed by the reference js
  poller in ``~/projects/odin/usage``) → ``used = max(0, total - remaining)``,
  ``utilization = used / total``.

Design
------
* No OAuth refresh — single static API key.
* Synchronous logical model: one HTTP fetch per tick → two snapshot
  writes → one ``minimax_usage_ok`` event (or ``_failed`` on errors).
* Errors NEVER propagate — they surface as events so the scheduler
  job loop stays alive across transient network blips.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .event_logger import log_event
from .rate_limits import RateLimitSnapshot, RateLimitStore


REMAINS_ENDPOINT = (
    "https://www.minimax.io/v1/api/openplatform/coding_plan/remains"
)

DEFAULT_MODEL_NAME = "general"
"""The chat/text plan bucket. As of 2026-06-01 Minimax's
coding_plan/remains endpoint groups all chat models under the CATEGORY
name ``general`` (with ``video`` as the other category) — the old
per-capability buckets keyed by model glob (``MiniMax-M*``, plus
``speech-hd`` / ``music-*`` / ``image-01`` / ``coding-plan-*``) went away
when the account moved from a Coding Plan to a Token Plan. A trailing
``*`` is still honoured by :func:`pick_model_entry` for forward-compat,
but the live chat bucket name is now an exact ``general``."""

DEFAULT_USER_AGENT = "Mozilla/5.0 (mimir/minimax-usage-poller)"
"""Browser-shaped UA. The endpoint's gateway rejects the default
``Python-urllib/3.11`` UA with a 403."""

DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass
class MinimaxPollerConfig:
    """Configuration for one poller instance."""

    api_key: str
    model_name: str = DEFAULT_MODEL_NAME
    endpoint: str = REMAINS_ENDPOINT
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


class MinimaxFetchError(Exception):
    """Raised when the HTTP fetch fails or the response shape is
    malformed. Callers in ``poll_once`` catch this and convert it to
    a ``minimax_usage_failed`` event."""


# ─── HTTP fetch ────────────────────────────────────────────────────────


async def fetch_remains(
    cfg: MinimaxPollerConfig,
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """One HTTP fetch of the remains endpoint. Returns the parsed
    JSON body on success.

    Raises :class:`MinimaxFetchError` on:

    * HTTP non-2xx
    * Non-JSON body
    * ``base_resp.status_code != 0`` (Minimax's in-band error marker)

    The ``session`` argument lets tests inject a mock client. When
    ``None``, a fresh session is created and closed around the call.
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        try:
            async with session.get(
                cfg.endpoint,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": cfg.user_agent,
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_seconds),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise MinimaxFetchError(
                        f"HTTP {resp.status}: {text[:200]}"
                    )
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise MinimaxFetchError(
                        f"non-JSON response: {exc}; body: {text[:200]}"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise MinimaxFetchError(
                f"client error: {type(exc).__name__}: {exc}"
            ) from exc
    finally:
        if owns_session:
            await session.close()

    if not isinstance(data, dict):
        raise MinimaxFetchError(f"response not a JSON object: {type(data).__name__}")
    base = data.get("base_resp") or {}
    if not isinstance(base, dict):
        raise MinimaxFetchError(f"base_resp not a dict: {type(base).__name__}")
    if base.get("status_code") != 0:
        raise MinimaxFetchError(
            f"base_resp.status_code={base.get('status_code')}: "
            f"{base.get('status_msg')!r}"
        )
    return data


# ─── Response parsing ──────────────────────────────────────────────────


def pick_model_entry(
    data: dict[str, Any], model_name: str,
) -> dict[str, Any] | None:
    """Find the ``model_remains`` entry whose ``model_name`` matches
    ``model_name``. A trailing ``*`` is treated as a prefix wildcard
    (mirroring Minimax's own ``MiniMax-M*`` naming for the chat
    models bucket). Returns ``None`` when nothing matches.
    """
    pattern = model_name
    if pattern.endswith("*"):
        prefix = pattern[:-1]

        def _matches(name: str) -> bool:
            return name == pattern or name.startswith(prefix)
    else:
        def _matches(name: str) -> bool:
            return name == pattern

    for entry in data.get("model_remains") or []:
        if not isinstance(entry, dict):
            continue
        if _matches(entry.get("model_name", "")):
            return entry
    return None


def _utilization(total: Any, remaining: Any) -> float:
    """Convert (``total``, ``remaining``) → ``utilization`` (0-1).

    Minimax's response uses ``usage_count`` for remaining (NOT used),
    confirmed by the reference js poller in ``~/projects/odin/usage``.
    """
    if not isinstance(total, (int, float)) or total <= 0:
        return 0.0
    if not isinstance(remaining, (int, float)):
        return 0.0
    used = max(0, total - remaining)
    return min(1.0, max(0.0, used / total))


def _utilization_from_remaining_percent(remaining_percent: Any) -> float | None:
    """Token-plan shape (2026-06+): Minimax reports quota as a remaining
    PERCENT (``current_interval_remaining_percent`` /
    ``current_weekly_remaining_percent``) rather than request counts, which
    are zeroed. ``utilization = (100 - remaining) / 100``. Returns ``None``
    when the field is absent / non-numeric so the caller can fall back to
    the legacy count-based computation."""
    if isinstance(remaining_percent, bool):  # bool is an int subclass — reject
        return None
    if not isinstance(remaining_percent, (int, float)):
        return None
    return min(1.0, max(0.0, (100.0 - float(remaining_percent)) / 100.0))


def _ms_to_unix(value: Any) -> int | None:
    """Convert a millisecond timestamp (Minimax's wire format) to
    unix seconds. Returns ``None`` for non-numeric / missing values
    so ``resets_at`` survives a malformed response."""
    if not isinstance(value, (int, float)):
        return None
    return int(value / 1000)


def interval_snapshot(entry: dict[str, Any]) -> RateLimitSnapshot:
    """5h-window snapshot from a model_remains entry.

    Prefers the token-plan ``current_interval_remaining_percent``; falls
    back to the legacy count-based (used/total) computation when the percent
    field is absent (older coding-plan response shape)."""
    util = _utilization_from_remaining_percent(
        entry.get("current_interval_remaining_percent")
    )
    if util is None:
        util = _utilization(
            entry.get("current_interval_total_count"),
            entry.get("current_interval_usage_count"),
        )
    return RateLimitSnapshot(
        status="allowed",
        utilization=util,
        resets_at=_ms_to_unix(entry.get("end_time")),
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def weekly_snapshot(entry: dict[str, Any]) -> RateLimitSnapshot:
    """7d-window snapshot from a model_remains entry.

    Prefers the token-plan ``current_weekly_remaining_percent``; falls back
    to the legacy count-based (used/total) computation when the percent
    field is absent (older coding-plan response shape)."""
    util = _utilization_from_remaining_percent(
        entry.get("current_weekly_remaining_percent")
    )
    if util is None:
        util = _utilization(
            entry.get("current_weekly_total_count"),
            entry.get("current_weekly_usage_count"),
        )
    return RateLimitSnapshot(
        status="allowed",
        utilization=util,
        resets_at=_ms_to_unix(entry.get("weekly_end_time")),
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
    )


# ─── Orchestration ────────────────────────────────────────────────────


async def poll_once(
    cfg: MinimaxPollerConfig,
    store: RateLimitStore,
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """One poll cycle. Never raises — all failure modes surface as
    ``minimax_usage_failed`` events. Returns a summary dict for tests
    / introspection.

    Steps:

    1. Fetch the remains endpoint.
    2. Pick the model_remains entry matching ``cfg.model_name``.
    3. Drop empty-plan responses (totals=0 means the API key's plan
       doesn't cover this model — writing utilization=0 would be
       misleading).
    4. Write both snapshots to the store.
    5. Emit ``minimax_usage_ok`` with the recorded values.
    """
    try:
        data = await fetch_remains(cfg, session=session)
    except MinimaxFetchError as exc:
        await log_event(
            "minimax_usage_failed",
            stage="fetch",
            error=str(exc),
        )
        return {"ok": False, "stage": "fetch", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — defensive; aiohttp can surface unexpected types
        await log_event(
            "minimax_usage_failed",
            stage="fetch",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "stage": "fetch", "error": str(exc)}

    entry = pick_model_entry(data, cfg.model_name)
    if entry is None:
        await log_event(
            "minimax_usage_failed",
            stage="model_match",
            error=f"no model_remains entry matching {cfg.model_name!r}",
            available=[
                (e.get("model_name") if isinstance(e, dict) else None)
                for e in (data.get("model_remains") or [])
            ],
        )
        return {"ok": False, "stage": "model_match"}

    interval_total = entry.get("current_interval_total_count") or 0
    weekly_total = entry.get("current_weekly_total_count") or 0
    interval_pct = entry.get("current_interval_remaining_percent")
    weekly_pct = entry.get("current_weekly_remaining_percent")
    # A bucket is "empty" only when NEITHER window carries usable data — no
    # request counts (legacy coding-plan shape) AND no remaining_percent
    # (current token-plan shape, where counts are always 0 and the real
    # quota lives in the percent fields). A bare ``totals == 0`` check would
    # mis-classify a live token plan as an uncovered model and spam
    # minimax_usage_failed every poll.
    has_interval = bool(interval_total) or interval_pct is not None
    has_weekly = bool(weekly_total) or weekly_pct is not None
    if not has_interval and not has_weekly:
        # Genuinely no data in either window = the API key's plan doesn't
        # cover this model. Writing utilization=0 here would be a lie.
        await log_event(
            "minimax_usage_failed",
            stage="empty_plan",
            error=(
                f"no interval/weekly counts or remaining_percent for "
                f"{entry.get('model_name')!r} — plan does not cover this model"
            ),
        )
        return {"ok": False, "stage": "empty_plan"}

    snapshots: dict[str, RateLimitSnapshot] = {
        "minimax_five_hour": interval_snapshot(entry),
        "minimax_seven_day": weekly_snapshot(entry),
    }
    for key, snap in snapshots.items():
        await store.record(key, snap)

    await log_event(
        "minimax_usage_ok",
        model_name=entry.get("model_name"),
        recorded={
            key: {
                "utilization": snap.utilization,
                "resets_at": snap.resets_at,
                "status": snap.status,
            }
            for key, snap in snapshots.items()
        },
    )
    return {"ok": True, "snapshots": snapshots, "model_name": entry.get("model_name")}
