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
    """Match the live shape of /api/oauth/usage."""
    return {
        "five_hour": {
            "utilization": 5.0,
            "resets_at": "2026-05-05T13:00:00.000000+00:00",
        },
        "seven_day": {
            "utilization": 43.0,
            "resets_at": "2026-05-06T19:00:00.000000+00:00",
        },
        "seven_day_opus": None,
        "seven_day_sonnet": {
            "utilization": 24.0,
            "resets_at": "2026-05-06T19:00:00.000000+00:00",
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
