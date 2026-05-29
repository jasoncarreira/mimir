"""Inbound + outbound attachment plumbing for chat bridges.

Inbound: when a user posts a file in Discord/Slack, the bridge downloads
it to ``MIMIR_HOME/attachments/<channel>/<chat>/<ts>-<uuid>-<safe-name>``
so the agent can ``Read`` it as a regular file. Per-channel-per-chat
sub-dirs keep things browsable; timestamp + 8-char UUID prefix avoids
filename collisions.

Outbound: when the agent emits ``<send-file path="...">`` directives the
path must resolve inside ``MIMIR_HOME/attachments/outbound/`` — escaping
is rejected. ``..`` is resolved before the containment check, and
symlinks are resolved before the check too, so neither bypass works.

Python port combining an inbound-builder + outbound-validator pattern
from sibling agent harnesses.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_DEFAULT_DOWNLOAD_TIMEOUT_S = 15.0

# Per-bridge CDN host allowlists for SSRF validation (chainlink #251).
# Suffix-matched: ``cdn.discordapp.com`` matches ``discordapp.com``.
# Extend if a bridge adds a new CDN origin (e.g. a new Discord media subdomain).
_DISCORD_CDN_HOSTS: frozenset[str] = frozenset({
    "discordapp.com",
    "discordapp.net",
    "discord.com",
})
_SLACK_CDN_HOSTS: frozenset[str] = frozenset({
    "slack.com",
    "slack-files.com",
    "slackb.com",  # Slack's CDN edge domain
})


class AttachmentPathError(ValueError):
    """Raised when an outbound attachment path escapes the outbound root
    or fails validation. Caller should surface this back to the agent
    so the next turn's feedback block reports the rejected directive."""


class AttachmentURLError(ValueError):
    """Raised when an attachment download URL fails SSRF security validation
    (non-HTTPS scheme or host not in the per-bridge CDN allowlist). Caller
    should log and skip the download rather than attempting a network request.
    """


def sanitize_filename(name: str) -> str:
    """Strip unsafe filename chars; collapse to ``"attachment"`` when
    sanitization leaves an empty string. Conservative — only ASCII
    alnum / ``.`` / ``_`` / ``-`` survive."""
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("_")
    return cleaned or "attachment"


def build_inbound_path(
    base_dir: Path, channel: str, chat_id: str, filename: str | None = None,
) -> Path:
    """Compute and create the directory for an inbound attachment.

    Returns the full target path (file may not exist yet — caller writes
    the bytes there). Channel/chat segments are sanitized so platform
    quirks (slack thread timestamps with ``.``, discord bot-user ids)
    don't blow up the filesystem layout.
    """
    safe_channel = sanitize_filename(channel)
    safe_chat = sanitize_filename(chat_id)
    safe_name = sanitize_filename(filename or "attachment")
    dir_path = base_dir / safe_channel / safe_chat
    dir_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex[:8]
    return dir_path / f"{stamp}-{token}-{safe_name}"


def resolve_outbound_path(outbound_root: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` against ``outbound_root`` and verify the
    result stays inside the root. Raises ``AttachmentPathError`` on
    escape, missing file, or non-file target.

    Path semantics:
    - Tildes (``~``) expand to the home dir before the containment
      check; if the resolved path is outside the root, that's an error.
    - Absolute paths must already resolve inside the root.
    - Relative paths are resolved against the root.
    - ``..`` segments are flattened by Path.resolve(); symlinks are
      followed, so symlink-out tricks fail the check too.
    """
    if not raw_path or not raw_path.strip():
        raise AttachmentPathError("send-file: path is empty")
    candidate = Path(raw_path.strip()).expanduser()
    root = outbound_root.resolve()
    if candidate.is_absolute():
        absolute = candidate.resolve()
        if root not in absolute.parents and absolute != root:
            raise AttachmentPathError(
                f"send-file: absolute path {raw_path!r} is outside the "
                f"outbound attachments dir"
            )
        resolved = absolute
    else:
        resolved = (outbound_root / candidate).resolve()
        # ``in parents`` is False when the path equals the root itself
        # (a directory, not a file) — handled by the is_file check below.
        if root not in resolved.parents and resolved != root:
            raise AttachmentPathError(
                f"send-file: path {raw_path!r} escapes the outbound "
                f"attachments dir"
            )

    if not resolved.exists():
        raise AttachmentPathError(
            f"send-file: file not found: {raw_path!r}"
        )
    if not resolved.is_file():
        raise AttachmentPathError(
            f"send-file: path is not a file: {raw_path!r}"
        )
    return resolved


def validate_download_url(url: str, allowed_host_suffixes: frozenset[str]) -> None:
    """Validate that *url* is HTTPS and its host matches one of the allowed
    CDN host suffixes (exact match OR ``host.endswith('.' + suffix)``).

    Raises :exc:`AttachmentURLError` on any violation. Must be called
    *before* opening a network connection (chainlink #251 — SSRF guard).

    Guard contract
    ~~~~~~~~~~~~~~
    Without this check, ``download_to_path`` would GET whatever URL arrived
    in the message payload — including ``http://169.254.169.254/...`` (cloud
    metadata) or other internal targets. Two validations:

    1. **Scheme**: must be ``https``. Blocks plain-HTTP downgrade and
       non-HTTP schemes (``file://``, ``ftp://``, ``gopher://``, …).
    2. **Host allowlist**: host must match a per-bridge CDN suffix. Blocks
       requests to arbitrary external hosts and to RFC-1918 / link-local
       addresses even if the scheme is ``https``.

    Complements the redirect guard shipped in chainlink #252 (which blocks
    token leakage *via* a redirect); this blocks SSRF on the *initial* URL.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AttachmentURLError(
            f"download: rejecting non-HTTPS URL (scheme={parsed.scheme!r}): {url!r}"
        )
    host = (parsed.hostname or "").lower()
    if not host or not any(
        host == s or host.endswith("." + s) for s in allowed_host_suffixes
    ):
        raise AttachmentURLError(
            f"download: URL host {host!r} not in CDN allowlist "
            f"({', '.join(sorted(allowed_host_suffixes))}): {url!r}"
        )


def _carries_auth(headers: dict[str, str] | None) -> bool:
    """True if *headers* carries an Authorization credential (case-
    insensitive key match). Used to decide whether following redirects
    is safe."""
    if not headers:
        return False
    return any(k.lower() == "authorization" for k in headers)


async def download_to_path(
    url: str, target: Path, *, max_bytes: int | None = None,
    timeout_s: float = _DEFAULT_DOWNLOAD_TIMEOUT_S,
    headers: dict[str, str] | None = None,
    allow_redirects: bool | None = None,
    allowed_host_suffixes: frozenset[str] | None = None,
) -> bool:
    """Stream ``url`` to ``target``. Returns True on success, False on
    failure (logged at WARNING). When ``max_bytes`` is set and the
    download exceeds it mid-stream, the partial file is removed and
    False is returned — Discord/Slack tell us the size up-front so this
    is a defense-in-depth check, not the primary size gate.

    ``headers`` are forwarded to the GET request. Useful for
    authenticated endpoints (e.g. Slack ``url_private`` requires
    ``Authorization: Bearer <bot-token>``).

    ``allow_redirects`` (chainlink #252): aiohttp re-sends request
    headers — including ``Authorization`` — on cross-origin redirects,
    unlike curl/requests which strip credentials on a host change. A
    30x to an attacker-controlled host would therefore receive the live
    bearer token. When ``allow_redirects`` is left ``None`` (default),
    it auto-resolves to ``False`` whenever the request carries an
    ``Authorization`` header (so an authed download cannot leak its
    credential across a redirect) and ``True`` otherwise (Discord CDN
    downloads are unauthenticated and may legitimately redirect). Pass
    an explicit bool to override. Slack ``url_private`` resources never
    legitimately redirect off Slack's domain, so disabling redirects
    for the authed path is safe.

    ``allowed_host_suffixes`` (chainlink #251 — SSRF gate): when
    provided, ``validate_download_url`` is called *before* opening any
    network connection. The URL must be HTTPS and its host must match one
    of the allowed CDN host suffixes. Pass ``_DISCORD_CDN_HOSTS`` or
    ``_SLACK_CDN_HOSTS`` from the respective bridge. When ``None``
    (default) validation is skipped — for backward compatibility in tests
    and non-bridge call sites. New call sites from bridge code MUST pass
    an allowlist.

    Uses aiohttp lazily so non-bridge deployments don't pay the import.
    """
    if allowed_host_suffixes is not None:
        try:
            validate_download_url(url, allowed_host_suffixes)
        except AttachmentURLError as exc:
            log.warning("download_to_path: SSRF guard rejected URL — %s", exc)
            return False

    try:
        import aiohttp
    except ImportError:
        log.error("download_to_path: aiohttp not installed")
        return False

    if allow_redirects is None:
        # Credentialed request → don't follow redirects (token-leak guard).
        allow_redirects = not _carries_auth(headers)

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=headers, allow_redirects=allow_redirects,
            ) as resp:
                if resp.status >= 400:
                    log.warning(
                        "download_to_path: %s returned %s", url, resp.status,
                    )
                    return False
                with target.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            log.warning(
                                "download_to_path: %s exceeded %s bytes",
                                url, max_bytes,
                            )
                            try:
                                target.unlink(missing_ok=True)
                            except OSError:
                                pass
                            return False
                        f.write(chunk)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("download_to_path: %s failed (%s)", url, exc)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return False


__all__ = [
    "AttachmentPathError",
    "AttachmentURLError",
    "validate_download_url",
    "_DISCORD_CDN_HOSTS",
    "_SLACK_CDN_HOSTS",
    "sanitize_filename",
    "build_inbound_path",
    "resolve_outbound_path",
    "download_to_path",
]
