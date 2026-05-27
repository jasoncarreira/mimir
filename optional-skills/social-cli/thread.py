#!/usr/bin/env python3
"""Fetch the full thread around a Bluesky post URI.

The notifications poller's surfacing (via ``social-cli sync``) is
capped at ``parentHeight=5`` per Bluesky's ``getPostThread`` call.
That's enough for the agent to apply the rabbit-hole guard (count
its own contributions in the visible ancestors), but insufficient
when the conversation has deeper history or sibling replies that
the operator wants the agent to read explicitly.

This script reuses the credentials the operator already configured
for ``social-cli`` (``STATE_DIR/.env`` with ``ATPROTO_HANDLE`` +
``ATPROTO_APP_PASSWORD`` + optional ``ATPROTO_PDS``) and calls the
AT proto XRPC endpoint directly — no extra Python deps beyond
stdlib + the optional ``pyyaml`` already used by ``poller.py``.

Usage:
  python3 thread.py <uri> [--parent-height N] [--depth N] [--json]

Args:
  uri              AT URI of the focus post
                   (``at://did:plc:.../app.bsky.feed.post/...``).
  --parent-height  Ancestors to walk up. Default 20 (vs sync's 5).
  --depth          Reply tree depth to flatten. Default 5.
  --json           Emit JSON instead of YAML.

Requires STATE_DIR/.env with ATPROTO_HANDLE + ATPROTO_APP_PASSWORD.
STATE_DIR defaults to the script's own directory; the poller frame-
work normally sets it to
``<home>/state/pollers/social-cli-notifications/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))

# Default PDS — bsky.social hosts most accounts. Operators on a
# self-hosted PDS override via ``ATPROTO_PDS`` in ``.env``.
DEFAULT_PDS = "https://bsky.social"

# Cap on rendered text per post to keep big threads from blowing
# the prompt budget when the agent pipes this through Read. 240 is
# 50% more than the poller's THREAD_CTX_PER_LINE_CHARS — caller
# explicitly asked for depth, so spend the tokens.
TEXT_PREVIEW_CHARS = 240


def _eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _load_env() -> dict[str, str]:
    """Parse ``STATE_DIR/.env`` into a flat dict (no inheritance,
    no shell expansion). Mirrors ``poller._own_handle_for`` parsing.
    """
    env: dict[str, str] = {}
    env_path = STATE_DIR / ".env"
    if not env_path.exists():
        return env
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    except OSError as exc:
        _eprint(f"thread: .env read failed: {exc}")
    return env


def _xrpc_post(pds: str, method: str, body: dict) -> dict:
    """POST to an XRPC endpoint with JSON body."""
    url = f"{pds}/xrpc/{method}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _xrpc_get(pds: str, method: str, params: dict, jwt: str) -> dict:
    """GET an XRPC endpoint with bearer auth."""
    query = urllib.parse.urlencode(params)
    url = f"{pds}/xrpc/{method}?{query}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {jwt}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _flatten_post(node: dict) -> dict:
    """Extract the agent-relevant fields from a thread node's
    ``.post``. Missing fields collapse to empty strings rather than
    raising — Bluesky occasionally returns "notFoundPost" /
    "blockedPost" nodes for deleted or blocked content.
    """
    if not isinstance(node, dict):
        return {}
    post = node.get("post") or {}
    if not isinstance(post, dict):
        return {}
    author = post.get("author") or {}
    record = post.get("record") or {}
    text = str(record.get("text") or "").strip()
    if len(text) > TEXT_PREVIEW_CHARS:
        text = text[: TEXT_PREVIEW_CHARS - 1] + "…"
    return {
        "uri": str(post.get("uri") or ""),
        "author": str(author.get("handle") or ""),
        "authorId": str(author.get("did") or "") or None,
        "text": text,
        "timestamp": str(post.get("indexedAt") or record.get("createdAt") or ""),
    }


def _walk_ancestors(focus_node: dict) -> list[dict]:
    """Walk parent links from ``focus_node`` upward, return list
    ordered **oldest first** (i.e. the root of the visible chain is
    index 0, the focus post's direct parent is the last entry).
    """
    out: list[dict] = []
    curr = focus_node.get("parent") if isinstance(focus_node, dict) else None
    while isinstance(curr, dict):
        flat = _flatten_post(curr)
        if flat.get("uri"):
            out.append(flat)
        curr = curr.get("parent")
    out.reverse()
    return out


def _walk_replies(focus_node: dict, max_depth: int) -> list[dict]:
    """Flatten the reply tree depth-first, annotating each entry
    with its ``depth`` (1 = direct reply, 2 = reply-to-a-reply, ...).
    """
    out: list[dict] = []

    def recurse(node: dict, depth: int) -> None:
        if depth > max_depth:
            return
        replies = node.get("replies") or []
        if not isinstance(replies, list):
            return
        for r in replies:
            if not isinstance(r, dict):
                continue
            flat = _flatten_post(r)
            if flat.get("uri"):
                flat["depth"] = depth
                out.append(flat)
                recurse(r, depth + 1)

    if isinstance(focus_node, dict):
        recurse(focus_node, 1)
    return out


def _dump(result: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps(result, indent=2, ensure_ascii=False)
    try:
        import yaml  # type: ignore[import-untyped]
        return yaml.safe_dump(
            result, sort_keys=False, allow_unicode=True, width=120,
        )
    except ImportError:
        return json.dumps(result, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "uri", help="AT URI of the focus post",
    )
    parser.add_argument(
        "--parent-height", type=int, default=20,
        help="Ancestors to walk up (default 20; sync uses 5).",
    )
    parser.add_argument(
        "--depth", type=int, default=5,
        help="Reply-tree depth to flatten (default 5).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of YAML.",
    )
    args = parser.parse_args()

    env = _load_env()
    handle = env.get("ATPROTO_HANDLE", "")
    password = env.get("ATPROTO_APP_PASSWORD", "")
    pds = env.get("ATPROTO_PDS") or DEFAULT_PDS
    if not handle or not password:
        _eprint(
            "thread: ATPROTO_HANDLE / ATPROTO_APP_PASSWORD missing in "
            f"{STATE_DIR}/.env"
        )
        return 1

    # parentHeight + depth are 0-100 per the spec; clamp so a typo
    # like ``--parent-height 9999`` doesn't 400 the request.
    parent_height = max(0, min(args.parent_height, 100))
    reply_depth = max(0, min(args.depth, 100))

    try:
        session = _xrpc_post(
            pds, "com.atproto.server.createSession",
            {"identifier": handle, "password": password},
        )
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200].decode("utf-8", "replace")
        _eprint(f"thread: auth failed ({exc.code}): {body}")
        return 2
    except (urllib.error.URLError, OSError) as exc:
        _eprint(f"thread: network error during auth: {exc}")
        return 2

    jwt = session.get("accessJwt")
    if not jwt:
        _eprint("thread: auth response missing accessJwt")
        return 2

    try:
        thread = _xrpc_get(
            pds, "app.bsky.feed.getPostThread",
            {"uri": args.uri,
             "parentHeight": parent_height, "depth": reply_depth},
            jwt,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200].decode("utf-8", "replace")
        _eprint(f"thread: getPostThread failed ({exc.code}): {body}")
        return 3
    except (urllib.error.URLError, OSError) as exc:
        _eprint(f"thread: network error during fetch: {exc}")
        return 3

    focus_node = thread.get("thread") or {}
    focus = _flatten_post(focus_node)
    ancestors = _walk_ancestors(focus_node)
    replies = _walk_replies(focus_node, reply_depth)

    own_handle = handle.lower()
    agent_replies_above = sum(
        1 for a in ancestors if str(a.get("author") or "").lower() == own_handle
    )

    result = {
        "focusUri": focus.get("uri") or args.uri,
        "focus": focus,
        "ancestors": ancestors,
        "replies": replies,
        "_meta": {
            "parentHeight": parent_height,
            "depth": reply_depth,
            "ancestorCount": len(ancestors),
            "replyCount": len(replies),
            "agentRepliesInAncestors": agent_replies_above,
            "ownHandle": handle,
        },
    }

    sys.stdout.write(_dump(result, args.json))
    if not _dump(result, args.json).endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
