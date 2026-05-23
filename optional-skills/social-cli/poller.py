#!/usr/bin/env python3
"""Bluesky + X notification poller backed by the `social-cli` tool.

Runs `social-cli sync` (which writes `inbox.yaml` in the working
directory), then walks the parsed YAML and emits one JSONL event
per never-before-seen notification ID.

The poller does NOT call `social-cli dispatch` — that's the agent's
job (decide what to do, write `outbox.yaml`, then dispatch). The
poller's only responsibility is signaling that there's something
new to look at. Re-emit prevention is cursor-side: we track which
notification IDs we've already surfaced, and skip them on subsequent
polls even if they're still pending in inbox.yaml (i.e. the agent
hasn't dispatched yet).

This matters because social-cli's `sync` merges new notifications
into inbox.yaml without removing pending ones — it's a "pending work
queue," not an append-only log. Without our cursor, every poll
would re-emit every un-dispatched mention until the agent finally
handled it, spamming the agent across cron cycles.

Working dir: STATE_DIR. social-cli reads/writes inbox.yaml,
processed-*.yaml, etc. in cwd, so we cd into STATE_DIR before
invoking it. The operator-supplied `.env` (credentials) must live
there too.

Environment:
    STATE_DIR              - Persistent state dir (set by framework).
                             Holds the poller's cursor, social-cli's
                             inbox/processed YAMLs, AND the .env with
                             ATPROTO_HANDLE/X_API_KEY/... credentials.
    POLLER_NAME            - This poller's name.
    MIMIR_SOCIAL_PLATFORMS - Optional CSV. Which platforms to sync.
                             Default: "bsky,x". Set to "bsky" or "x"
                             to scope down.
    MIMIR_SOCIAL_LIMIT     - Optional. Per-platform sync limit. Default 50.
    MIMIR_SOCIAL_USERS_DIR - Optional. Path to user-memory .md files
                             for context enrichment (see social-cli
                             AGENT_GUIDE.md). Default: unset → no
                             userContext on emitted events.
    SOCIAL_CLI_BIN         - Optional. Override the social-cli binary
                             path. Default: "social-cli" (must be on PATH).

Requires social-cli installed and `.env` configured in STATE_DIR.
See SKILL.md install section.

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
POLLER_NAME = os.environ.get("POLLER_NAME", "social-cli-notifications")

# Cursor cap. Notification IDs are larger than Gmail message IDs
# (Bluesky AT URIs run ~80 chars), so 1000 = ~100KB on disk. Covers
# ~10 days of light social traffic at 15-min cadence; busy operators
# may want to bump.
CURSOR_MAX_IDS = 1000

# Truncate notification text for the prompt — social-cli already
# returns the full text, but the framework caps prompts at ~16KB
# total. 300 chars matches the gmail-poller snippet cap so the two
# look uniform in the agent's view.
TEXT_PREVIEW_CHARS = 300


def _eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _load_cursor() -> list[str]:
    """Ordered list of previously-emitted notification IDs.

    Order is insertion-order (oldest first) so LRU eviction at the
    head works without metadata. Same shape as gmail-poller cursor.
    """
    if not CURSOR_FILE.exists():
        return []
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _eprint(f"social-cli: cursor load failed ({exc}); resetting")
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _save_cursor(ids: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


def _sync(platforms: list[str], limit: int, users_dir: str | None,
          bin_path: str) -> None:
    """Run `social-cli sync` in STATE_DIR so inbox.yaml lands there.

    Per-platform flag style: ``--platform bsky --platform x`` (one
    flag per platform — matches social-cli's documented multi-value
    convention). A single CSV `--platform bsky,x` is NOT accepted.
    """
    cmd = [bin_path, "sync"]
    for p in platforms:
        cmd.extend(["--platform", p])
    cmd.extend(["--limit", str(limit)])
    if users_dir:
        cmd.extend(["--users-dir", users_dir])
    proc = subprocess.run(
        cmd, cwd=str(STATE_DIR),
        capture_output=True, text=True, check=False, timeout=45,
    )
    if proc.returncode != 0:
        _eprint(
            f"social-cli sync exit {proc.returncode}; stderr: "
            f"{proc.stderr.strip()[:500]}"
        )
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr,
        )
    # social-cli sync prints a brief summary to stdout; ignore it
    # for the poller's purposes — the data we care about is in
    # inbox.yaml on disk.


def _load_inbox(platforms: list[str]) -> list[dict]:
    """Parse inbox-{platform}.yaml for each configured platform and
    return the concatenated notifications list (empty if no files /
    no items).

    Modern social-cli writes per-platform inbox files
    (``inbox-bsky.yaml``, ``inbox-x.yaml``) rather than a single
    merged ``inbox.yaml``. The `--output` flag is documented but
    silently ignored — the per-platform layout is mandatory. Pre-
    refactor this function looked at ``inbox.yaml`` and emitted
    nothing because that file never gets written.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        _eprint(
            "social-cli: PyYAML not installed; "
            "install pyyaml in mimir's venv for robust parsing"
        )
        raise

    out: list[dict] = []
    for platform in platforms:
        inbox_path = STATE_DIR / f"inbox-{platform}.yaml"
        if not inbox_path.exists():
            continue
        try:
            data = yaml.safe_load(inbox_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _eprint(
                f"social-cli: inbox-{platform}.yaml parse failed: {exc}"
            )
            continue
        notifications = data.get("notifications") if isinstance(data, dict) else None
        if not isinstance(notifications, list):
            continue
        out.extend(n for n in notifications if isinstance(n, dict))
    return out


def _format_event(notif: dict) -> dict | None:
    """Build the JSONL event for one notification. Returns None when
    the notification lacks a stable ID (can't cursor → don't emit).
    """
    nid = notif.get("id")
    if not nid:
        return None
    platform = notif.get("platform") or "unknown"
    ntype = notif.get("type") or "notification"  # mention | reply | follow | like | repost | ...
    author = notif.get("author") or "<unknown>"
    text = (notif.get("text") or "").strip()
    if len(text) > TEXT_PREVIEW_CHARS:
        text = text[: TEXT_PREVIEW_CHARS - 1] + "…"
    # PyYAML parses unquoted ISO timestamps into ``datetime`` objects
    # (social-cli emits them unquoted). JSON can't serialize datetime,
    # so coerce to ISO string here.
    raw_ts = notif.get("timestamp")
    if hasattr(raw_ts, "isoformat"):
        timestamp = raw_ts.isoformat()
    else:
        timestamp = str(raw_ts) if raw_ts else ""
    post_id = notif.get("postId") or notif.get("post_id")
    user_context = (notif.get("userContext") or "").strip()
    user_ctx_block = ""
    if user_context:
        # Truncate user context too — operator-curated memory files
        # can be multi-paragraph; one paragraph is plenty here.
        if len(user_context) > 500:
            user_context = user_context[:499] + "…"
        user_ctx_block = f"\n  context: {user_context}"

    text_line = f"\n  > {text}" if text else ""
    prompt = (
        f"[{platform}] {ntype} from {author}"
        f"{text_line}"
        f"\n  id: {nid}"
        f"{user_ctx_block}"
    )
    out = {
        "poller": POLLER_NAME,
        "prompt": prompt,
        "source_platform": platform,
        "notification_id": nid,
        "notification_type": ntype,
        "author": author,
        "text": text,
        "timestamp": timestamp,
    }
    if post_id:
        out["post_id"] = post_id
    if notif.get("authorId"):
        out["author_id"] = notif["authorId"]
    return out


def main() -> int:
    platforms_csv = os.environ.get("MIMIR_SOCIAL_PLATFORMS", "bsky,x").strip()
    platforms = [p.strip() for p in platforms_csv.split(",") if p.strip()]
    if not platforms:
        _eprint("social-cli: MIMIR_SOCIAL_PLATFORMS resolves to no platforms; exiting")
        return 1

    try:
        limit = int(os.environ.get("MIMIR_SOCIAL_LIMIT", "").strip())
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    users_dir = os.environ.get("MIMIR_SOCIAL_USERS_DIR", "").strip() or None
    bin_path = os.environ.get("SOCIAL_CLI_BIN", "").strip() or "social-cli"

    # Step 1: sync — populates inbox.yaml in STATE_DIR.
    try:
        _sync(platforms, limit, users_dir, bin_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _eprint(f"social-cli: sync failed: {exc}")
        return 2

    # Step 2: parse per-platform inbox files.
    try:
        notifications = _load_inbox(platforms)
    except (ImportError, OSError) as exc:
        _eprint(f"social-cli: inbox parse failed: {exc}")
        return 3

    # Step 3: emit new IDs only.
    cursor = _load_cursor()
    seen = set(cursor)
    new_ids: list[str] = []
    for notif in notifications:
        event = _format_event(notif)
        if event is None:
            continue
        nid = event["notification_id"]
        if nid in seen:
            continue
        print(json.dumps(event), flush=True)
        new_ids.append(nid)
        seen.add(nid)

    if not new_ids:
        return 0

    cursor.extend(new_ids)
    if len(cursor) > CURSOR_MAX_IDS:
        cursor = cursor[-CURSOR_MAX_IDS:]
    _save_cursor(cursor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
