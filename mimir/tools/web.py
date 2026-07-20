"""Web search + URL fetch tools (gated on LLM provider).

Two tools, ported from open-strix's ``tools.py``:

* ``web_search`` — Tavily HTTP API. Returns compact YAML results.
* ``fetch_url``  — download a URL into ``<home>/attachments/fetch-cache/``
  and return the virtual path + metadata; the agent then re-reads
  the body via the standard Read tool.

These are added to ``all_mimir_tools()`` only when the configured
LLM provider is **not** ``claude_code`` — Claude Code subprocesses
get WebSearch/WebFetch natively from the SDK, so layering Tavily
on top would be both redundant and stylistically inconsistent.
For external providers (anthropic API, openai-compat, voyage, …)
these tools are how the agent reaches the open web.

``web_search`` additionally requires ``TAVILY_API_KEY``. ``fetch_url``
does not — it's a thin urllib wrapper. Operators can disable
``fetch_url`` independently via ``MIMIR_FETCH_URL_DISABLED=1``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

import yaml
from langchain_core.tools import tool

from .web_search_destination import web_search_url

log = logging.getLogger(__name__)

FETCH_CHUNK_SIZE_BYTES = 64 * 1024
UTC = timezone.utc

# ─── Module-level dependency injection ─────────────────────────────
# Set once at server startup; tools read these at invocation time so
# tests can override without monkeypatching the @tool decorators.

_home: Path | None = None
_authorized_fetch_urls: ContextVar[frozenset[str] | None] = ContextVar(
    "mimir_authorized_fetch_urls", default=None,
)


def set_home(home: Path) -> None:
    """Mimir home path. Used to compute the fetch-cache directory."""
    global _home
    _home = home


def begin_authorized_fetch(urls: frozenset[str]) -> Token:
    """Bind the exact URLs authorized for one middleware-controlled fetch."""
    return _authorized_fetch_urls.set(urls)


def end_authorized_fetch(token: Token) -> None:
    _authorized_fetch_urls.reset(token)


def _fetch_cache_dir() -> Path:
    if _home is None:
        raise RuntimeError(
            "mimir.tools.web: set_home(...) was never called — "
            "wire it from mimir.server:build_app before agent construction."
        )
    return _home / "attachments" / "fetch-cache"


# ─── HTTP helpers (verbatim port from open-strix) ──────────────────


def _virtual_path(path: Path, *, root: Path) -> str:
    return "/" + path.relative_to(root).as_posix()


def _sanitize_download_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not cleaned:
        return "download.bin"
    if len(cleaned) <= 120:
        return cleaned
    suffix = Path(cleaned).suffix
    stem = Path(cleaned).stem[: max(1, 120 - len(suffix))]
    return f"{stem}{suffix}"


def _name_from_url(url: str) -> str:
    parsed = urlparse(url)
    raw_name = Path(unquote(parsed.path)).name
    if not raw_name:
        raw_name = "index.html" if parsed.path in {"", "/"} else "download.bin"
    name = _sanitize_download_name(raw_name)
    if "." not in name:
        return f"{name}.bin"
    return name


class SSRFBlocked(ValueError):
    """Raised when a fetch target resolves to a non-public address.

    Subclass of ValueError so the existing exception handling on
    fetch_url / web_search picks it up — but distinct enough that
    callers can re-raise / log specifically if needed.
    """


def _validate_public_host(hostname: str) -> None:
    """Reject SSRF-shaped targets before any network call.

    Resolves ``hostname`` via ``getaddrinfo`` and asserts every returned
    address is a public unicast IP — private RFC1918, loopback,
    link-local (``169.254.0.0/16`` covers AWS/GCP metadata),
    site-local, multicast, reserved, and the literal ``localhost``
    string all reject. Defends against prompt-controlled URLs pivoting
    the agent into the operator's internal network.

    Note on DNS rebinding: this call resolves the host once. The
    subsequent ``urlopen`` re-resolves at connect time, so a TTL-0
    record returning ``1.2.3.4`` here and ``127.0.0.1`` at connect
    is a residual gap — fully closing it requires connecting to the
    IP literal with a ``Host:`` header override, which breaks HTTPS
    cert validation without significant urllib3 plumbing. The
    redirect handler below re-validates each hop, so the practical
    attack window is narrow. Documented; defer to a follow-up.
    """
    if not hostname or hostname.lower() in ("localhost", "localhost.localdomain"):
        raise SSRFBlocked(f"fetch target host {hostname!r} is not allowed")
    # Resolve. If the host is an IP literal getaddrinfo just echoes it.
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFBlocked(f"DNS resolution failed for {hostname!r}: {exc}") from exc
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise SSRFBlocked(
                f"could not parse resolved address {ip_str!r} for {hostname!r}"
            ) from None
        # Reject every category that could land traffic inside the
        # network perimeter or against a service-meta endpoint.
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise SSRFBlocked(
                f"fetch target {hostname!r} resolves to non-public address {ip_str}"
            )


def _validate_fetch_url(url: str) -> None:
    """Run SSRF + scheme checks on a candidate URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SSRFBlocked(f"only http/https URLs are allowed (got {parsed.scheme!r})")
    if not parsed.hostname:
        raise SSRFBlocked("fetch target has no hostname")
    _validate_public_host(parsed.hostname)


class _SSRFCheckingRedirectHandler(HTTPRedirectHandler):
    """``HTTPRedirectHandler`` that re-runs ``_validate_fetch_url`` on every hop.

    Without this, a whitelisted public URL can ``Location:``-redirect to
    ``http://169.254.169.254/...`` (AWS metadata service) — the initial
    validation passed but the second hop is unchecked. Re-validating on
    each redirect closes the cross-hop bypass.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        # Will raise SSRFBlocked if the redirect target is unsafe.
        _validate_fetch_url(newurl)
        allowed = _authorized_fetch_urls.get()
        if allowed is not None:
            from ..access_control import SinkCategory, normalize_sink_destination

            normalized = normalize_sink_destination(SinkCategory.NETWORK, newurl)
            if normalized not in allowed:
                raise SSRFBlocked(
                    f"redirect target exact URL {newurl!r} is not approved"
                )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(request: Request, timeout: int):
    """Open a URL with the SSRF-checking redirect handler installed."""
    opener = build_opener(_SSRFCheckingRedirectHandler())
    return opener.open(request, timeout=timeout)  # noqa: S310


def _download_url_bytes(
    *,
    url: str,
    target_path: Path,
    timeout_seconds: int,
    max_bytes: int,
) -> dict[str, Any]:
    request = Request(url=url, headers={"User-Agent": "mimir/fetch_url"})
    with _open_url(request, timeout=timeout_seconds) as response:
        status = int(response.getcode() or 0)
        final_url = str(response.geturl())
        content_type = str(response.headers.get("Content-Type", ""))
        # Content-Length pre-check: refuse the download before writing
        # a single byte if the server advertises a size beyond max_bytes.
        # The streaming check below remains the source of truth for
        # responses without Content-Length or with lying headers.
        advertised_len = response.headers.get("Content-Length")
        if advertised_len is not None:
            try:
                if int(advertised_len) > max_bytes:
                    raise ValueError(
                        f"server advertised Content-Length={advertised_len} > "
                        f"max_bytes={max_bytes} for url={url}",
                    )
            except (TypeError, ValueError) as exc:
                # Re-raise our own ValueError; ignore unparseable
                # Content-Length headers and fall back to streaming check.
                if isinstance(exc, ValueError) and "max_bytes" in str(exc):
                    raise
        total_bytes = 0
        hasher = hashlib.sha256()
        with target_path.open("wb") as f:
            while True:
                chunk = response.read(FETCH_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(
                        f"download exceeded max_bytes={max_bytes} for url={url}",
                    )
                hasher.update(chunk)
                f.write(chunk)
    return {
        "status": status,
        "final_url": final_url,
        "content_type": content_type,
        "bytes": total_bytes,
        "sha256": hasher.hexdigest(),
    }


def _post_json(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: int,
    max_bytes: int = 2_000_000,
) -> dict[str, Any]:
    request_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "mimir/web_search",
        **headers,
    }
    # Tavily endpoint is a public API but we still gate it through the
    # SSRF check — operator-overridden TAVILY_SEARCH_URL could point
    # anywhere and the same defenses apply.
    _validate_fetch_url(url)
    request = Request(url=url, data=request_bytes, headers=request_headers, method="POST")
    with _open_url(request, timeout=timeout_seconds) as response:
        status = int(response.getcode() or 0)
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"response exceeded max_bytes={max_bytes} for url={url}")
        decoded = body.decode("utf-8", errors="replace")
        parsed = json.loads(decoded)
        return {
            "status": status,
            "json": parsed,
            "response_bytes": len(body),
            "final_url": str(response.geturl()),
        }


# ─── Tools ─────────────────────────────────────────────────────────


@tool("web_search")
async def web_search(
    query: str,
    limit: int = 5,
    topic: str = "general",
    time_range: str | None = None,
    timeout_seconds: int = 20,
) -> str:
    """Search the web via Tavily and return compact YAML-formatted results.

    Args:
        query: Search text. Required.
        limit: Max results to return (1-10). Defaults to 5.
        topic: One of ``general``, ``news``, ``finance``.
        time_range: Optional ``day``, ``week``, ``month``, or ``year``.
        timeout_seconds: HTTP timeout. Must be > 0.

    Returns YAML with rank/title/url/snippet/score per hit. Snippets
    are truncated at 800 chars.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return "query is required."
    if limit <= 0:
        return "limit must be > 0."
    if limit > 10:
        limit = 10

    normalized_topic = topic.strip().lower()
    if normalized_topic not in {"general", "news", "finance"}:
        return "topic must be one of: general, news, finance."

    normalized_time_range = time_range.strip().lower() if time_range else None
    if normalized_time_range and normalized_time_range not in {"day", "week", "month", "year"}:
        return "time_range must be one of: day, week, month, year."
    if timeout_seconds <= 0:
        return "timeout_seconds must be > 0."

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return "web_search is disabled (TAVILY_API_KEY not set)."

    search_url = web_search_url()

    payload: dict[str, Any] = {
        "query": normalized_query,
        "topic": normalized_topic,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if normalized_time_range:
        payload["time_range"] = normalized_time_range

    try:
        response = await asyncio.to_thread(
            _post_json,
            url=search_url,
            payload=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout_seconds=timeout_seconds,
        )
    except HTTPError as exc:
        return f"web_search failed: HTTP {exc.code} ({exc.reason})"
    except URLError as exc:
        return f"web_search failed: {getattr(exc, 'reason', exc)}"
    except (ValueError, json.JSONDecodeError) as exc:
        return f"web_search failed: {exc}"

    raw = response["json"]
    rows = raw.get("results")
    if not isinstance(rows, list):
        rows = []

    compact: list[dict[str, Any]] = []
    for idx, item in enumerate(rows[:limit], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        snippet = str(item.get("content", "")).strip()
        if len(snippet) > 800:
            snippet = snippet[:800].rstrip() + "..."
        compact.append(
            {"rank": idx, "title": title, "url": url, "snippet": snippet, "score": item.get("score")},
        )

    return yaml.safe_dump(
        {
            "query": normalized_query,
            "topic": normalized_topic,
            "time_range": normalized_time_range,
            "count": len(compact),
            "results": compact,
            "response_time": raw.get("response_time"),
        },
        sort_keys=False,
    )


@tool("fetch_url")
async def fetch_url(
    url: str,
    timeout_seconds: int = 20,
    max_bytes: int = 2_000_000,
) -> str:
    """Download a URL to a cache file and return the virtual path + metadata.

    The body is written to ``<home>/attachments/fetch-cache/<stamp>-<digest>-<name>``;
    a sidecar ``.meta.json`` records URL, status, content-type, sha256,
    and bytes. The agent re-reads the body via the standard Read tool
    using the returned ``file_path``.

    Args:
        url: HTTP/HTTPS URL.
        timeout_seconds: Socket timeout. Must be > 0.
        max_bytes: Hard cap on body size; the download aborts if exceeded.
    """
    normalized_url = url.strip()
    if not normalized_url:
        return "url is required."
    if timeout_seconds <= 0:
        return "timeout_seconds must be > 0."
    if max_bytes <= 0:
        return "max_bytes must be > 0."

    # SSRF + scheme validation. Returns a friendly error string instead
    # of letting the SSRFBlocked exception bubble out as a generic
    # "ValueError: …" — the agent reads this back as a tool result.
    try:
        _validate_fetch_url(normalized_url)
    except SSRFBlocked as exc:
        return f"fetch_url failed: {exc}"

    cache_dir = _fetch_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    base_name = _name_from_url(normalized_url)
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:12]
    # Dedup on URL digest: the previous filename included a UTC stamp
    # so repeat fetches of the same URL piled up on disk. Now the
    # cache file is keyed solely on the digest + sanitized name, and
    # we short-circuit if a cached body for this exact URL already
    # exists (returning the existing metadata sidecar without
    # re-downloading). Operators wanting forced refresh can delete the
    # files manually.
    body_path = cache_dir / f"{digest}-{base_name}"
    meta_path = cache_dir / f"{body_path.name}.meta.json"
    if body_path.is_file() and meta_path.is_file():
        try:
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_meta = None
        if existing_meta and existing_meta.get("url") == normalized_url:
            return yaml.safe_dump(existing_meta, sort_keys=False)

    try:
        fetched = await asyncio.to_thread(
            _download_url_bytes,
            url=normalized_url,
            target_path=body_path,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except HTTPError as exc:
        return f"fetch_url failed: HTTP {exc.code} ({exc.reason})"
    except URLError as exc:
        return f"fetch_url failed: {getattr(exc, 'reason', exc)}"
    except ValueError as exc:
        body_path.unlink(missing_ok=True)
        return f"fetch_url failed: {exc}"
    except OSError:
        body_path.unlink(missing_ok=True)
        return "fetch_url failed: could not write downloaded content."

    if _home is None:
        return "fetch_url failed: home dir not configured."

    body_virtual_path = _virtual_path(body_path, root=_home)
    meta_virtual_path = _virtual_path(meta_path, root=_home)
    meta_payload = {
        "url": normalized_url,
        "final_url": fetched["final_url"],
        "status": fetched["status"],
        "content_type": fetched["content_type"],
        "bytes": fetched["bytes"],
        "sha256": fetched["sha256"],
        "file_path": body_virtual_path,
        "metadata_path": meta_virtual_path,
    }
    meta_path.write_text(
        json.dumps(meta_payload, ensure_ascii=True, sort_keys=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return yaml.safe_dump(meta_payload, sort_keys=False)


# ─── Provider gating ───────────────────────────────────────────────


def _provider_from_model_spec(model_spec: str | None) -> str:
    """Extract the provider prefix from a ``MIMIR_MODEL_SPEC`` value.

    The spec format is ``provider:model`` (e.g. ``claude-code:claude-sonnet-4-6``,
    ``anthropic:claude-haiku-4-5``, ``openai:gpt-5.4-nano``). Falls back to
    ``"claude_code"`` when unparseable — matches mimir's default model_spec.
    Both ``claude-code`` and ``claude_code`` are normalized to ``claude_code``.
    """
    if not model_spec:
        return "claude_code"
    head, _, _ = model_spec.partition(":")
    return head.strip().lower().replace("-", "_") or "claude_code"


def web_tools_enabled(model_spec: str | None = None) -> tuple[bool, bool]:
    """Return ``(web_search_enabled, fetch_url_enabled)`` for the active provider.

    * ``web_search`` is enabled only when the provider is not ``claude_code``
      AND ``TAVILY_API_KEY`` is set.
    * ``fetch_url`` is enabled only when the provider is not ``claude_code``
      AND ``MIMIR_FETCH_URL_DISABLED`` is not truthy.

    Claude Code path already exposes native WebSearch/WebFetch tools via
    the subprocess SDK; adding Tavily on top would duplicate (and possibly
    conflict with) the native surface.
    """
    spec = model_spec if model_spec is not None else os.environ.get("MIMIR_MODEL_SPEC", "")
    provider = _provider_from_model_spec(spec)
    if provider == "claude_code":
        return (False, False)
    web_search_on = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    fetch_url_on = os.environ.get("MIMIR_FETCH_URL_DISABLED", "").strip().lower() not in (
        "1", "true", "yes", "on",
    )
    return (web_search_on, fetch_url_on)
