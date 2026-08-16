"""PyPI version-check for the installed mimir package.

Queries PyPI's public JSON endpoint
(``https://pypi.org/pypi/<package>/json``) to find the latest released
version of a package, compares it against the locally-installed
version, and reports whether an update is available.

Used by:
- The ``update-check`` daily cron (``Scheduler.add_update_check_job``)
  — emits a ``mimir_update_available`` algedonic event when a newer
  version is on PyPI so the operator sees it in the per-turn feedback
  block and on the /ops dashboard.
- The ``mimir update`` CLI subcommand — operator-facing status check
  with optional ``--apply`` to run ``python -m pip install --upgrade``.

Design choices
==============

**Pre-release filtering.** PyPI returns the latest released version in
``info.version``, which by convention excludes pre-releases. We
additionally classify versions with PEP 440 and filter pre/dev releases
unless the local version is itself a pre-release. Operators who want
pre-release surfacing pass ``include_prereleases=True``.

**Failure mode is silent.** Network errors / 404 (package not yet
published) / malformed JSON all return a ``VersionCheck`` with
``is_newer=False`` and a populated ``error_msg``. The daily cron then
emits no event for that day — no algedonic spam on transient
failures.

**Local version source.** Defaults to ``mimir.__version__``. Tests pass
explicit versions to exercise the comparison logic without mocking
imports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


def _pypi_package_name() -> str:
    """Return the PyPI distribution name to check.

    Defaults to ``"mimir-agent"`` — the bare ``mimir`` name on PyPI
    is taken by Ralph Meijer's unrelated Twisted-protocol project
    (``Mimir daemons``), so the open-source release uses
    ``mimir-agent`` as its distribution name. Python import path
    stays ``mimir`` (the package directory); only the install
    incantation differs: ``pip install mimir-agent``.

    Operators on a fork or pre-release channel override via
    ``MIMIR_PYPI_PACKAGE_NAME`` env var without code changes.
    """
    return os.environ.get("MIMIR_PYPI_PACKAGE_NAME", "mimir-agent").strip() or "mimir-agent"

# 5-second timeout matches the existing OAuth poller etc. — PyPI is a
# CDN-backed endpoint that should respond in << 1s; longer timeouts
# just delay the cron when network is degraded.
_HTTP_TIMEOUT_S = 5.0

@dataclass(frozen=True)
class VersionCheck:
    """Result of a PyPI version-check call.

    ``current`` is the locally-installed version; ``latest`` is what
    PyPI reports (or ``None`` on lookup failure). ``is_newer`` is True
    iff ``latest`` is strictly greater than ``current`` AND passes the
    pre-release filter. ``error_msg`` is set when the check couldn't
    complete (network failure, 404, parse error) — callers should
    treat the check as "no signal" rather than "no update."
    """

    current: str
    latest: Optional[str]
    is_newer: bool
    error_msg: Optional[str] = None


def _parse_version(text: str) -> Optional[Version]:
    """Parse a PEP 440 version, returning ``None`` for invalid input."""
    try:
        return Version(text.strip())
    except InvalidVersion:
        return None


def _is_prerelease(version: str) -> bool:
    """Return whether a valid PEP 440 version is a pre/dev release."""
    parsed = _parse_version(version)
    return parsed is not None and parsed.is_prerelease


def _http_get_json(url: str, timeout_s: float = _HTTP_TIMEOUT_S) -> dict:
    """Minimal HTTP GET → JSON. Uses urllib (stdlib) so the daily
    update-check has no extra runtime dependency. Raises on any
    failure; caller wraps in try/except."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "mimir/version-check (https://github.com/jasoncarreira/mimir)"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def check_for_update(
    package: Optional[str] = None,
    current_version: Optional[str] = None,
    *,
    include_prereleases: bool = False,
) -> VersionCheck:
    """Query PyPI for ``package``'s latest version and compare to
    ``current_version`` (default: ``mimir.__version__``).

    Returns a :class:`VersionCheck` with ``is_newer`` True iff:
      1. PyPI lookup succeeded
       2. The reported latest PEP 440 version is strictly greater than
          the current version
      3. The latest version is not a pre-release (unless
         ``include_prereleases=True``)

    Any failure (network, 404, malformed JSON, unparseable version)
    returns ``is_newer=False`` with a populated ``error_msg``. The
    daily cron interprets that as "no signal today" and emits no
    event — operator sees nothing rather than noisy errors.
    """
    if current_version is None:
        from . import __version__
        current_version = __version__
    if package is None:
        package = _pypi_package_name()

    url = _PYPI_JSON_URL.format(package=package)
    try:
        payload = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        # 404 is the expected case before first publication. Anything
        # else is genuinely degraded.
        if exc.code == 404:
            return VersionCheck(
                current=current_version,
                latest=None,
                is_newer=False,
                error_msg=f"package not found on PyPI (HTTP 404)",
            )
        return VersionCheck(
            current=current_version,
            latest=None,
            is_newer=False,
            error_msg=f"HTTP {exc.code}: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return VersionCheck(
            current=current_version,
            latest=None,
            is_newer=False,
            error_msg=f"network: {exc.reason}",
        )
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return VersionCheck(
            current=current_version,
            latest=None,
            is_newer=False,
            error_msg=f"{type(exc).__name__}: {exc}",
        )

    info = payload.get("info") or {}
    latest = info.get("version")
    if not isinstance(latest, str) or not latest:
        return VersionCheck(
            current=current_version,
            latest=None,
            is_newer=False,
            error_msg="PyPI response missing info.version",
        )

    current_parsed = _parse_version(current_version)
    latest_parsed = _parse_version(latest)
    if current_parsed is None or latest_parsed is None:
        return VersionCheck(
            current=current_version,
            latest=latest,
            is_newer=False,
            error_msg=f"unparseable version (current={current_version!r}, latest={latest!r})",
        )

    # Pre-release filter — operators get stable releases by default.
    # Exception: if the LOCAL version is itself a pre-release, then
    # newer pre-releases are eligible (operator is already on a
    # pre-release channel, so suppressing pre-releases here would
    # mean they never get notified of newer pre-releases).
    if (
        not include_prereleases
        and _is_prerelease(latest)
        and not _is_prerelease(current_version)
    ):
        return VersionCheck(
            current=current_version,
            latest=latest,
            is_newer=False,
            error_msg=None,
        )

    is_newer = latest_parsed > current_parsed
    return VersionCheck(
        current=current_version,
        latest=latest,
        is_newer=is_newer,
        error_msg=None,
    )


async def run_scheduled_update_check(home) -> None:  # type: ignore[no-untyped-def]
    """Daily cron callable. Calls :func:`check_for_update` and emits
    ``mimir_update_available`` (positive algedonic) when a newer
    version is available. Below-threshold runs emit nothing — no
    event noise when the operator is on the latest.

    Best-effort: any exception is logged and emits
    ``mimir_update_check_error`` but does not propagate. Daily retry
    pileup is the failure mode we want to avoid.

    The ``home`` argument is unused but kept in the signature for
    consistency with the other scheduler callables (which all take
    home for state directory access).
    """
    del home  # signature parity with sibling cron callables
    from .event_logger import log_event

    try:
        result = await asyncio.to_thread(check_for_update)
    except Exception as exc:  # noqa: BLE001 — defensive scheduler boundary
        log.exception("update-check raised unexpectedly")
        await log_event(
            "mimir_update_check_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    if result.error_msg:
        # Don't emit on 404 (expected pre-publication) or transient
        # network errors. Log at debug for diagnosis if operator asks.
        log.debug(
            "update-check: %s (current=%s, latest=%s)",
            result.error_msg, result.current, result.latest,
        )
        return

    if not result.is_newer:
        # On latest — silence.
        return

    await log_event(
        "mimir_update_available",
        current=result.current,
        latest=result.latest,
    )
