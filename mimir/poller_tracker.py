"""Shared validators, constants and notice formatters for tracker pollers.

Tracker pollers (``trust_source: tracker``) deliver text written by people the
host cannot vouch for. Everything that crosses from that text into a host
structure (extras, scope targets, operator notices) passes through the strict
validators here, so a hostile issue cannot smuggle markup, credentials or
instructions through an identifier. This module imports nothing from the rest
of the host so ``pollers``, ``poller_recovery`` and ``access_control`` can all
use it without a cycle.
"""

from __future__ import annotations

import re
import urllib.parse

from .prompt_safety import sanitize_prompt_field

TRACKER_SIGNALS = frozenset({"tracker_issue_withheld", "tracker_transition_unavailable"})
TRACKER_EXTRA_KEYS = ("delivery_key", "adapter", "ref", "url", "kind", "attested_event_id")
TRACKER_KINDS = frozenset({"triage", "update"})
WITHHELD_REASONS = frozenset({
    "untrusted_latest_activity", "no_trusted_attestation", "thread_too_long",
})
LIFECYCLE_EVENTS = frozenset({"filed", "build_started", "pr_opened", "merged", "agent"})
NOTICE_REASONS = frozenset({
    "state_not_found", "ambiguous_state", "permission_denied", "unsupported",
    "merged_state_required", "api_error", "failed",
})
TRACKER_STDERR_CODE_RE = re.compile(r"[a-z0-9_:.-]{1,120}")

_ADAPTER_RE = re.compile(r"[a-z][a-z_]{0,31}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.#/-]{0,127}")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_PATH_RE = re.compile(r"(/[A-Za-z0-9._~-]{1,128}){1,8}")
_URL_CHARS_RE = re.compile(r"[A-Za-z0-9._~:/-]+")
_EVENT_ID_RE = re.compile(r"ev[0-9a-f]{24}")
_DELIVERY_KEY_RE = re.compile(r"trk-[0-9a-f]{24}")
_ACTOR_RE = re.compile(r"[\w.@:\[\]-]{1,128}", re.ASCII)
_URL_MAX_CHARS = 300
_TITLE_NOTICE_CHARS = 200


def _match(pattern: re.Pattern[str], value: object) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return None


def safe_adapter(value: object) -> str | None:
    return _match(_ADAPTER_RE, value)


def safe_ref(value: object) -> str | None:
    return _match(_REF_RE, value)


def safe_event_id(value: object) -> str | None:
    return _match(_EVENT_ID_RE, value)


def safe_delivery_key(value: object) -> str | None:
    return _match(_DELIVERY_KEY_RE, value)


def safe_actor(value: object) -> str | None:
    return _match(_ACTOR_RE, value)


def safe_url(url: object, ref: object) -> str | None:
    """Return ``url`` when it is a plain https issue URL naming ``ref``."""
    if not isinstance(url, str) or not isinstance(ref, str) or len(url) > _URL_MAX_CHARS:
        return None
    if safe_ref(ref) is None or not _URL_CHARS_RE.fullmatch(url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.query or parts.fragment:
        return None
    if ":" in parts.netloc:
        return None
    if not _HOST_RE.fullmatch(parts.netloc) or not _PATH_RE.fullmatch(parts.path):
        return None
    if parts.path.rsplit("/", 1)[-1] != re.split(r"[#/]", ref)[-1]:
        return None
    return url


def withheld_lines(adapter: str, ref: str, url: str, reason: str, title: str | None) -> tuple[str, str]:
    """Return ``(live, title_free)`` notice text for a withheld issue."""
    title_free = f"Tracker issue withheld: {adapter} {ref} ({reason}) {url}"
    live = title_free + " - " + sanitize_prompt_field(title or "")[:_TITLE_NOTICE_CHARS]
    return live, title_free


def transition_line(adapter: str, ref: str, url: str, lifecycle_event: str, reason: str) -> str:
    return (
        f"Tracker transition unavailable: {adapter} {ref} {lifecycle_event} ({reason}) "
        f"{url or '(no URL)'}"
    )
