"""Smoke probe for the Codex Plus subscription protocol.

Reads ``~/.codex/auth.json`` (populated by ``codex login``), then GETs
``https://chatgpt.com/backend-api/codex/models?client_version=...`` with
the OAuth bearer token attached. Goal is NOT to do real work — it's to
confirm that:

1. ``CodexAuth`` parsing works against a real auth.json on this box.
2. The Codex API base accepts our bearer token without the full codex-rs
   client baggage (CA pinning, cookie store, etc).
3. Response carries the ``x-codex-primary-*`` / ``x-codex-secondary-*``
   rate-limit headers documented in ``codex-rs/codex-api/src/rate_limits.rs``.

If (3) holds, the architecture for ``OpenAIQuotaProvider`` is validated
— wiring becomes a response-header interceptor on a future Codex Plus
LangChain client, not a separate poller.

Usage::

    uv run python scripts/codex_plus_smoke_probe.py

Does not require any flags; reads ``CODEX_HOME`` from env (default
``~/.codex``).
"""
from __future__ import annotations

import json
import sys

import urllib.error
import urllib.request

from mimir.codex_auth import (
    CODEX_API_BASE,
    auth_file_path,
    is_likely_expired,
    load_codex_auth,
)


# Pulled from codex-rs/login/src/auth/default_client.rs:
# DEFAULT_ORIGINATOR = "codex_cli_rs". We claim to be a probe so any
# server-side telemetry doesn't conflate this with real codex traffic.
ORIGINATOR = "mimir_codex_probe"
USER_AGENT = "mimir-codex-smoke-probe/0.1"
# /codex/models is documented to take a `client_version` query param.
# Sending a recent-ish stub; the gateway is lenient about exact value.
CLIENT_VERSION = "0.99.0"


def main() -> int:
    auth = load_codex_auth()
    if auth is None:
        print(
            f"ERROR: no Codex OAuth at {auth_file_path()}. "
            f"Run `codex login` first.",
            file=sys.stderr,
        )
        return 2

    print(f"auth_mode      = {auth.auth_mode}")
    print(f"account_id     = {auth.account_id}")
    print(f"last_refresh   = {auth.last_refresh}")
    print(
        f"likely_expired = {is_likely_expired(auth)} "
        f"(heuristic: ttl_minutes=55)"
    )
    print(f"access_token   = <{len(auth.access_token)} chars, redacted>")
    print()

    url = (
        f"{CODEX_API_BASE}/codex/models?client_version={CLIENT_VERSION}"
    )
    print(f"GET {url}")
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {auth.access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "originator": ORIGINATOR,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        # Important: rate-limit headers come back on errors too.
        status = e.code
        headers = dict(e.headers.items()) if e.headers else {}
        body_bytes = e.read() if e.fp else b""
        print(f"\nHTTPError {status}")
    except urllib.error.URLError as e:
        print(f"\nURLError: {e.reason}", file=sys.stderr)
        return 3

    print(f"\nstatus = {status}")
    print()
    print("--- rate-limit / quota headers ---")
    interesting = sorted(
        (k, v)
        for k, v in headers.items()
        if k.lower().startswith(("x-codex", "x-ratelimit"))
        or k.lower() in {"retry-after", "x-request-id"}
    )
    if not interesting:
        print("(none returned)")
    for k, v in interesting:
        print(f"{k}: {v}")

    print()
    print("--- other headers (sample) ---")
    for k in (
        "Date",
        "Content-Type",
        "Cf-Ray",
        "Server",
        "Etag",
    ):
        if k in headers:
            print(f"{k}: {headers[k]}")

    print()
    print("--- body (first 600 chars) ---")
    body_text = body_bytes.decode("utf-8", errors="replace")
    if body_text.startswith("{") and len(body_text) < 8000:
        try:
            parsed = json.loads(body_text)
            print(json.dumps(parsed, indent=2)[:600])
        except json.JSONDecodeError:
            print(body_text[:600])
    else:
        print(body_text[:600])

    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
