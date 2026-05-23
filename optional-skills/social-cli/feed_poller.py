#!/usr/bin/env python3
"""Bluesky + X timeline poller backed by ``social-cli feed``.

Sibling to ``poller.py`` (the notifications poller). The feed
poller surfaces posts from accounts the operator follows — for
context, awareness, and proactive engagement opportunities — rather
than mentions/replies directed at the agent's handle.

Different surface, different cadence. ``poller.py`` runs every 15
min because mentions need fast turnaround; this one defaults to
every 2h (configured in ``pollers.json``) because timeline volume
is much higher and the agent doesn't owe follows a quick reply.

Per platform, runs ``social-cli feed -p <platform> -n <limit>``,
parses the resulting ``feed-<platform>.yaml``, and emits one JSONL
event per never-before-seen post ID. Cursor lives at
``<STATE_DIR>/emitted.json``.

Working dir: STATE_DIR (typically ``<home>/state/pollers/
social-cli-feed/``). social-cli reads ``.env`` from cwd, so the
operator either copies the notifications poller's ``.env`` here
or symlinks it — credentials are the same.

Environment:
    STATE_DIR              - Persistent state dir (set by framework).
                             Holds the poller's cursor, social-cli's
                             feed-{platform}.yaml outputs, AND the
                             .env with ATPROTO_HANDLE/X_API_KEY/...
                             credentials.
    POLLER_NAME            - This poller's name (default ``social-cli-feed``).
    MIMIR_SOCIAL_PLATFORMS - Optional CSV. Default: "bsky,x".
    MIMIR_SOCIAL_FEED_LIMIT - Optional. Per-platform feed limit.
                             Default 50, clamp 1–200.
    SOCIAL_CLI_BIN         - Optional. Override the social-cli binary
                             path. Default: "social-cli".

Output contract:
    stdout: JSONL — {"poller": str, "prompt": str, ...extras} per event
    stderr: diagnostic logging
    exit 0: success (zero events = nothing new since cursor)
    non-zero: error (framework drops any events emitted by this run)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
CURSOR_FILE = STATE_DIR / "emitted.json"
POLLER_NAME = os.environ.get("POLLER_NAME", "social-cli-feed")

# Cursor cap. Feed traffic is denser than notifications, so we keep
# a larger window — at limit=50/platform × 2 platforms × 12 pulls/day,
# that's ~1200 IDs/day worst case. 3000 IDs ≈ ~2-3 days of busy feed.
CURSOR_MAX_IDS = 3000

# Truncate post text for the prompt. Feed posts can be longer than
# mention notifications, but the framework caps prompts at ~16KB
# total and batch_size for this poller is 10 — keep per-post text
# tight so 10-post batches don't blow the cap.
TEXT_PREVIEW_CHARS = 280


def _eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _load_cursor() -> list[str]:
    if not CURSOR_FILE.exists():
        return []
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _eprint(f"social-cli: feed cursor load failed ({exc}); resetting")
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _save_cursor(ids: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


def _fetch(platform: str, limit: int, bin_path: str) -> Path:
    """Run ``social-cli feed`` for one platform, return path to the
    output YAML.

    social-cli writes the output to ``feed.yaml`` by default. We use
    ``-o feed-<platform>.yaml`` so multiple platforms don't clobber
    each other when the framework runs us serially.
    """
    out_path = STATE_DIR / f"feed-{platform}.yaml"
    cmd = [
        bin_path, "feed",
        "--platform", platform,
        "--limit", str(limit),
        "--output", str(out_path.name),  # relative to cwd
    ]
    proc = subprocess.run(
        cmd, cwd=str(STATE_DIR),
        capture_output=True, text=True, check=False, timeout=45,
    )
    if proc.returncode != 0:
        _eprint(
            f"social-cli feed exit {proc.returncode} (platform={platform}); "
            f"stderr: {proc.stderr.strip()[:500]}"
        )
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr,
        )
    return out_path


def _load_feed(path: Path) -> list[dict]:
    """Parse a feed-{platform}.yaml file. social-cli emits a flat
    top-level list of post dicts (unlike inbox.yaml which wraps in
    ``{notifications: [...]}``). Returns [] if missing or malformed.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]
        data = yaml.safe_load(text) or []
    except ImportError:
        _eprint(
            "social-cli: PyYAML not installed; "
            "install pyyaml in mimir's venv for robust parsing"
        )
        raise
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict)]


def _format_event(post: dict) -> dict | None:
    """Build the JSONL event for one feed post. Returns None when
    the post lacks a stable ID (can't cursor → don't emit).
    """
    pid = post.get("id")
    if not pid:
        return None
    platform = post.get("platform") or "unknown"
    author = post.get("author") or "<unknown>"
    text = (post.get("text") or "").strip()
    if len(text) > TEXT_PREVIEW_CHARS:
        text = text[: TEXT_PREVIEW_CHARS - 1] + "…"
    timestamp = post.get("timestamp") or ""
    likes = post.get("likeCount") or 0
    replies = post.get("replyCount") or 0
    reposts = post.get("repostCount") or 0

    text_line = f"\n  > {text}" if text else ""
    stats = f"likes:{likes} replies:{replies} reposts:{reposts}"
    prompt = (
        f"[{platform}] feed post from {author}"
        f"{text_line}"
        f"\n  id: {pid}"
        f"\n  {stats}"
    )
    out = {
        "poller": POLLER_NAME,
        "prompt": prompt,
        "source_platform": platform,
        "post_id": pid,
        "author": author,
        "text": text,
        "timestamp": timestamp,
        "like_count": likes,
        "reply_count": replies,
        "repost_count": reposts,
    }
    if post.get("authorId"):
        out["author_id"] = post["authorId"]
    return out


def main() -> int:
    platforms_csv = os.environ.get("MIMIR_SOCIAL_PLATFORMS", "bsky,x").strip()
    platforms = [p.strip() for p in platforms_csv.split(",") if p.strip()]
    if not platforms:
        _eprint("social-cli: MIMIR_SOCIAL_PLATFORMS resolves to no platforms; exiting")
        return 1

    try:
        limit = int(os.environ.get("MIMIR_SOCIAL_FEED_LIMIT", "").strip())
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    bin_path = os.environ.get("SOCIAL_CLI_BIN", "").strip() or "social-cli"

    seen = set(_load_cursor())
    # Preserve cursor insertion order so LRU eviction at the head
    # works without metadata. Same shape as the notifications poller.
    ordered: list[str] = _load_cursor()

    emitted_count = 0
    for platform in platforms:
        try:
            feed_path = _fetch(platform, limit, bin_path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _eprint(f"social-cli: feed fetch failed for {platform}: {exc}")
            continue  # other platforms may still succeed

        try:
            posts = _load_feed(feed_path)
        except (OSError, ValueError) as exc:
            _eprint(f"social-cli: feed parse failed for {platform}: {exc}")
            continue

        for post in posts:
            event = _format_event(post)
            if event is None:
                continue
            pid = event["post_id"]
            if pid in seen:
                continue
            print(json.dumps(event))
            seen.add(pid)
            ordered.append(pid)
            emitted_count += 1

    # Trim LRU. Drop oldest IDs once we exceed the cap.
    if len(ordered) > CURSOR_MAX_IDS:
        ordered = ordered[-CURSOR_MAX_IDS:]
    _save_cursor(ordered)

    return 0


if __name__ == "__main__":
    sys.exit(main())
