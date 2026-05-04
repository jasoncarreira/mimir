"""Tests for the Claude Max quota poller (mimir/quota_poller.py).

The poller spins up a throwaway ClaudeSDKClient, calls
``get_context_usage()``, parses the ``apiUsage`` field, and writes
per-window snapshots into ``RateLimitStore``. Tests stub the SDK to
return synthetic apiUsage payloads and verify the store gets the
right shape.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from mimir.event_logger import init_logger
from mimir.rate_limits import RateLimitStore


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-quota")


def _install_fake_sdk(monkeypatch, response: Any) -> None:
    """Install a fake claude_agent_sdk module that returns ``response``
    from ``ClaudeSDKClient.get_context_usage()``."""

    class _FakeOptions:
        def __init__(self, **kw):
            self.cwd = kw.get("cwd")
            self.model = kw.get("model")

    class _FakeClient:
        def __init__(self, options=None):
            self.options = options
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def get_context_usage(self):
            if isinstance(response, Exception):
                raise response
            return response

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.ClaudeSDKClient = _FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


@pytest.mark.asyncio
async def test_poll_writes_known_windows_to_store(tmp_path: Path, monkeypatch):
    """Apple's apiUsage with five_hour + seven_day_opus buckets gets
    parsed into RateLimitSnapshots in the store."""
    _install_fake_sdk(monkeypatch, {
        "apiUsage": {
            "five_hour": {
                "status": "allowed",
                "utilization": 0.42,
                "resets_at": 9999999999,
            },
            "seven_day_opus": {
                "status": "allowed_warning",
                "utilization": 0.78,
                "resets_at": 9999999998,
            },
        },
    })

    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)

    snaps = store.current()
    assert "five_hour" in snaps
    assert snaps["five_hour"].utilization == pytest.approx(0.42)
    assert snaps["five_hour"].resets_at == 9999999999
    assert "seven_day_opus" in snaps
    assert snaps["seven_day_opus"].utilization == pytest.approx(0.78)


@pytest.mark.asyncio
async def test_poll_normalizes_percentage_form(tmp_path: Path, monkeypatch):
    """If the daemon reports utilization as 0-100 instead of 0-1.0,
    the poller rescales rather than recording a 78x value."""
    _install_fake_sdk(monkeypatch, {
        "apiUsage": {
            "five_hour": {
                "status": "allowed",
                "utilization": 78,  # percent, not ratio
                "resets_at": 9999999999,
            },
        },
    })
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)
    snaps = store.current()
    assert snaps["five_hour"].utilization == pytest.approx(0.78)


@pytest.mark.asyncio
async def test_poll_handles_iso_resets_at(tmp_path: Path, monkeypatch):
    """resets_at as an ISO string gets converted to unix seconds."""
    _install_fake_sdk(monkeypatch, {
        "apiUsage": {
            "five_hour": {
                "utilization": 0.5,
                "resets_at": "2099-01-01T00:00:00Z",
            },
        },
    })
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)
    snaps = store.current()
    assert snaps["five_hour"].resets_at is not None
    assert snaps["five_hour"].resets_at > 1700000000


@pytest.mark.asyncio
async def test_poll_skips_unparseable_buckets(tmp_path: Path, monkeypatch):
    """A bucket with no utilization AND no resets_at can't make a
    useful snapshot — skip rather than write garbage."""
    _install_fake_sdk(monkeypatch, {
        "apiUsage": {
            "good": {"utilization": 0.3, "resets_at": 9999999999},
            "empty": {},
            "garbage": "not a dict",
        },
    })
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)
    snaps = store.current()
    assert "good" in snaps
    assert "empty" not in snaps
    assert "garbage" not in snaps


@pytest.mark.asyncio
async def test_poll_logs_quota_poll_ok_event(tmp_path: Path, monkeypatch):
    """Successful poll emits quota_poll_ok with the windows recorded —
    visible in events.jsonl for audit."""
    _install_fake_sdk(monkeypatch, {
        "apiUsage": {
            "five_hour": {"utilization": 0.4, "resets_at": 9999999999},
        },
    })
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)

    events = [
        json.loads(l)
        for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    ok = [e for e in events if e.get("type") == "quota_poll_ok"]
    assert len(ok) == 1
    assert "five_hour" in ok[0]["windows"]


@pytest.mark.asyncio
async def test_poll_logs_failed_event_on_exception(tmp_path: Path, monkeypatch):
    """When get_context_usage() raises, the poller catches it, logs
    quota_poll_failed, and doesn't propagate."""
    _install_fake_sdk(monkeypatch, RuntimeError("daemon refused"))
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)  # must not raise

    events = [
        json.loads(l)
        for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    failed = [e for e in events if e.get("type") == "quota_poll_failed"]
    assert len(failed) == 1
    assert "daemon refused" in failed[0]["error"]


# ─── running_on_claude_max detection ────────────────────────────────


def test_running_on_claude_max_true_with_oauth_only(monkeypatch):
    from mimir.quota_poller import running_on_claude_max
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat...")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert running_on_claude_max() is True


def test_running_on_claude_max_false_when_oauth_missing(monkeypatch):
    """Direct ANTHROPIC_API_KEY without OAuth → not Max."""
    from mimir.quota_poller import running_on_claude_max
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    assert running_on_claude_max() is False


def test_running_on_claude_max_false_with_base_url_override(monkeypatch):
    """OAuth set BUT ANTHROPIC_BASE_URL points at OpenRouter / Minimax →
    agent calls don't go to Anthropic, so Max windows aren't relevant."""
    from mimir.quota_poller import running_on_claude_max
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat...")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1")
    assert running_on_claude_max() is False


def test_running_on_claude_max_false_when_oauth_blank(monkeypatch):
    """Empty OAuth token (operator left it blank) doesn't count."""
    from mimir.quota_poller import running_on_claude_max
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "  ")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert running_on_claude_max() is False


@pytest.mark.asyncio
async def test_poll_handles_missing_apiusage(tmp_path: Path, monkeypatch):
    """Fresh OAuth session with no traffic yet: response has no
    apiUsage. Poller logs quota_poll_ok with empty windows; doesn't
    fail."""
    _install_fake_sdk(monkeypatch, {"apiUsage": None})
    from mimir.quota_poller import poll_max_plan_quota
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    await poll_max_plan_quota(tmp_path, store)
    assert store.current() == {}

    events = [
        json.loads(l)
        for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    ok = [e for e in events if e.get("type") == "quota_poll_ok"]
    assert len(ok) == 1
    assert ok[0]["windows"] == {}
