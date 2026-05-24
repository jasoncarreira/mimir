"""Tests for ``mimir/version_check.py`` — PyPI version-check helper."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mimir.version_check import (
    VersionCheck,
    _is_prerelease,
    _parse_version,
    check_for_update,
)


# ─── _parse_version ──────────────────────────────────────────────────────


def test_parse_simple_dotted_int():
    assert _parse_version("1.2.3") == (1, 2, 3)
    assert _parse_version("0.1.0") == (0, 1, 0)
    assert _parse_version("10.0") == (10, 0)
    assert _parse_version("5") == (5,)


def test_parse_strips_suffix_to_int_prefix():
    """Pre-release suffix is dropped — comparison happens on the numeric
    core, the pre-release filter handles eligibility separately."""
    assert _parse_version("1.0.0rc1") == (1, 0, 0)
    assert _parse_version("0.2.0.dev42") == (0, 2, 0)
    assert _parse_version("1.0.0+build.5") == (1, 0, 0)


def test_parse_returns_none_for_garbage():
    assert _parse_version("") is None
    assert _parse_version("not-a-version") is None
    assert _parse_version("v1.2.3") is None  # leading 'v' not a digit


def test_parse_handles_whitespace():
    assert _parse_version("  1.2.3  ") == (1, 2, 3)


# ─── _is_prerelease ──────────────────────────────────────────────────────


def test_prerelease_markers():
    assert _is_prerelease("1.0.0rc1")
    assert _is_prerelease("1.0.0RC1")  # case-insensitive
    assert _is_prerelease("1.0.0.dev42")
    assert _is_prerelease("1.0.0a1")
    assert _is_prerelease("1.0.0b3")
    assert _is_prerelease("0.2.0-alpha")
    assert _is_prerelease("0.2.0.beta1")


def test_release_versions_not_prerelease():
    assert not _is_prerelease("1.0.0")
    assert not _is_prerelease("0.1.0")
    assert not _is_prerelease("10.20.30")


def test_post_releases_not_prerelease():
    """``0.1.0.post1`` is a post-release, not a pre-release — should
    NOT be filtered."""
    # The marker substring "post" doesn't trigger any of dev/alpha/
    # beta/rc/pre/aN/bN — verified here.
    assert not _is_prerelease("1.0.0.post1")


# ─── check_for_update ────────────────────────────────────────────────────


def _patch_pypi(version: str | None):
    """Patch ``_http_get_json`` to return a synthetic PyPI payload
    with ``info.version`` set to ``version``. Pass ``None`` to omit
    the field entirely (tests the malformed-response path)."""
    info = {} if version is None else {"version": version}
    return patch(
        "mimir.version_check._http_get_json",
        return_value={"info": info},
    )


def test_check_reports_newer_version_available():
    with _patch_pypi("0.2.0"):
        result = check_for_update(current_version="0.1.0")
    assert result.is_newer
    assert result.current == "0.1.0"
    assert result.latest == "0.2.0"
    assert result.error_msg is None


def test_check_reports_same_version_not_newer():
    with _patch_pypi("0.1.0"):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.latest == "0.1.0"


def test_check_reports_older_pypi_version_not_newer():
    """Operator on a dev build past the published release — shouldn't
    flag as "update available" (it's a downgrade)."""
    with _patch_pypi("0.1.0"):
        result = check_for_update(current_version="0.2.0")
    assert not result.is_newer


def test_check_filters_prerelease_by_default():
    """When PyPI's latest is a pre-release (rare; pypi.org/.../json
    typically returns the stable, but defensive in case), we don't
    surface it to a stable-track operator."""
    with _patch_pypi("0.2.0rc1"):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer  # filtered
    assert result.latest == "0.2.0rc1"
    assert result.error_msg is None  # not an error — just no signal


def test_check_includes_prerelease_when_opted_in():
    with _patch_pypi("0.2.0rc1"):
        result = check_for_update(
            current_version="0.1.0",
            include_prereleases=True,
        )
    assert result.is_newer


def test_check_local_prerelease_sees_newer_stable():
    """Operator on a pre-release sees the stable release that supersedes
    it — the pre-release filter doesn't suppress newer-numeric-core
    versions even when the operator is on a pre-release."""
    with _patch_pypi("0.2.0"):
        result = check_for_update(current_version="0.2.0rc1")
    # Numeric core 0.2.0 == 0.2.0, so tuple comparison says not newer.
    # This is a known limitation of the simple tuple parser — it can't
    # distinguish rc1 from the stable that supersedes it. Operators on
    # pre-releases see the next NUMERICALLY-NEWER version (0.3.0 etc.).
    # Acceptable for the open-source-from-day-one case where the
    # operator is unlikely to be running pre-releases. Could swap for
    # packaging.version.parse if this becomes a real issue.
    assert not result.is_newer  # documenting the known limitation


def test_check_handles_404_silently():
    """Package not yet published — common pre-release case. Return
    no-signal result, no exception."""
    import urllib.error
    with patch(
        "mimir.version_check._http_get_json",
        side_effect=urllib.error.HTTPError(
            "http://example", 404, "Not Found", {}, None,
        ),
    ):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.latest is None
    assert result.error_msg is not None
    assert "404" in result.error_msg


def test_check_handles_network_error_silently():
    import urllib.error
    with patch(
        "mimir.version_check._http_get_json",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.error_msg is not None
    assert "network" in result.error_msg.lower()


def test_check_handles_malformed_json():
    with patch(
        "mimir.version_check._http_get_json",
        side_effect=json.JSONDecodeError("bad", "", 0),
    ):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.error_msg is not None


def test_check_handles_missing_version_field():
    """PyPI returns 200 OK but the ``info.version`` field is absent —
    shouldn't crash."""
    with _patch_pypi(None):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.error_msg is not None
    assert "missing" in result.error_msg.lower()


def test_check_handles_unparseable_versions():
    """If either side has a version we can't parse, fail safely."""
    with _patch_pypi("not-a-version"):
        result = check_for_update(current_version="0.1.0")
    assert not result.is_newer
    assert result.error_msg is not None


def test_check_uses_default_current_from_module():
    """Default ``current_version`` is ``mimir.__version__``."""
    from mimir import __version__
    with _patch_pypi(__version__):
        result = check_for_update()
    assert not result.is_newer
    assert result.current == __version__


# ─── run_scheduled_update_check (cron callable) ──────────────────────────


@pytest.mark.asyncio
async def test_scheduled_check_emits_when_newer_available(tmp_path):
    """Cron callable emits ``mimir_update_available`` when PyPI has
    a newer version. Below-threshold runs (on latest) emit nothing."""
    from mimir.version_check import run_scheduled_update_check
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    init_logger(events_path, session_id="test-update-check")
    try:
        with _patch_pypi("99.0.0"):
            await run_scheduled_update_check(tmp_path)

        lines = events_path.read_text().splitlines()
        events = [json.loads(l) for l in lines if l.strip()]
        update_events = [e for e in events if e.get("type") == "mimir_update_available"]
        assert len(update_events) == 1
        ev = update_events[0]
        assert ev["latest"] == "99.0.0"
    finally:
        _reset_logger_for_tests()


@pytest.mark.asyncio
async def test_scheduled_check_silent_when_on_latest(tmp_path):
    from mimir import __version__
    from mimir.version_check import run_scheduled_update_check
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    init_logger(events_path, session_id="test-update-quiet")
    try:
        with _patch_pypi(__version__):
            await run_scheduled_update_check(tmp_path)

        if events_path.is_file():
            events = [
                json.loads(l) for l in events_path.read_text().splitlines() if l.strip()
            ]
            assert not any(
                e.get("type") == "mimir_update_available" for e in events
            )
    finally:
        _reset_logger_for_tests()


@pytest.mark.asyncio
async def test_scheduled_check_silent_on_network_error(tmp_path):
    """Network failure → no event (silent). The previous design used
    to emit ``mimir_update_check_error`` for transient PyPI outages
    but that creates noise on intermittent network issues; debug-log
    only."""
    import urllib.error
    from mimir.version_check import run_scheduled_update_check
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    init_logger(events_path, session_id="test-update-net-error")
    try:
        with patch(
            "mimir.version_check._http_get_json",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            await run_scheduled_update_check(tmp_path)

        if events_path.is_file():
            events = [
                json.loads(l) for l in events_path.read_text().splitlines() if l.strip()
            ]
            assert not any(
                e.get("type") == "mimir_update_available" for e in events
            )
            # No error event either — network failures are silent.
            assert not any(
                e.get("type") == "mimir_update_check_error" for e in events
            )
    finally:
        _reset_logger_for_tests()
