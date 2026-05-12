#!/usr/bin/env python3
"""Gmail inbox poller backed by the `gog` CLI (Google Workspace).

Watches `in:inbox` (or a caller-supplied search) for new messages,
emits one JSONL event per never-before-seen message ID to stdout.

Cursor is a SET of message IDs, not a timestamp — Gmail can deliver
out of order (server-side spam filters re-route, late-arriving items
backdate), so a timestamp cursor would either miss or double-emit.
A rolling set keyed on the stable Gmail message ID is correct
regardless of order.

Cursor size cap: 500 IDs (LRU evict — oldest emission first). 5
minutes between polls × 500 IDs covers ~40 hours of busy email; a
weeklong vacation backlog flushes through naturally.

Environment:
    STATE_DIR              - Persistent cursor dir (set by framework)
    POLLER_NAME            - This poller's name
    GOG_ACCOUNT            - Required. Gmail account to poll. e.g. you@example.com
    MIMIR_GMAIL_QUERY      - Optional. Gmail search query override.
                             Default: "in:inbox newer_than:1d".
                             Narrow with sender/label filters as needed.
    MIMIR_GMAIL_MAX_FETCH  - Optional. Per-poll fetch cap. Default: 50.

Requires `gog` binary (https://gogcli.sh) installed and authed for
GOG_ACCOUNT. See SKILL.md install section.

Output contract:
    stdout: JSONL — {"poller": str, "prompt": str, ...extras} per event
    stderr: diagnostic logging
    exit 0: success (zero events = silence-as-filter, nothing new)
    non-zero: error (framework drops any events emitted by this run)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
CURSOR_FILE = STATE_DIR / "cursor.json"
POLLER_NAME = os.environ.get("POLLER_NAME", "gmail-inbox")

# Default search: last 24h in inbox. Caller can narrow via
# MIMIR_GMAIL_QUERY (e.g. ``"in:inbox is:unread newer_than:6h
# -from:noreply@*"``). The framework re-fires this every cron tick
# so the window only needs to cover one tick's worth of latency
# tolerance plus a margin — 1d is conservative.
DEFAULT_QUERY = "in:inbox newer_than:1d"

# Per-poll fetch cap. 50 is comfortable for a 5-min cron + busy
# inbox; the cursor-set dedup handles the case where the window
# returns the same messages on every poll until they age out.
DEFAULT_MAX_FETCH = 50

# Cursor cap. Each ID is ~16 chars + JSON overhead → ~40 bytes ×
# 500 = 20KB on disk. LRU evict keeps the file bounded while still
# covering a multi-day vacation backlog (5min × 500 = ~40h).
CURSOR_MAX_IDS = 500

# Prompt body preview cap — gog's `snippet` is already short
# (Gmail truncates to ~200 chars), but the SDK-side prompt budget
# is 16KB total so being defensive here is cheap.
SNIPPET_PREVIEW_CHARS = 300


def _eprint(*args, **kwargs) -> None:
    """Print to stderr (becomes a `poller_stderr` event for grep)."""
    print(*args, file=sys.stderr, **kwargs)


def _load_cursor() -> list[str]:
    """Return the ordered list of previously-emitted message IDs.

    Order is insertion-order (oldest first) so LRU eviction at the
    head works without metadata. JSON load is defensive — corrupt
    file = empty cursor + warn; we'd rather re-emit a backlog than
    crash the poller.
    """
    if not CURSOR_FILE.exists():
        return []
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _eprint(f"gmail-poller: cursor load failed ({exc}); resetting")
        return []
    if isinstance(data, list):
        return [str(x) for x in data]
    # Handle a corrupt non-list shape — treat as missing.
    return []


def _save_cursor(ids: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write via tempfile + rename so a crashed poller mid-write
    # doesn't leave a half-truncated cursor that future runs misread.
    tmp = CURSOR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


def _gog_search(account: str, query: str, max_fetch: int) -> list[dict]:
    """Invoke `gog gmail messages search --json` and return the parsed
    list. Raises CalledProcessError on non-zero exit (handled by main).

    Why ``messages search`` and not ``search``: the latter returns one
    row per thread (and threads conflate replies). For a poller we want
    per-message granularity so a thread that gets a new reply produces
    one event for THAT message, not the whole thread again.
    """
    cmd = [
        "gog", "gmail", "messages", "search",
        query,
        "--account", account,
        "--max", str(max_fetch),
        "--json",
        "--no-input",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=45,
    )
    if proc.returncode != 0:
        # Surface gog's stderr so operator can diagnose auth /
        # network issues without running gog manually.
        _eprint(
            f"gog exit {proc.returncode}; stderr: "
            f"{proc.stderr.strip()[:500]}"
        )
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr,
        )
    if not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        _eprint(
            f"gog returned unparseable JSON ({exc}); stdout head: "
            f"{proc.stdout[:200]!r}"
        )
        raise
    # gog wraps results in a top-level object — accept either
    # ``{"messages": [...]}``, ``{"results": [...]}``, or a bare list.
    # Defensive against minor cli-side shape drift.
    if isinstance(data, dict):
        for key in ("messages", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        # Single-message envelope? Wrap it.
        if "id" in data:
            return [data]
        return []
    if isinstance(data, list):
        return data
    return []


def _format_event(msg: dict) -> dict | None:
    """Build the JSONL event for one Gmail message.

    Returns None if the message lacks an ``id`` (we have nothing to
    cursor on — safer to skip than emit an un-deduplicable event).
    """
    msg_id = msg.get("id") or msg.get("messageId") or msg.get("message_id")
    if not msg_id:
        return None
    sender = (
        msg.get("from")
        or msg.get("sender")
        or (msg.get("headers") or {}).get("From")
        or "<unknown>"
    )
    subject = (
        msg.get("subject")
        or (msg.get("headers") or {}).get("Subject")
        or "<no subject>"
    )
    snippet = (msg.get("snippet") or "").strip()
    if len(snippet) > SNIPPET_PREVIEW_CHARS:
        snippet = snippet[: SNIPPET_PREVIEW_CHARS - 1] + "…"
    thread_id = msg.get("threadId") or msg.get("thread_id") or msg_id
    web_url = (
        f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    )

    # Prompt format mirrors the github-poller shape: a brief
    # human-readable line the agent can read at a glance, with
    # structured fields in extras for any rendering layer that
    # wants them.
    snippet_line = f"\n  > {snippet}" if snippet else ""
    prompt = (
        f"[gmail] new message from {sender}: {subject!r}"
        f"{snippet_line}\n"
        f"  URL: {web_url}\n"
        f"  message_id: {msg_id}"
    )
    return {
        "poller": POLLER_NAME,
        "prompt": prompt,
        "source_platform": "gmail",
        "message_id": msg_id,
        "thread_id": thread_id,
        "from": sender,
        "subject": subject,
        "snippet": snippet,
        "url": web_url,
    }


def main() -> int:
    account = os.environ.get("GOG_ACCOUNT", "").strip()
    if not account:
        _eprint("gmail-poller: GOG_ACCOUNT not set; exiting")
        return 1

    query = os.environ.get("MIMIR_GMAIL_QUERY", "").strip() or DEFAULT_QUERY
    try:
        max_fetch = int(os.environ.get("MIMIR_GMAIL_MAX_FETCH", "").strip())
    except (TypeError, ValueError):
        max_fetch = DEFAULT_MAX_FETCH
    max_fetch = max(1, min(max_fetch, 200))  # clamp

    cursor = _load_cursor()
    seen = set(cursor)

    try:
        messages = _gog_search(account, query, max_fetch)
    except (subprocess.CalledProcessError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        _eprint(f"gmail-poller: search failed: {exc}")
        return 2

    # Emit new IDs in order. Append-to-cursor in order so LRU eviction
    # preserves the natural "oldest emitted first" semantic.
    new_ids: list[str] = []
    for msg in messages:
        event = _format_event(msg)
        if event is None:
            continue
        mid = event["message_id"]
        if mid in seen:
            continue
        print(json.dumps(event), flush=True)
        new_ids.append(mid)
        seen.add(mid)

    if not new_ids:
        return 0  # silence-as-filter

    # Update cursor: append new, then LRU-evict from the front.
    cursor.extend(new_ids)
    if len(cursor) > CURSOR_MAX_IDS:
        cursor = cursor[-CURSOR_MAX_IDS:]
    _save_cursor(cursor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
