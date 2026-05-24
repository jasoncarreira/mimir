"""Tests for ``_validate_bind_security`` — refuse non-loopback bind
without an API key (pre-OSS hardening, review item #2)."""

from __future__ import annotations

import pytest

from mimir.server import _LOOPBACK_HOSTS, _validate_bind_security


# ─── loopback bind: no key required ──────────────────────────────────────


@pytest.mark.parametrize("host", sorted(_LOOPBACK_HOSTS))
def test_loopback_without_key_is_allowed(host: str) -> None:
    """``127.0.0.1`` / ``::1`` / ``localhost`` are loopback — safe with
    no auth because only the operator can reach them."""
    # Does not raise.
    _validate_bind_security(host, api_key="")


@pytest.mark.parametrize("host", sorted(_LOOPBACK_HOSTS))
def test_loopback_with_key_is_allowed(host: str) -> None:
    _validate_bind_security(host, api_key="any-key")


# ─── non-loopback bind: requires a key ───────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_non_loopback_without_key_refused(host: str) -> None:
    """Binding any non-loopback interface without ``MIMIR_API_KEY`` is
    refused at startup — open to any reachable peer otherwise."""
    with pytest.raises(SystemExit) as excinfo:
        _validate_bind_security(host, api_key="")
    msg = str(excinfo.value)
    assert "MIMIR_API_KEY" in msg
    assert host in msg


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_loopback_with_key_allowed(host: str) -> None:
    """Non-loopback bind is fine once the operator opts in by setting
    a key — they've explicitly accepted the auth-required path."""
    _validate_bind_security(host, api_key="secret-key")
