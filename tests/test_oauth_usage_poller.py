"""Tests for mimir/oauth_usage_poller.py.

Covers:
- credentials.json read/write round-trip
- expiry detection (with skew)
- refresh-grant happy path + invalid_grant logged_out path + transport error
- /api/oauth/usage fetch + 401-then-refresh-then-retry
- record_usage writes per-window snapshots into RateLimitStore
- poll_once orchestration:
  * happy path: emits oauth_usage_ok
  * expired access token → refresh + fetch → emits oauth_refresh_ok + oauth_usage_ok
  * refresh fails (logged out) → emits oauth_logged_out
  * refresh-token-age threshold → emits oauth_refresh_token_age_warn
  * read-credentials failure → emits oauth_usage_failed (stage=read_credentials)

Network calls are intercepted via aiohttp_client mocks where practical;
where the orchestration is what we're checking, we monkeypatch the
http functions directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mimir import oauth_usage_poller as op
from mimir.oauth_usage_poller import (
    OAuthRefreshError,
    PollerConfig,
    UsageFetchError,
    days_since_first_login,
    is_access_token_expired,
    poll_once,
    read_credentials,
    record_first_seen,
    record_usage,
    reset_first_seen,
    write_credentials,
)
from mimir.rate_limits import RateLimitStore


# ─── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def credentials_path(tmp_path: Path) -> Path:
    """Sample credentials.json laid out the way ``claude /login`` writes."""
    path = tmp_path / ".credentials.json"
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test-access",
            "refreshToken": "sk-ant-ort01-test-refresh-original",
            "expiresAt": int((time.time() + 3600) * 1000),  # 1h ahead
            "scopes": [
                "user:profile", "user:inference", "user:mcp_servers",
                "user:file_upload", "user:sessions:claude_code",
            ],
            "subscriptionType": "max",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def cfg(credentials_path: Path) -> PollerConfig:
    return PollerConfig(credentials_path=credentials_path, refresh_warn_days=25)


@pytest.fixture
def rate_store(tmp_path: Path) -> RateLimitStore:
    return RateLimitStore(path=tmp_path / "rate_limits.json")


def _usage_response() -> dict:
    """Match the live shape of /api/oauth/usage. Reset times are
    computed relative to ``now`` so the fixture doesn't rot — RateLimitStore
    filters out windows whose ``resets_at`` is in the past, which would
    silently make assertions on ``store.current()`` fail once wall-clock
    crosses a hardcoded timestamp."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    five_hour_reset = (now + timedelta(hours=2)).isoformat()
    seven_day_reset = (now + timedelta(days=3)).isoformat()
    return {
        "five_hour": {
            "utilization": 5.0,
            "resets_at": five_hour_reset,
        },
        "seven_day": {
            "utilization": 43.0,
            "resets_at": seven_day_reset,
        },
        "seven_day_opus": None,
        "seven_day_sonnet": {
            "utilization": 24.0,
            "resets_at": seven_day_reset,
        },
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": None,
            "used_credits": None,
            "utilization": None,
        },
    }


# ─── credentials I/O ──────────────────────────────────────────────────


def test_read_credentials_returns_oauth_block(credentials_path: Path) -> None:
    oauth = read_credentials(credentials_path)
    assert oauth["accessToken"] == "sk-ant-oat01-test-access"
    assert "user:profile" in oauth["scopes"]


def test_read_credentials_rejects_missing_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    with pytest.raises(ValueError, match="claudeAiOauth"):
        read_credentials(path)


def test_write_credentials_roundtrip(credentials_path: Path) -> None:
    oauth = read_credentials(credentials_path)
    oauth["accessToken"] = "sk-ant-oat01-rotated"
    write_credentials(credentials_path, oauth)
    again = read_credentials(credentials_path)
    assert again["accessToken"] == "sk-ant-oat01-rotated"
    # File mode set restrictively (when supported by FS).
    mode = credentials_path.stat().st_mode & 0o777
    assert mode in (0o600, 0o644)  # tmp_path may not honor chmod


def test_is_access_token_expired_within_skew(tmp_path: Path) -> None:
    # 30 seconds in the future, with 60s skew → expired.
    near = {"expiresAt": int((time.time() + 30) * 1000)}
    assert is_access_token_expired(near, skew_seconds=60)
    # 5 minutes ahead, 60s skew → not expired.
    far = {"expiresAt": int((time.time() + 300) * 1000)}
    assert not is_access_token_expired(far, skew_seconds=60)
    # Missing expiresAt → treat as expired.
    assert is_access_token_expired({}, skew_seconds=60)


# ─── refresh-token age tracking ───────────────────────────────────────


def test_record_first_seen_creates_sidecar(tmp_path: Path) -> None:
    cred_path = tmp_path / "creds.json"
    cred_path.write_text("{}", encoding="utf-8")
    record_first_seen(cred_path, "rt-this-token-tail", now=1700000000.0)
    sidecar = tmp_path / op.FIRST_SEEN_SIDECAR_NAME
    data = json.loads(sidecar.read_text())
    assert data["first_login_at_unix"] == 1700000000
    # Tail is the last 12 characters of the refresh token.
    assert data["last_seen_refresh_tail"] == "s-token-tail"


def test_record_first_seen_preserves_first_login_on_rotation(
    tmp_path: Path,
) -> None:
    cred_path = tmp_path / "creds.json"
    cred_path.write_text("{}", encoding="utf-8")
    record_first_seen(cred_path, "rt-original-token", now=1700000000.0)
    # 5 days later, refresh token rotated — first_login_at must NOT update.
    record_first_seen(cred_path, "rt-different-token", now=1700432000.0)
    sidecar = tmp_path / op.FIRST_SEEN_SIDECAR_NAME
    data = json.loads(sidecar.read_text())
    assert data["first_login_at_unix"] == 1700000000


def test_days_since_first_login(tmp_path: Path) -> None:
    cred_path = tmp_path / "creds.json"
    cred_path.write_text("{}", encoding="utf-8")
    # No sidecar yet.
    assert days_since_first_login(cred_path) is None
    # 7 days ago.
    record_first_seen(cred_path, "tok", now=1700000000.0)
    age = days_since_first_login(cred_path, now=1700000000.0 + 7 * 86400)
    assert age == pytest.approx(7.0)


def test_reset_first_seen(tmp_path: Path) -> None:
    cred_path = tmp_path / "creds.json"
    cred_path.write_text("{}", encoding="utf-8")
    record_first_seen(cred_path, "tok-old", now=1700000000.0)
    reset_first_seen(cred_path, now=1701000000.0)
    age = days_since_first_login(cred_path, now=1701000000.0)
    assert age == 0.0


def test_record_first_seen_does_not_reset_first_login_on_corrupt_sidecar(
    tmp_path: Path,
) -> None:
    """CR#8: a corrupt sidecar must NOT silently reset first_login_at.

    Previously the JSONDecodeError branch reset existing={} and wrote a
    fresh sidecar with first_login_at_unix=now, restarting the 30-day
    age-warn countdown. The fix: refuse to write when the sidecar can't
    be parsed; let the operator notice via days_since_first_login=None.
    """
    cred_path = tmp_path / "creds.json"
    cred_path.write_text("{}", encoding="utf-8")
    sidecar = tmp_path / op.FIRST_SEEN_SIDECAR_NAME

    # Plant a corrupt sidecar (truncated JSON).
    corrupt_blob = '{"first_login_at_unix": 17000000'
    sidecar.write_text(corrupt_blob, encoding="utf-8")
    sidecar_mtime_before = sidecar.stat().st_mtime

    # record_first_seen against the corrupt sidecar must return without
    # rewriting it.
    result = record_first_seen(cred_path, "rt-tail", now=1701000000.0)
    assert result.get("corrupt") is True

    # Sidecar untouched — same bytes, same mtime (within FS resolution).
    assert sidecar.read_text(encoding="utf-8") == corrupt_blob
    assert sidecar.stat().st_mtime == sidecar_mtime_before

    # And days_since_first_login still returns None (its existing
    # JSONDecodeError handler), so the age-warn correctly does not fire.
    assert days_since_first_login(cred_path, now=1701000000.0) is None


def test_atomic_write_json_fsyncs_and_replaces(tmp_path: Path) -> None:
    """CR#7: write_credentials must use the atomic helper that fsyncs.

    Chainlink #239: the helper moved to ``mimir._atomic`` so all three
    sidecar paths (oauth_usage_poller, rate_limits, quota_pause) share
    the same fsync-file + fsync-parent-dir contract. The contract test
    lives in ``tests/test_atomic.py``; this one verifies oauth_usage_poller
    still uses it (import-level assertion).
    """
    from mimir import _atomic

    target = tmp_path / "out.json"
    op.atomic_write_json(target, {"a": 1, "b": [2, 3]})
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}
    # Mode 0o600 (POSIX).
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    # oauth_usage_poller's atomic_write_json is the same object as
    # _atomic.atomic_write_json — the unification (chainlink #239)
    # didn't introduce a divergent copy.
    assert op.atomic_write_json is _atomic.atomic_write_json


# ─── refresh-grant HTTP ───────────────────────────────────────────────


class _MockResponse:
    def __init__(self, status: int, body: str | dict):
        self.status = status
        if isinstance(body, dict):
            body = json.dumps(body)
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _MockSession:
    """Minimal aiohttp.ClientSession stand-in. Records the last call
    and returns canned responses."""

    def __init__(self, post_resp: _MockResponse | None = None,
                 get_resps: list[_MockResponse] | None = None):
        self.post_resp = post_resp
        self.get_resps = list(get_resps) if get_resps else []
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, *, json=None, headers=None, **_):  # noqa: A002
        self.calls.append(("POST", url, {"json": json, "headers": headers}))
        return self.post_resp

    def get(self, url, *, headers=None, **_):
        self.calls.append(("GET", url, {"headers": headers}))
        if not self.get_resps:
            raise AssertionError(f"unexpected GET {url}")
        return self.get_resps.pop(0)


@pytest.mark.asyncio
async def test_refresh_access_token_happy_path(
    cfg: PollerConfig, credentials_path: Path,
) -> None:
    oauth = read_credentials(credentials_path)
    session = _MockSession(post_resp=_MockResponse(200, {
        "access_token": "sk-ant-oat01-new-access",
        "refresh_token": "sk-ant-ort01-rotated",
        "expires_in": 3600,
        "token_type": "Bearer",
    }))
    new_oauth = await op.refresh_access_token(session, oauth, cfg)
    assert new_oauth["accessToken"] == "sk-ant-oat01-new-access"
    assert new_oauth["refreshToken"] == "sk-ant-ort01-rotated"
    assert new_oauth["expiresAt"] > int(time.time() * 1000)
    # Original-fields preserved.
    assert "user:profile" in new_oauth["scopes"]
    assert new_oauth["subscriptionType"] == "max"


@pytest.mark.asyncio
async def test_refresh_access_token_invalid_grant_logs_out(
    cfg: PollerConfig, credentials_path: Path,
) -> None:
    oauth = read_credentials(credentials_path)
    session = _MockSession(post_resp=_MockResponse(
        400, {"error": "invalid_grant", "error_description": "expired"},
    ))
    with pytest.raises(OAuthRefreshError) as exc:
        await op.refresh_access_token(session, oauth, cfg)
    assert exc.value.logged_out is True
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_refresh_access_token_5xx_is_transient(
    cfg: PollerConfig, credentials_path: Path,
) -> None:
    oauth = read_credentials(credentials_path)
    session = _MockSession(post_resp=_MockResponse(503, "service unavailable"))
    with pytest.raises(OAuthRefreshError) as exc:
        await op.refresh_access_token(session, oauth, cfg)
    assert exc.value.logged_out is False
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_refresh_access_token_falls_back_to_old_refresh_when_omitted(
    cfg: PollerConfig, credentials_path: Path,
) -> None:
    """Some providers omit refresh_token from the response when not
    rotated. Verify we keep the old one rather than nulling it."""
    oauth = read_credentials(credentials_path)
    original_refresh = oauth["refreshToken"]
    session = _MockSession(post_resp=_MockResponse(200, {
        "access_token": "sk-ant-oat01-new",
        "expires_in": 3600,
    }))
    new_oauth = await op.refresh_access_token(session, oauth, cfg)
    assert new_oauth["refreshToken"] == original_refresh


# ─── /api/oauth/usage HTTP ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_usage_happy_path(cfg: PollerConfig) -> None:
    session = _MockSession(get_resps=[_MockResponse(200, _usage_response())])
    payload = await op.fetch_usage(session, "sk-ant-oat01-x", cfg)
    assert payload["five_hour"]["utilization"] == 5.0
    # Confirm headers were sent.
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == cfg.usage_endpoint
    assert kwargs["headers"]["Authorization"] == "Bearer sk-ant-oat01-x"
    assert kwargs["headers"]["anthropic-beta"] == op.OAUTH_BETA_HEADER


@pytest.mark.asyncio
async def test_fetch_usage_401_marks_unauthorized(cfg: PollerConfig) -> None:
    session = _MockSession(
        get_resps=[_MockResponse(401, '{"error":"unauthorized"}')],
    )
    with pytest.raises(UsageFetchError) as exc:
        await op.fetch_usage(session, "stale", cfg)
    assert exc.value.unauthorized is True


@pytest.mark.asyncio
async def test_fetch_usage_403_not_unauthorized(cfg: PollerConfig) -> None:
    """403 (insufficient scope) is a different bucket from 401 — the
    caller shouldn't try to refresh; refreshing won't add scopes."""
    session = _MockSession(
        get_resps=[_MockResponse(403, '{"error":"missing scope"}')],
    )
    with pytest.raises(UsageFetchError) as exc:
        await op.fetch_usage(session, "tok", cfg)
    assert exc.value.unauthorized is False
    assert exc.value.status == 403


# ─── snapshot recording ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_writes_known_windows(rate_store: RateLimitStore) -> None:
    recorded = await record_usage(rate_store, _usage_response())
    assert "five_hour" in recorded
    assert recorded["five_hour"]["utilization"] == pytest.approx(0.05)
    assert "seven_day" in recorded
    assert "seven_day_sonnet" in recorded
    # Null bucket skipped, extra_usage skipped.
    assert "seven_day_opus" not in recorded
    assert "extra_usage" not in recorded
    # Persisted on disk.
    persisted = rate_store.current()
    assert persisted["five_hour"].utilization == pytest.approx(0.05)


# ─── CR#22 layer a: cross-window anomaly detection ──────────────────


def test_detect_5h_anomaly_flags_jump_with_no_7d_response() -> None:
    """The classic glitch shape: 5h jumped 7%→100% (+93pp) while 7d
    barely moved (49% → 49%, ~0pp). Rule should reject the spike."""
    from mimir.oauth_usage_poller import detect_5h_anomaly

    reason = detect_5h_anomaly(
        new_5h=1.00, prev_5h=0.07,
        new_7d=0.49, prev_7d=0.49,
    )
    assert reason is not None
    assert "five_hour jumped +93pp" in reason
    assert "seven_day only moved 0.0pp" in reason


def test_detect_5h_anomaly_passes_real_growth() -> None:
    """A real saturation event grows gradually AND the 7d climbs in
    proportion. 5h 7%→60% (+53pp) with 7d 49%→55% (+6pp) is plausible
    — should NOT be flagged."""
    from mimir.oauth_usage_poller import detect_5h_anomaly

    reason = detect_5h_anomaly(
        new_5h=0.60, prev_5h=0.07,
        new_7d=0.55, prev_7d=0.49,
    )
    assert reason is None  # 7d delta of 6pp clears the threshold


def test_detect_5h_anomaly_passes_small_jump() -> None:
    """5h moved only 30pp; cross-check doesn't even apply (below
    the trigger threshold)."""
    from mimir.oauth_usage_poller import detect_5h_anomaly

    reason = detect_5h_anomaly(
        new_5h=0.40, prev_5h=0.10,
        new_7d=0.49, prev_7d=0.49,
    )
    assert reason is None


def test_detect_5h_anomaly_no_data_skips_check() -> None:
    """First poll after restart: prev values are None. Trust the
    reading — no signal to distrust it on."""
    from mimir.oauth_usage_poller import detect_5h_anomaly

    assert detect_5h_anomaly(
        new_5h=1.00, prev_5h=None, new_7d=0.49, prev_7d=None,
    ) is None
    # Missing 7d means we can't cross-check; trust the 5h value.
    assert detect_5h_anomaly(
        new_5h=1.00, prev_5h=0.07, new_7d=None, prev_7d=None,
    ) is None


@pytest.mark.asyncio
async def test_record_usage_rejects_anomalous_5h_spike(
    rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: prior store has 5h=7% and 7d=49%; new payload says
    5h=100% and 7d=49%. The 5h reading should be rejected and the
    prior 7% kept; 7d updates normally. ``quota_reading_anomalous``
    event fires."""
    events: list[tuple[str, dict]] = []
    async def _cap(t, **kw):
        events.append((t, kw))
    monkeypatch.setattr(op, "log_event", _cap)
    # Seed prior state directly via the store's _load monkey-patch
    # path (parallels the test fixtures elsewhere in this file).
    rate_store._load = lambda: {  # type: ignore[method-assign]
        "five_hour": {
            "status": "allowed",
            "utilization": 0.07,
            "resets_at": int(time.time() + 3600),
            "observed_at": "2026-05-06T04:00:00+00:00",
        },
        "seven_day": {
            "status": "allowed",
            "utilization": 0.49,
            "resets_at": int(time.time() + 86400),
            "observed_at": "2026-05-06T04:00:00+00:00",
        },
    }
    recorded = await record_usage(rate_store, {
        "five_hour": {
            "utilization": 100.0,  # 100% in percent form
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day": {
            "utilization": 49.0,
            "resets_at": "2099-01-01T00:00:00Z",
        },
    })
    # 5h was rejected — recorded shows the anomaly metadata.
    assert recorded["five_hour"]["anomalous"] is True
    assert recorded["five_hour"]["rejected_utilization"] == pytest.approx(1.00)
    assert recorded["five_hour"]["kept_utilization"] == pytest.approx(0.07)
    # 7d wrote through unchanged.
    assert recorded["seven_day"]["utilization"] == pytest.approx(0.49)
    # Algedonic event fired with both values + cross-window context.
    anomaly_events = [e for e in events if e[0] == "quota_reading_anomalous"]
    assert len(anomaly_events) == 1
    payload = anomaly_events[0][1]
    assert payload["window_type"] == "five_hour"
    assert payload["rejected_utilization"] == pytest.approx(1.00)
    assert payload["kept_utilization"] == pytest.approx(0.07)


@pytest.mark.asyncio
async def test_record_usage_passes_real_5h_growth(
    rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse: a legitimate 5h climb with matching 7d delta writes
    through normally. Avoids over-aggressive rejection."""
    events: list[tuple[str, dict]] = []
    async def _cap(t, **kw):
        events.append((t, kw))
    monkeypatch.setattr(op, "log_event", _cap)
    rate_store._load = lambda: {  # type: ignore[method-assign]
        "five_hour": {
            "status": "allowed",
            "utilization": 0.07,
            "resets_at": int(time.time() + 3600),
            "observed_at": "",
        },
        "seven_day": {
            "status": "allowed",
            "utilization": 0.49,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
    }
    recorded = await record_usage(rate_store, {
        "five_hour": {
            "utilization": 60.0,  # +53pp
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day": {
            "utilization": 55.0,  # +6pp — proportional
            "resets_at": "2099-01-01T00:00:00Z",
        },
    })
    assert recorded["five_hour"]["utilization"] == pytest.approx(0.60)
    assert "anomalous" not in recorded["five_hour"]
    # No anomaly event.
    assert not [e for e in events if e[0] == "quota_reading_anomalous"]


# ─── 7d-vs-sub-buckets anomaly check (chainlink #220) ─────────────────


def test_detect_seven_day_anomaly_flags_cliff_jump() -> None:
    """Today's incident shape: overall 7d jumped 50%→100% in one
    sample while every sub-bucket stayed on its smooth ramp
    (sonnet 48%→50%, omelette 0%→0%). Reject the overall reading.

    With the absolute-coherence check (chainlink #250), this case is now
    caught by Check 2 first (1.00 > 0.50 + 0.10 = 0.60) rather than
    Check 1 (jump+delta). The reason message changes accordingly."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=1.00, prev_7d=0.50,
        new_sub_buckets={"seven_day_sonnet": 0.50, "seven_day_omelette": 0.00},
        prev_sub_buckets={"seven_day_sonnet": 0.48, "seven_day_omelette": 0.00},
    )
    assert reason is not None
    assert "seven_day" in reason
    assert "seven_day_sonnet" in reason  # names the incoherent sub-bucket


def test_detect_seven_day_anomaly_passes_real_growth() -> None:
    """A real saturation event has at least one sub-bucket moving
    proportionally. 7d 50%→85% (+35pp) with sonnet 48%→78% (+30pp)
    is plausible — should NOT be flagged."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=0.85, prev_7d=0.50,
        new_sub_buckets={"seven_day_sonnet": 0.78, "seven_day_omelette": 0.00},
        prev_sub_buckets={"seven_day_sonnet": 0.48, "seven_day_omelette": 0.00},
    )
    assert reason is None  # 30pp sub-bucket delta clears the threshold


def test_detect_seven_day_anomaly_passes_small_jump() -> None:
    """7d moved only 10pp; cross-check doesn't apply (below
    the trigger threshold). No sub-bucket movement required.

    Numbers chosen so the overall is also within ANOMALY_7D_COHERENCE_SLACK
    of the max sub-bucket (0.60 ≤ 0.55 + 0.10 = 0.65) — no coherence flag."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=0.60, prev_7d=0.50,
        new_sub_buckets={"seven_day_sonnet": 0.55, "seven_day_omelette": 0.00},
        prev_sub_buckets={"seven_day_sonnet": 0.48, "seven_day_omelette": 0.00},
    )
    assert reason is None


def test_detect_seven_day_anomaly_no_data_skips_check() -> None:
    """First poll after restart (prev_7d=None) → trust the reading;
    no cross-reference."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    assert detect_seven_day_anomaly(
        new_7d=1.00, prev_7d=None,
        new_sub_buckets={"seven_day_sonnet": 0.50},
        prev_sub_buckets={"seven_day_sonnet": 0.48},
    ) is None
    # No sub-buckets present in BOTH maps → delta check can't cross-check.
    # Numbers chosen so the coherence check also passes (0.55 ≤ 0.50 + 0.10).
    assert detect_seven_day_anomaly(
        new_7d=0.55, prev_7d=0.50,
        new_sub_buckets={"seven_day_sonnet": 0.50},
        prev_sub_buckets={},  # cold start on sub-buckets
    ) is None


def test_detect_seven_day_anomaly_only_new_sub_bucket_ignored() -> None:
    """A sub-bucket that first appeared in this poll (e.g. Anthropic
    shipped a new model tier between polls) has no prior value to
    compare against — should be skipped, not treated as zero delta.
    With no OTHER sub-buckets to cross-check, the check returns None."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=1.00, prev_7d=0.50,
        new_sub_buckets={
            "seven_day_sonnet": 0.50,  # +2pp from prev
            "seven_day_haiku_3": 0.99,  # new bucket — would be a big delta if counted
        },
        prev_sub_buckets={"seven_day_sonnet": 0.48},
    )
    # Sonnet delta is 2pp (below 5pp threshold); haiku_3 is ignored
    # (not in prev) → anomaly fires. The flag is correct: only the
    # observable sub-bucket (sonnet) is steady, overall jumped 50pp.
    assert reason is not None


def test_detect_seven_day_anomaly_incoherent_absolute_2026_05_28() -> None:
    """chainlink #250 — 2026-05-28 incident trace.

    A prior glitch had pegged seven_day_sonnet=1.0 (matching the bogus
    overall=1.0). Between polls the prior glitch resolved: sonnet dropped
    back to its real value of ~0.02. The detector now saw:
      - prev_7d kept at 0.0 (trusted prior, glitch was being rejected)
      - new_7d=1.00  (still bogus, overall endpoint still pegged)
      - prev_sonnet=1.00 (glitched value from last accepted state)
      - new_sonnet=0.02  (now corrected to real value)
      - delta=98pp  ≥  5pp threshold → delta-based check passed it through

    The absolute-coherence check catches it: 1.00 > 0.02 + 0.10 = 0.12.
    Without this fix, 140+ minutes of heartbeat suppression followed.
    """
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=1.00,
        prev_7d=0.00,  # prior trusted value (glitch being rejected)
        new_sub_buckets={"seven_day_sonnet": 0.02, "seven_day_omelette": 0.00},
        prev_sub_buckets={"seven_day_sonnet": 1.00, "seven_day_omelette": 0.00},
    )
    assert reason is not None, (
        "should be flagged as incoherent: 7d=100% but max sub-bucket=2%"
    )
    assert "incoherent" in reason
    assert "seven_day_sonnet" in reason


def test_detect_seven_day_anomaly_coherent_high_usage_passes() -> None:
    """chainlink #250 — legitimate high-usage should NOT trigger the
    coherence check. 7d=95%, sonnet=90% → gap=5pp ≤ 10pp slack. Pass."""
    from mimir.oauth_usage_poller import detect_seven_day_anomaly

    reason = detect_seven_day_anomaly(
        new_7d=0.95,
        prev_7d=0.60,
        new_sub_buckets={"seven_day_sonnet": 0.90, "seven_day_omelette": 0.00},
        prev_sub_buckets={"seven_day_sonnet": 0.58, "seven_day_omelette": 0.00},
    )
    # 30pp delta → Check 1 fires; sonnet moved 32pp ≥ 5pp threshold → passes.
    # Coherence: 0.95 ≤ 0.90 + 0.10 = 1.00 → passes.
    assert reason is None


def test_detect_seven_day_anomaly_coherence_slack_boundary() -> None:
    """chainlink #250 — test right at the 10pp slack boundary.

    Just inside: 7d=0.60, sonnet=0.55 → gap=5pp ≤ 10pp → no flag.
    Just outside: 7d=0.66, sonnet=0.55 → gap=11pp > 10pp → flagged.
    """
    from mimir.oauth_usage_poller import (
        ANOMALY_7D_COHERENCE_SLACK,
        detect_seven_day_anomaly,
    )

    base_sub = {"seven_day_sonnet": 0.55, "seven_day_omelette": 0.00}

    # At or within slack: no flag.
    assert detect_seven_day_anomaly(
        new_7d=0.55 + ANOMALY_7D_COHERENCE_SLACK,  # exactly at slack
        prev_7d=0.50,
        new_sub_buckets=base_sub,
        prev_sub_buckets=base_sub,
    ) is None

    # One basis-point over slack: flag.
    reason = detect_seven_day_anomaly(
        new_7d=0.55 + ANOMALY_7D_COHERENCE_SLACK + 0.01,
        prev_7d=0.50,
        new_sub_buckets=base_sub,
        prev_sub_buckets=base_sub,
    )
    assert reason is not None
    assert "incoherent" in reason


@pytest.mark.asyncio
async def test_record_usage_rejects_anomalous_7d_cliff(
    rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (chainlink #220): prior store has 7d=50% with
    sonnet=48%/omelette=0%; new payload cliff-jumps 7d to 100% while
    sub-buckets stay near their prior values. The 7d-overall reading
    should be rejected and the prior 50% kept; sub-buckets update
    normally; ``quota_reading_anomalous`` event fires with
    ``window_type='seven_day'``."""
    events: list[tuple[str, dict]] = []
    async def _cap(t, **kw):
        events.append((t, kw))
    monkeypatch.setattr(op, "log_event", _cap)
    rate_store._load = lambda: {  # type: ignore[method-assign]
        "five_hour": {
            "status": "allowed",
            "utilization": 0.09,
            "resets_at": int(time.time() + 3600),
            "observed_at": "",
        },
        "seven_day": {
            "status": "allowed",
            "utilization": 0.50,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
        "seven_day_sonnet": {
            "status": "allowed",
            "utilization": 0.48,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
        "seven_day_omelette": {
            "status": "allowed",
            "utilization": 0.00,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
    }
    recorded = await record_usage(rate_store, {
        "five_hour": {
            "utilization": 9.0,
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day": {
            "utilization": 100.0,  # cliff jump from 50% — bogus
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day_sonnet": {
            "utilization": 50.0,  # +2pp — real
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day_omelette": {
            "utilization": 0.0,
            "resets_at": "2099-01-01T00:00:00Z",
        },
    })
    # 7d-overall was rejected — anomaly metadata in recorded.
    assert recorded["seven_day"]["anomalous"] is True
    assert recorded["seven_day"]["rejected_utilization"] == pytest.approx(1.00)
    assert recorded["seven_day"]["kept_utilization"] == pytest.approx(0.50)
    # Sub-buckets wrote through normally.
    assert recorded["seven_day_sonnet"]["utilization"] == pytest.approx(0.50)
    assert recorded["seven_day_omelette"]["utilization"] == pytest.approx(0.00)
    # Algedonic event fired with seven_day window_type + sub-bucket context.
    anomaly_events = [e for e in events if e[0] == "quota_reading_anomalous"]
    assert len(anomaly_events) == 1
    payload = anomaly_events[0][1]
    assert payload["window_type"] == "seven_day"
    assert payload["rejected_utilization"] == pytest.approx(1.00)
    assert payload["kept_utilization"] == pytest.approx(0.50)
    assert "sub_buckets_new" in payload
    assert payload["sub_buckets_new"]["seven_day_sonnet"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_record_usage_passes_real_7d_growth(
    rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse: 7d climbs 50%→85% AND sub-buckets climb in proportion
    (sonnet 48%→78%). Real saturation — write through, no anomaly
    event."""
    events: list[tuple[str, dict]] = []
    async def _cap(t, **kw):
        events.append((t, kw))
    monkeypatch.setattr(op, "log_event", _cap)
    rate_store._load = lambda: {  # type: ignore[method-assign]
        "seven_day": {
            "status": "allowed",
            "utilization": 0.50,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
        "seven_day_sonnet": {
            "status": "allowed",
            "utilization": 0.48,
            "resets_at": int(time.time() + 86400),
            "observed_at": "",
        },
    }
    recorded = await record_usage(rate_store, {
        "seven_day": {
            "utilization": 85.0,  # +35pp
            "resets_at": "2099-01-01T00:00:00Z",
        },
        "seven_day_sonnet": {
            "utilization": 78.0,  # +30pp — proportional
            "resets_at": "2099-01-01T00:00:00Z",
        },
    })
    assert recorded["seven_day"]["utilization"] == pytest.approx(0.85)
    assert "anomalous" not in recorded["seven_day"]
    assert not [e for e in events if e[0] == "quota_reading_anomalous"]


# ─── poll_once orchestration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_happy_path(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[tuple[str, dict]] = []

    async def _capture_event(event_type: str, **kwargs):
        events.append((event_type, kwargs))

    monkeypatch.setattr(op, "log_event", _capture_event)

    # Mock fetch_usage and ensure no refresh is needed (token is fresh).
    async def _fake_fetch(session, token, cfg):
        return _usage_response()

    async def _fake_refresh(*a, **kw):
        raise AssertionError("refresh should not be called when token is fresh")

    monkeypatch.setattr(op, "fetch_usage", _fake_fetch)
    monkeypatch.setattr(op, "refresh_access_token", _fake_refresh)

    # Provide a fake session so we don't open a real one.
    result = await poll_once(cfg, rate_store, session=_MockSession())
    assert result["ok"] is True
    types = [t for t, _ in events]
    assert "oauth_usage_ok" in types


@pytest.mark.asyncio
async def test_poll_once_expired_token_triggers_refresh(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch, credentials_path: Path,
) -> None:
    """If accessToken is past expiry, poll_once should refresh
    proactively, persist the new creds, then fetch usage."""
    # Make the token already expired.
    oauth = read_credentials(credentials_path)
    oauth["expiresAt"] = int((time.time() - 60) * 1000)
    write_credentials(credentials_path, oauth)

    events: list[tuple[str, dict]] = []

    async def _cap(t, **kw):
        events.append((t, kw))

    refresh_calls = []

    async def _fake_refresh(session, oauth_in, cfg_in):
        refresh_calls.append(oauth_in["accessToken"])
        new = dict(oauth_in)
        new["accessToken"] = "sk-ant-oat01-fresh"
        new["refreshToken"] = "sk-ant-ort01-rotated"
        new["expiresAt"] = int((time.time() + 3600) * 1000)
        return new

    async def _fake_fetch(session, token, cfg_in):
        # Confirm we called fetch with the fresh token.
        assert token == "sk-ant-oat01-fresh"
        return _usage_response()

    monkeypatch.setattr(op, "log_event", _cap)
    monkeypatch.setattr(op, "refresh_access_token", _fake_refresh)
    monkeypatch.setattr(op, "fetch_usage", _fake_fetch)

    result = await poll_once(cfg, rate_store, session=_MockSession())
    assert result["ok"] is True
    assert len(refresh_calls) == 1
    types = [t for t, _ in events]
    assert "oauth_refresh_ok" in types
    assert "oauth_usage_ok" in types
    # Credentials were persisted with the new accessToken.
    persisted = read_credentials(credentials_path)
    assert persisted["accessToken"] == "sk-ant-oat01-fresh"


@pytest.mark.asyncio
async def test_poll_once_refresh_fails_logged_out(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch, credentials_path: Path,
) -> None:
    oauth = read_credentials(credentials_path)
    oauth["expiresAt"] = int((time.time() - 60) * 1000)
    write_credentials(credentials_path, oauth)

    events: list[tuple[str, dict]] = []

    async def _cap(t, **kw):
        events.append((t, kw))

    async def _fake_refresh(*a, **kw):
        raise OAuthRefreshError(
            "refresh denied 400: invalid_grant", logged_out=True, status=400,
        )

    monkeypatch.setattr(op, "log_event", _cap)
    monkeypatch.setattr(op, "refresh_access_token", _fake_refresh)

    result = await poll_once(cfg, rate_store, session=_MockSession())
    assert result["ok"] is False
    types = [t for t, _ in events]
    assert "oauth_logged_out" in types


@pytest.mark.asyncio
async def test_poll_once_default_now_handles_logged_out_throttle(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch, credentials_path: Path, tmp_path: Path,
) -> None:
    """chainlink #230: poll_once(now=None) used to crash with TypeError on the
    logged-out-reminder throttle path when the sidecar carried a
    ``logged_out_last_reminder_unix`` float. The fix: resolve ``now`` to
    ``time.time()`` at the top of poll_once so subsequent ``now - sidecar_float``
    arithmetic always operates on two floats.
    """
    # Pre-seed sidecar with a sticky logged_out state + a recent reminder.
    # If the throttle path subtracts None from these floats, we get TypeError.
    # The credentials_path fixture writes refreshToken="sk-ant-ort01-test-refresh-original";
    # last 12 chars = "sh-original". Sidecar's last_seen_refresh_tail must match,
    # otherwise the tail-changed branch in record_first_seen clears logged_out state.
    sidecar = credentials_path.parent / op.FIRST_SEEN_SIDECAR_NAME
    refresh_token = "sk-ant-ort01-test-refresh-original"
    sidecar.write_text(json.dumps({
        "first_login_at_unix": int(time.time() - 86400),
        "last_seen_refresh_tail": refresh_token[-12:],
        "logged_out_since_unix": int(time.time() - 7200),
        "logged_out_last_reminder_unix": int(time.time() - 60),  # recent → throttle should fire
    }), encoding="utf-8")

    events: list[tuple[str, dict]] = []
    async def _cap(t, **kw):
        events.append((t, kw))
    monkeypatch.setattr(op, "log_event", _cap)

    # Invoke with the production default (no now= kwarg). Pre-fix, this raised
    # TypeError when computing ``now - last_reminder``.
    result = await poll_once(cfg, rate_store, session=_MockSession())

    # The throttle correctly short-circuited (recent reminder → no event spam).
    assert result == {"ok": False, "stage": "logged_out_throttled"}
    # No reminder event fired (the existing one is < interval).
    types = [t for t, _ in events]
    assert "oauth_logged_out_reminder" not in types


@pytest.mark.asyncio
async def test_poll_once_age_warn_emits(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch, credentials_path: Path,
) -> None:
    """If the sidecar shows first-login was older than refresh_warn_days,
    poll_once emits oauth_refresh_token_age_warn."""
    # Pre-seed sidecar to 30 days ago.
    long_ago = time.time() - 30 * 86400
    record_first_seen(credentials_path, "any-tail", now=long_ago)

    events: list[tuple[str, dict]] = []

    async def _cap(t, **kw):
        events.append((t, kw))

    async def _fake_fetch(session, token, cfg_in):
        return _usage_response()

    monkeypatch.setattr(op, "log_event", _cap)
    monkeypatch.setattr(op, "fetch_usage", _fake_fetch)

    await poll_once(cfg, rate_store, session=_MockSession())
    types = [t for t, _ in events]
    assert "oauth_refresh_token_age_warn" in types


@pytest.mark.asyncio
async def test_poll_once_missing_credentials_file(
    tmp_path: Path, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PollerConfig(credentials_path=tmp_path / "missing.json")

    events: list[tuple[str, dict]] = []

    async def _cap(t, **kw):
        events.append((t, kw))

    monkeypatch.setattr(op, "log_event", _cap)
    result = await poll_once(cfg, rate_store, session=_MockSession())
    assert result["ok"] is False
    assert result["stage"] == "read_credentials"
    types = [t for t, _ in events]
    assert "oauth_usage_failed" in types


@pytest.mark.asyncio
async def test_poll_once_401_then_refresh_then_retry(
    cfg: PollerConfig, rate_store: RateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the access token looks fresh but the server rejects with 401
    (clock skew, server-side rotation), poll_once should refresh and
    retry the fetch."""
    events: list[tuple[str, dict]] = []

    async def _cap(t, **kw):
        events.append((t, kw))

    fetch_calls: list[str] = []

    async def _fake_fetch(session, token, cfg_in):
        fetch_calls.append(token)
        if len(fetch_calls) == 1:
            raise UsageFetchError("usage 401", unauthorized=True, status=401)
        return _usage_response()

    async def _fake_refresh(session, oauth_in, cfg_in):
        new = dict(oauth_in)
        new["accessToken"] = "sk-ant-oat01-after-401"
        return new

    monkeypatch.setattr(op, "log_event", _cap)
    monkeypatch.setattr(op, "fetch_usage", _fake_fetch)
    monkeypatch.setattr(op, "refresh_access_token", _fake_refresh)

    result = await poll_once(cfg, rate_store, session=_MockSession())
    assert result["ok"] is True
    # First call with stale token, second call with rotated token.
    assert fetch_calls[1] == "sk-ant-oat01-after-401"
    types = [t for t, _ in events]
    # Both refresh and usage_ok logged.
    assert "oauth_refresh_ok" in types
    assert "oauth_usage_ok" in types


# ── chainlink #17 (CR#22 layer b): derive_5h_from_cost ───────────────


import json as _json
from datetime import datetime, timezone
from datetime import timedelta as _timedelta


def _seed_turns(path: Path, *, recent_costs: list[float], older_costs: list[float]) -> None:
    """Drop a turns.jsonl with cost rows. ``recent_costs`` go inside
    the 5h window; ``older_costs`` outside the 5h but inside the 7d
    window (so they count toward 7d total, not 5h)."""
    now = datetime.now(timezone.utc)
    rows = []
    for i, c in enumerate(recent_costs):
        rows.append({
            "ts": (now - _timedelta(minutes=i + 1)).isoformat(),
            "turn_id": f"r{i}", "session_id": "s",
            "saga_session_id": None, "trigger": "user_message",
            "channel_id": "c", "input": "x",
            "total_cost_usd": c,
        })
    for i, c in enumerate(older_costs):
        rows.append({
            "ts": (now - _timedelta(hours=6 + i)).isoformat(),
            "turn_id": f"o{i}", "session_id": "s",
            "saga_session_id": None, "trigger": "user_message",
            "channel_id": "c", "input": "x",
            "total_cost_usd": c,
        })
    path.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")


def test_derive_5h_basic_math(tmp_path: Path):
    """Math-shape test, factor pinned explicitly so test math is
    decoupled from the production default. 5h_cost=$20, 7d_cost=$500,
    prior_7d=0.50, factor=10 → back_derived_quota=$1000 →
    estimated = 20×10/1000 = 0.20 (no rounding)."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[20.0], older_costs=[480.0])
    out = derive_5h_from_cost(
        path, prior_7d_utilization=0.50, backderive_factor=10.0,
    )
    assert out == pytest.approx(0.20, abs=1e-6)


def test_derive_5h_rounds_to_5pp(tmp_path: Path):
    """Round-to-5pp shape, factor=10. 5h_cost=$20, 7d_cost=$500.
    prior_7d=0.40 → 0.16 → 0.15;  prior_7d=0.80 → 0.32 → 0.30."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[20.0], older_costs=[480.0])
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.40, backderive_factor=10.0,
    ) == pytest.approx(0.15)
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.80, backderive_factor=10.0,
    ) == pytest.approx(0.30)


def test_derive_5h_uses_env_default_factor(tmp_path: Path, monkeypatch):
    """When ``backderive_factor=None``, the function reads the env or
    falls back to ``QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT`` (=10.0)."""
    from mimir.oauth_usage_poller import (
        derive_5h_from_cost, QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT,
    )
    assert QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT == 10.0
    monkeypatch.delenv("MIMIR_QUOTA_5H_BACKDERIVE_FACTOR", raising=False)
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[20.0], older_costs=[480.0])
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.50,
    ) == pytest.approx(0.20)


def test_derive_5h_env_override_factor(tmp_path: Path, monkeypatch):
    """``MIMIR_QUOTA_5H_BACKDERIVE_FACTOR`` env var overrides the
    default factor — operators on different plan tiers can re-calibrate
    without a code change."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    monkeypatch.setenv("MIMIR_QUOTA_5H_BACKDERIVE_FACTOR", "5.0")
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[20.0], older_costs=[480.0])
    # factor=5 instead of 10 → estimated halves: 0.20 → 0.10
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.50,
    ) == pytest.approx(0.10)


def test_derive_5h_invalid_env_falls_back_to_default(
    tmp_path: Path, monkeypatch,
):
    """Garbage env values warn + fall back to the default. Protects
    against typos silently killing the estimator (e.g. operator
    sets ``MIMIR_QUOTA_5H_BACKDERIVE_FACTOR=ten``)."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    monkeypatch.setenv("MIMIR_QUOTA_5H_BACKDERIVE_FACTOR", "not-a-number")
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[20.0], older_costs=[480.0])
    # Falls back to default 10.0 → 0.20
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.50,
    ) == pytest.approx(0.20)


def test_derive_5h_clamps_to_one(tmp_path: Path):
    """Pathological case: 5h spend dominates the 7d window AND
    prior_7d_util is near saturation → math overshoots 100%, must
    clamp. With factor=10, 5h=$200, 7d=$210, prior_7d=0.99 →
    back_derived=$212 → estimated=200×10/212=9.4 → clamp 1.0.
    (Bounded by the math: estimated > 1 requires 5h_cost × FACTOR
    × prior_7d_util > 7d_cost.)"""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[200.0], older_costs=[10.0])
    assert derive_5h_from_cost(
        path, prior_7d_utilization=0.99, backderive_factor=10.0,
    ) == pytest.approx(1.0)


def test_derive_5h_returns_none_on_zero_prior_7d(tmp_path: Path):
    """prior_7d_util == 0 means no observable usage to back-derive
    the 7d quota dollar amount from. Return None — caller falls
    back to prior trusted value."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[50.0], older_costs=[150.0])
    assert derive_5h_from_cost(path, prior_7d_utilization=0.0) is None
    assert derive_5h_from_cost(path, prior_7d_utilization=-0.1) is None


def test_derive_5h_returns_none_on_out_of_range_prior(tmp_path: Path):
    """Bogus utilization >1 means the input is broken. Return None
    rather than producing a wildly-scaled estimate."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    _seed_turns(path, recent_costs=[50.0], older_costs=[150.0])
    assert derive_5h_from_cost(path, prior_7d_utilization=1.5) is None


def test_derive_5h_returns_none_on_zero_7d_cost(tmp_path: Path):
    """Empty turns.jsonl (or all turns >7d old) → no observable 7d cost
    → can't back-derive quota → None."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    path = tmp_path / "turns.jsonl"
    path.write_text("")  # empty file
    assert derive_5h_from_cost(path, prior_7d_utilization=0.50) is None


def test_derive_5h_returns_none_on_missing_file(tmp_path: Path):
    """Missing turns.jsonl shouldn't raise — caller treats None as
    'no signal' and keeps the prior trusted value."""
    from mimir.oauth_usage_poller import derive_5h_from_cost
    out = derive_5h_from_cost(
        tmp_path / "nope.jsonl", prior_7d_utilization=0.50,
    )
    assert out is None


# ── record_usage with derive enabled ─────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_writes_derived_5h_on_anomaly(tmp_path: Path):
    """End-to-end: anomaly fires + cfg has turns_log + prior_7d
    available → derived 5h snapshot lands in store with derived=True."""
    from mimir.oauth_usage_poller import (
        PollerConfig, record_usage,
    )
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op

    # Seed turns.jsonl: 5h=$20, 7d=$500. With the production
    # back-derive factor (10×) and the new_7d=0.51 used after Mimir's
    # PR #89 nit #2 fix, derived = 20 × 10 × 0.51 / 500 = 0.204 → 0.20.
    turns_path = tmp_path / "turns.jsonl"
    _seed_turns(turns_path, recent_costs=[20.0], older_costs=[480.0])

    store_path = tmp_path / "rate_limits.json"
    store = RateLimitStore(path=store_path)
    # Seed the store with prior 5h (10%) + prior 7d (50%) — the
    # cross-check needs a prior 5h to compute the jump.
    await store.record("five_hour", RateLimitSnapshot(
        status="allowed", utilization=0.10, observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50, observed_at="2026-05-09T00:00:00+00:00",
    ))

    # New payload: 5h jumps to 70% (60pp jump > 50pp threshold) but 7d
    # only moves to 51% (1pp delta < 5pp threshold) → anomaly.
    payload = {
        "five_hour": {"status": "allowed", "utilization": 0.70,
                      "resets_at": 9999999999},
        "seven_day": {"status": "allowed", "utilization": 0.51,
                      "resets_at": 9999999999},
    }

    cfg = PollerConfig(
        credentials_path=tmp_path / "creds.json",
        turns_log_path=turns_path,
    )

    events: list[tuple[str, dict]] = []

    async def _cap(et, **kw):
        events.append((et, kw))

    monkeypatch_target = op
    orig = monkeypatch_target.log_event
    monkeypatch_target.log_event = _cap
    try:
        recorded = await record_usage(store, payload, cfg=cfg)
    finally:
        monkeypatch_target.log_event = orig

    # Anomaly fired AND derive succeeded.
    assert recorded["five_hour"]["derived"] is True
    assert recorded["five_hour"]["utilization"] == pytest.approx(0.20, abs=1e-6)
    # quota_5h_derived event landed.
    types = [t for t, _ in events]
    assert "quota_reading_anomalous" in types
    assert "quota_5h_derived" in types

    # Store now has the derived snapshot, NOT the prior trusted 0.10.
    snap = store.current().get("five_hour")
    assert snap is not None
    assert snap.utilization == pytest.approx(0.20, abs=1e-6)
    assert snap.derived is True


@pytest.mark.asyncio
async def test_record_usage_falls_back_when_no_turns_log(tmp_path: Path):
    """No cfg → no turns_log_path → derive can't run → prior
    trusted value persists (current layer-(a) behavior preserved)."""
    from mimir.oauth_usage_poller import record_usage
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op

    store_path = tmp_path / "rate_limits.json"
    store = RateLimitStore(path=store_path)
    await store.record("five_hour", RateLimitSnapshot(
        status="allowed", utilization=0.10, observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50, observed_at="2026-05-09T00:00:00+00:00",
    ))

    payload = {
        "five_hour": {"status": "allowed", "utilization": 0.70,
                      "resets_at": 9999999999},
        "seven_day": {"status": "allowed", "utilization": 0.51,
                      "resets_at": 9999999999},
    }

    async def _cap(et, **kw):
        pass

    orig = op.log_event
    op.log_event = _cap
    try:
        # No cfg arg → falls back to layer-(a) behavior.
        recorded = await record_usage(store, payload)
    finally:
        op.log_event = orig

    assert recorded["five_hour"]["anomalous"] is True
    assert recorded["five_hour"]["kept_utilization"] == pytest.approx(0.10)
    assert "derived" not in recorded["five_hour"]
    # Store retains the prior trusted value.
    snap = store.current().get("five_hour")
    assert snap.utilization == pytest.approx(0.10)
    assert snap.derived is False


@pytest.mark.asyncio
async def test_record_usage_falls_back_when_derive_fails(tmp_path: Path):
    """cfg has turns_log_path but turns.jsonl is empty → derive
    returns None → prior trusted value persists."""
    from mimir.oauth_usage_poller import PollerConfig, record_usage
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op

    turns_path = tmp_path / "turns.jsonl"
    turns_path.write_text("")  # empty → no observable cost

    store_path = tmp_path / "rate_limits.json"
    store = RateLimitStore(path=store_path)
    await store.record("five_hour", RateLimitSnapshot(
        status="allowed", utilization=0.10,
    ))
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50,
    ))

    payload = {
        "five_hour": {"status": "allowed", "utilization": 0.70,
                      "resets_at": 9999999999},
        "seven_day": {"status": "allowed", "utilization": 0.51,
                      "resets_at": 9999999999},
    }

    cfg = PollerConfig(
        credentials_path=tmp_path / "creds.json",
        turns_log_path=turns_path,
    )

    async def _cap(et, **kw):
        pass

    orig = op.log_event
    op.log_event = _cap
    try:
        recorded = await record_usage(store, payload, cfg=cfg)
    finally:
        op.log_event = orig

    assert recorded["five_hour"]["anomalous"] is True
    assert "derived" not in recorded["five_hour"]
    snap = store.current().get("five_hour")
    assert snap.utilization == pytest.approx(0.10)
    assert snap.derived is False


@pytest.mark.asyncio
async def test_derived_snapshot_has_resets_at_none(tmp_path: Path):
    """Self-review fix: derived snapshots unconditionally use
    resets_at=None. Two reasons documented inline in the producer:
    (1) we don't actually know when the window resets (no successful
    endpoint reading this poll), and (2) inheriting a value that
    later goes stale (long glitch crosses a window boundary) would
    cause RateLimitStore.current() to filter the derived snapshot
    out — silently evicting our derived signal. None survives the
    filter unconditionally and the arbiter handles missing window-
    timing as "no time signal" (on-pace projection is already
    skipped for derived in AnthropicQuotaProvider)."""
    from mimir.oauth_usage_poller import PollerConfig, record_usage
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op
    import time as _time

    turns_path = tmp_path / "turns.jsonl"
    _seed_turns(turns_path, recent_costs=[20.0], older_costs=[480.0])

    store = RateLimitStore(path=tmp_path / "rl.json")
    # Prior 5h has a future resets_at — even when "just inheriting"
    # would work for this poll, the producer must NOT, because doing
    # so creates a multi-poll bug class on long glitches.
    future_reset = int(_time.time()) + 3 * 3600
    await store.record("five_hour", RateLimitSnapshot(
        status="allowed",
        utilization=0.10,
        resets_at=future_reset,
        observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50,
    ))

    payload = {
        "five_hour": {"status": "allowed", "utilization": 0.70,
                      "resets_at": 9999999999},
        "seven_day": {"status": "allowed", "utilization": 0.51,
                      "resets_at": 9999999999},
    }
    cfg = PollerConfig(
        credentials_path=tmp_path / "creds.json",
        turns_log_path=turns_path,
    )

    async def _cap(et, **kw):
        pass
    orig = op.log_event
    op.log_event = _cap
    try:
        await record_usage(store, payload, cfg=cfg)
    finally:
        op.log_event = orig

    snap = store.current().get("five_hour")
    assert snap is not None
    assert snap.derived is True
    assert snap.resets_at is None, (
        "derived snapshots must always have resets_at=None — "
        f"inheritance would create a multi-poll bug class. Got "
        f"{snap.resets_at!r}"
    )


# ─── CR2 (external I/O): logged_out throttle ───────────────────────────


def test_is_known_logged_out_returns_false_for_clean_sidecar(tmp_path):
    """Default state — sidecar exists with normal first_login data,
    no logged_out_since. is_known_logged_out returns False."""
    from mimir.oauth_usage_poller import (
        is_known_logged_out, record_first_seen,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "x"}')
    record_first_seen(creds, "x", now=1000.0)
    is_lo, since, last = is_known_logged_out(creds)
    assert is_lo is False
    assert since is None
    assert last is None


def test_is_known_logged_out_returns_true_after_mark(tmp_path):
    from mimir.oauth_usage_poller import (
        is_known_logged_out, mark_logged_out, record_first_seen,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "x"}')
    record_first_seen(creds, "x", now=1000.0)
    mark_logged_out(creds, now=2000.0)
    is_lo, since, last = is_known_logged_out(creds)
    assert is_lo is True
    assert since == 2000.0
    assert last == 2000.0


def test_clear_logged_out_removes_state(tmp_path):
    from mimir.oauth_usage_poller import (
        clear_logged_out, is_known_logged_out, mark_logged_out,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "x"}')
    mark_logged_out(creds, now=1000.0)
    assert is_known_logged_out(creds)[0] is True
    clear_logged_out(creds)
    assert is_known_logged_out(creds)[0] is False


def test_mark_logged_out_preserves_first_login_at(tmp_path):
    """Sticky logged_out doesn't clobber the first_login_at_unix
    timestamp — refresh-token age warn must keep working through
    the logged-out state."""
    from mimir.oauth_usage_poller import (
        days_since_first_login, mark_logged_out, record_first_seen,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "x"}')
    record_first_seen(creds, "x", now=1000.0)
    mark_logged_out(creds, now=86400.0 + 1000.0)  # 1 day later
    age = days_since_first_login(creds, now=86400.0 * 31 + 1000.0)
    assert age is not None
    # 31 days have elapsed since first_login_at_unix=1000 — exact integer
    # boundary because we used exact day multiples in the test setup.
    assert age == 31.0


def test_record_first_seen_clears_logged_out_on_refresh_tail_change(tmp_path):
    """PR #111 re-review fix: when the refresh-token tail changes
    (operator re-ran ``/login``), ``record_first_seen`` clears the
    sticky ``logged_out_*`` fields so the next poll resumes the
    regular flow. Pre-fix the throttle was permanent for the
    sidecar's lifetime — re-/login didn't recover the agent's
    usage polling because ``clear_logged_out`` only ran in the
    refresh path which was gated by the throttle."""
    from mimir.oauth_usage_poller import (
        is_known_logged_out, mark_logged_out, record_first_seen,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "old"}')
    # Establish the prior tail.
    record_first_seen(creds, "old-token-A", now=1000.0)
    # Mark logged out (refresh failed earlier).
    mark_logged_out(creds, now=2000.0)
    is_lo, _, _ = is_known_logged_out(creds)
    assert is_lo is True

    # Operator re-runs ``/login`` — refresh token rotates.
    record_first_seen(creds, "new-token-B", now=3000.0)
    is_lo_after, since, last = is_known_logged_out(creds)
    assert is_lo_after is False
    assert since is None
    assert last is None


def test_record_first_seen_does_not_clear_on_same_tail(tmp_path):
    """Defensive: a normal poll where the refresh-token tail is
    unchanged must NOT clear the logged_out state. Recovery is
    explicitly triggered by tail change, not by the next poll
    landing."""
    from mimir.oauth_usage_poller import (
        is_known_logged_out, mark_logged_out, record_first_seen,
    )
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"refreshToken": "stable"}')
    record_first_seen(creds, "stable-token", now=1000.0)
    mark_logged_out(creds, now=2000.0)
    # Same tail on next poll → throttle stays.
    record_first_seen(creds, "stable-token", now=3000.0)
    is_lo, since, _ = is_known_logged_out(creds)
    assert is_lo is True
    assert since == 2000.0


# ─── chainlink #231: 7d anomaly confirm-counter tests ─────────────────


def test_load_anomaly_confirm_state_missing_file_returns_empty(tmp_path: Path):
    """Missing sidecar → empty dict (graceful bootstrap on first poll)."""
    from mimir.oauth_usage_poller import _load_anomaly_confirm_state
    result = _load_anomaly_confirm_state(tmp_path / "does_not_exist.json")
    assert result == {}


def test_load_save_anomaly_confirm_state_roundtrip(tmp_path: Path):
    """Write state, read it back — values are preserved."""
    from mimir.oauth_usage_poller import (
        _load_anomaly_confirm_state, _save_anomaly_confirm_state,
    )
    path = tmp_path / "anomaly_confirm_state.json"
    _save_anomaly_confirm_state(path, {"seven_day": 3})
    loaded = _load_anomaly_confirm_state(path)
    assert loaded == {"seven_day": 3}


def test_load_anomaly_confirm_state_ignores_non_int_values(tmp_path: Path):
    """Non-integer values in sidecar are silently dropped (forward-compat)."""
    from mimir.oauth_usage_poller import _load_anomaly_confirm_state
    path = tmp_path / "anomaly_confirm_state.json"
    path.write_text('{"seven_day": 2, "other_key": "not-an-int", "five_hour": 1}',
                    encoding="utf-8")
    loaded = _load_anomaly_confirm_state(path)
    assert loaded == {"seven_day": 2, "five_hour": 1}


@pytest.mark.asyncio
async def test_7d_anomaly_confirm_counter_increments_on_rejection(tmp_path: Path):
    """Each anomalous 7d reading below the threshold increments the counter
    and records confirm_count in the returned metadata."""
    from mimir.oauth_usage_poller import (
        PollerConfig, record_usage,
        _load_anomaly_confirm_state, _anomaly_confirm_state_path,
        ANOMALY_7D_CONFIRM_THRESHOLD_DEFAULT,
    )
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op_mod

    store = RateLimitStore(path=tmp_path / "rl.json")
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day_sonnet", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))

    # Payload: 7d jumps 50→100% while sub-bucket moves <5pp → anomaly.
    payload = {
        "seven_day": {"status": "allowed", "utilization": 1.00,
                      "resets_at": 9999999999},
        "seven_day_sonnet": {"status": "allowed", "utilization": 0.52,
                             "resets_at": 9999999999},
    }

    cfg = PollerConfig(credentials_path=tmp_path / "creds.json")
    state_path = _anomaly_confirm_state_path(cfg)

    orig = op_mod.log_event
    op_mod.log_event = AsyncMock()
    try:
        # First call: counter starts at 0 → increments to 1.
        recorded = await record_usage(store, payload, cfg=cfg)
    finally:
        op_mod.log_event = orig

    assert recorded["seven_day"]["anomalous"] is True
    assert recorded["seven_day"]["confirm_count"] == 1
    assert recorded["seven_day"]["confirm_threshold"] == ANOMALY_7D_CONFIRM_THRESHOLD_DEFAULT
    # Prior value (0.50) preserved in store.
    snap = store.current().get("seven_day")
    assert snap is not None
    assert snap.utilization == pytest.approx(0.50)
    # Sidecar persisted counter = 1.
    state = _load_anomaly_confirm_state(state_path)
    assert state.get("seven_day") == 1


@pytest.mark.asyncio
async def test_7d_anomaly_confirm_threshold_accepts_after_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """After THRESHOLD consecutive anomalous readings, the value is
    accepted, ``quota_reading_anomaly_confirmed`` fires, and the counter
    resets to 0."""
    from mimir.oauth_usage_poller import (
        PollerConfig, record_usage,
        _load_anomaly_confirm_state, _anomaly_confirm_state_path,
        _save_anomaly_confirm_state,
    )
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op_mod

    # Force threshold to 2 so the test doesn't need 5 full async calls.
    monkeypatch.setenv("MIMIR_QUOTA_7D_ANOMALY_CONFIRM_THRESHOLD", "2")

    store = RateLimitStore(path=tmp_path / "rl.json")
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day_sonnet", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))

    payload = {
        "seven_day": {"status": "allowed", "utilization": 1.00,
                      "resets_at": 9999999999},
        "seven_day_sonnet": {"status": "allowed", "utilization": 0.52,
                             "resets_at": 9999999999},
    }

    cfg = PollerConfig(credentials_path=tmp_path / "creds.json")
    state_path = _anomaly_confirm_state_path(cfg)

    # Seed the sidecar with count=2 (already at threshold).
    _save_anomaly_confirm_state(state_path, {"seven_day": 2})

    events: list[str] = []
    orig = op_mod.log_event

    async def _cap(et, **kw):
        events.append(et)

    op_mod.log_event = _cap
    try:
        recorded = await record_usage(store, payload, cfg=cfg)
    finally:
        op_mod.log_event = orig

    # Should NOT have "anomalous" key — fall-through to normal write.
    assert "anomalous" not in recorded.get("seven_day", {})
    # seven_day written through at the new value (1.00).
    snap = store.current().get("seven_day")
    assert snap is not None
    assert snap.utilization == pytest.approx(1.00)
    # Confirmed event fired.
    assert "quota_reading_anomaly_confirmed" in events
    # Counter reset to 0 in sidecar.
    state = _load_anomaly_confirm_state(state_path)
    assert state.get("seven_day", 0) == 0


@pytest.mark.asyncio
async def test_7d_anomaly_confirm_counter_resets_on_clean_reading(
    tmp_path: Path,
):
    """A non-anomalous 7d reading resets the confirmation counter so
    prior anomaly runs don't carry over."""
    from mimir.oauth_usage_poller import (
        PollerConfig, record_usage,
        _load_anomaly_confirm_state, _anomaly_confirm_state_path,
        _save_anomaly_confirm_state,
    )
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import mimir.oauth_usage_poller as op_mod

    store = RateLimitStore(path=tmp_path / "rl.json")
    await store.record("seven_day", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))
    await store.record("seven_day_sonnet", RateLimitSnapshot(
        status="allowed", utilization=0.50,
        observed_at="2026-05-09T00:00:00+00:00",
    ))

    # Non-anomalous payload: 7d stays flat (no big jump).
    payload = {
        "seven_day": {"status": "allowed", "utilization": 0.51,
                      "resets_at": 9999999999},
        "seven_day_sonnet": {"status": "allowed", "utilization": 0.51,
                             "resets_at": 9999999999},
    }

    cfg = PollerConfig(credentials_path=tmp_path / "creds.json")
    state_path = _anomaly_confirm_state_path(cfg)

    # Seed the sidecar with count=3 from prior anomaly run.
    _save_anomaly_confirm_state(state_path, {"seven_day": 3})

    orig = op_mod.log_event
    op_mod.log_event = AsyncMock()
    try:
        recorded = await record_usage(store, payload, cfg=cfg)
    finally:
        op_mod.log_event = orig

    # 7d should have written through normally (no anomaly).
    assert "anomalous" not in recorded.get("seven_day", {})
    snap = store.current().get("seven_day")
    assert snap is not None
    assert snap.utilization == pytest.approx(0.51)
    # Counter reset to 0 (key removed).
    state = _load_anomaly_confirm_state(state_path)
    assert "seven_day" not in state
