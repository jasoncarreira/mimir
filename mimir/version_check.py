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
additionally guard against operators who explicitly publish
``0.2.0rc1`` as a non-prerelease and against parsing edge cases by
filtering version strings containing ``a``, ``b``, ``rc``, ``dev``,
``alpha``, ``beta`` (case-insensitive) unless the local version is
itself a pre-release. Operators who want pre-release surfacing pass
``include_prereleases=True``.

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

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


def _pypi_package_name() -> str:
    """Return the PyPI distribution name to check.

    Defaults to ``"mimir"`` but a name collision with an unrelated
    package (Ralph Meijer's "Mimir daemons", existing on PyPI under
    that name) means the open-source release uses a different
    distribution name. Operator sets ``MIMIR_PYPI_PACKAGE_NAME`` to
    the actual published name (e.g., ``mimir-agent``, ``odin-mimir``)
    when the release happens.

    Until the env is set, the check returns "no signal" via the 404
    path (the local ``mimir`` name finds the unrelated package, which
    will pass the version comparison harmlessly since semver tuples
    differ and pre-release filtering doesn't apply, but the check
    surfaces an unrelated project's release as a mimir update — bad).
    """
    return os.environ.get("MIMIR_PYPI_PACKAGE_NAME", "mimir").strip() or "mimir"

# 5-second timeout matches the existing OAuth poller etc. — PyPI is a
# CDN-backed endpoint that should respond in << 1s; longer timeouts
# just delay the cron when network is degraded.
_HTTP_TIMEOUT_S = 5.0

# Pre-release markers that bar a version from "latest" auto-surfacing.
# Case-insensitive match against any of these substrings in the
# version string.
_PRERELEASE_MARKERS = ("dev", "alpha", "beta", "rc", "pre", "a", "b")


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


def _parse_version(text: str) -> Optional[tuple[int, ...]]:
    """Return a comparable tuple-of-ints for a dotted-int version
    string. Returns ``None`` for any input that doesn't match the
    common ``N[.N[.N…]]`` shape — pre-release suffixes etc. fall
    through to the marker-based filter rather than trying to parse
    semver completely.
    """
    text = text.strip()
    if not text:
        return None
    # Match leading dotted-int prefix. ``0.1.0.post1`` → ``(0, 1, 0)``;
    # ``1.0.0rc1`` → ``(1, 0, 0)``. The numeric core is what we order on;
    # the trailing junk is handled by the pre-release marker filter.
    m = re.match(r"(\d+(?:\.\d+)*)", text)
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def _is_prerelease(version: str) -> bool:
    """True iff the version string contains a pre-release marker.

    Case-insensitive substring match — ``0.2.0rc1`` and
    ``1.0.0.dev42`` both qualify. ``a`` and ``b`` are matched only
    when they appear AFTER a digit (so version ``2.0`` doesn't trip,
    but ``2.0a1`` does)."""
    lowered = version.lower()
    # The single-letter markers ``a`` and ``b`` need anchoring to a
    # digit boundary; otherwise they'd match version names containing
    # those letters (e.g., "abc-package-1.0"). We're parsing a version
    # string here, not a package name, so leading-letter cases are
    # unlikely — but the anchoring is cheap insurance.
    for marker in ("dev", "alpha", "beta", "rc", "pre"):
        if marker in lowered:
            return True
    if re.search(r"\d[ab]\d", lowered):
        return True
    return False


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
      2. The reported latest version's numeric prefix is strictly
         greater than the current version's
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

    current_tuple = _parse_version(current_version)
    latest_tuple = _parse_version(latest)
    if current_tuple is None or latest_tuple is None:
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

    is_newer = latest_tuple > current_tuple
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
        result = check_for_update()
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
