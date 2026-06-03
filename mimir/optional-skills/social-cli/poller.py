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

# Per-ancestor cap in the rendered thread context. social-cli writes
# the full text of every ancestor (no upstream truncation); 5 ancestors
# at ~600 chars each blow past the 16KB prompt budget by themselves.
# 160 keeps the gist of each post intact while staying within batch
# budget (3 events × 5 ancestors × 160 = ~2.4KB).
THREAD_CTX_PER_LINE_CHARS = 160

# Bluesky handle of the agent itself, lifted from STATE_DIR/.env's
# ATPROTO_HANDLE when present. Used to mark "you" entries in the
# threadContext block and compute ``agent_replies_in_thread``.
# Looked up lazily per-platform and memoized for the poller run.
_OWN_HANDLE_CACHE: dict[str, str] = {}


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


def _own_handle_for(platform: str) -> str:
    """Return the agent's own handle on ``platform`` (lowercase).

    Reads ``STATE_DIR/.env`` once per platform per poller run.
    Returns empty string when the env file is missing, malformed, or
    doesn't carry the platform-specific key — in which case the
    ``(you)`` annotation in thread context just won't fire (degrades
    to the pre-feature behavior).
    """
    if platform in _OWN_HANDLE_CACHE:
        return _OWN_HANDLE_CACHE[platform]
    env_key = {"bsky": "ATPROTO_HANDLE"}.get(platform)
    # x.ts doesn't populate threadContext yet, so there's no X case
    # to surface; revisit when it does. Other platforms similarly.
    if not env_key:
        _OWN_HANDLE_CACHE[platform] = ""
        return ""
    env_path = STATE_DIR / ".env"
    val = ""
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{env_key}="):
                    val = line[len(env_key) + 1 :].strip().strip("'\"").lower()
                    break
        except OSError:
            val = ""
    _OWN_HANDLE_CACHE[platform] = val
    return val


def _format_thread_context(
    thread_context: list, own_handle: str,
) -> tuple[int, int, str]:
    """Render the threadContext array into a prompt block.

    Returns ``(depth, agent_replies, block)`` where:
        depth         — number of well-formed ancestor entries.
        agent_replies — count of those entries whose author matches
                        ``own_handle`` (case-insensitive). Zero when
                        ``own_handle`` is empty.
        block         — newline-prefixed string suitable for inline
                        injection into the prompt; empty if no
                        ancestors.

    Each rendered line: ``@<author>[ (you)]: <text>`` with text capped
    at ``THREAD_CTX_PER_LINE_CHARS``. Whitespace inside post text is
    collapsed to single spaces so multi-paragraph ancestors don't
    bleed across the rendered block.
    """
    if not isinstance(thread_context, list) or not thread_context:
        return (0, 0, "")
    lines: list[str] = []
    agent_replies = 0
    own_lower = own_handle.lower()
    for entry in thread_context:
        if not isinstance(entry, dict):
            continue
        author = str(entry.get("author") or "?")
        text = " ".join((str(entry.get("text") or "")).split())
        if len(text) > THREAD_CTX_PER_LINE_CHARS:
            text = text[: THREAD_CTX_PER_LINE_CHARS - 1] + "…"
        is_self = bool(own_lower) and author.lower() == own_lower
        if is_self:
            agent_replies += 1
            lines.append(f"    @{author} (you): {text}")
        else:
            lines.append(f"    @{author}: {text}")
    if not lines:
        return (0, 0, "")
    header = f"  thread ({len(lines)} prior post{'s' if len(lines) != 1 else ''}"
    if agent_replies > 0:
        header += f", {agent_replies} from you"
    header += "):"
    block = "\n" + header + "\n" + "\n".join(lines)
    return (len(lines), agent_replies, block)


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

    # Thread depth + agent's own prior contributions in this thread.
    # social-cli already does the upstream fetch (``getPostThread``
    # with ``parentHeight: 5``) and writes the result as
    # ``threadContext: [{author, text}, ...]`` on each notification.
    # Surfacing the count and the rendered chain here is what gives
    # the agent the structural feedback to apply the "don't rabbit-
    # hole past N replies" rule — without it, every reply turn sees
    # only the current notification and re-justifies engagement.
    thread_depth, agent_replies, thread_block = _format_thread_context(
        notif.get("threadContext") or [], _own_handle_for(platform),
    )

    text_line = f"\n  > {text}" if text else ""
    # Action hint: identifies the right tool (outbox + dispatch) for any
    # response the agent makes to this notification. The bare event
    # ("mention from X / > text / id: ...") doesn't tell the agent
    # WHERE to send the reply, so the default reach is ``send_message``
    # — which routes to Discord/Slack, NOT back to Bluesky/X. Caught
    # by muninn-mimir 2026-05-23. Per-event overhead ~250 chars;
    # bounded by ``batch_size`` (default 3 for notifications).
    #
    # For ``like`` / ``follow`` / ``repost`` notifications (where the
    # user is signaling, not asking), the agent may decide no
    # outbound is needed — the hint still applies if it DOES decide
    # to acknowledge.
    target_id = post_id or nid
    action_hint = (
        "\n\n→ To reply or react: append to <STATE_DIR>/outbox.yaml + "
        "run `social-cli dispatch`.\n"
        "  Minimal shape:\n"
        "    dispatch:\n"
        f"      - reply: {{ platform: {platform}, id: \"{target_id}\", text: \"...\" }}\n"
        f"      - like:  {{ platform: {platform}, id: \"{target_id}\" }}\n"
        f"      - ignore: {{ id: \"{nid}\", reason: \"...\" }}   # skip without action\n"
        f"  send_message routes to Discord/Slack — NOT to {platform}. Use outbox."
    )
    prompt = (
        f"[{platform}] {ntype} from {author}"
        f"{text_line}"
        f"{thread_block}"
        f"\n  id: {nid}"
        f"{user_ctx_block}"
        f"{action_hint}"
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
        "thread_depth": thread_depth,
        "agent_replies_in_thread": agent_replies,
    }
    if post_id:
        out["post_id"] = post_id
    if notif.get("authorId"):
        out["author_id"] = notif["authorId"]
    return out


_STATE_GITIGNORE = """\
# Transient social-cli poller state — seeded by the social-cli skill
# (write-if-missing; edit freely). git reads per-directory .gitignore natively.
# Ignore the high-churn / per-run / secret-bearing files; the home allowlist
# still tracks the DURABLE state in this dir: session-*.md (audit trail),
# sent_ledger-*/processed-* (dedup ledgers), and config.*.
outbox_archive/
feed-*.yaml
inbox-*.yaml
outbox.yaml
dispatch_result-*.yaml
*-new.yaml
cursor.json
emitted.json
.env
*.sh
*.tmp
"""


def _seed_state_gitignore() -> None:
    """Seed STATE_DIR/.gitignore (only if absent) so the poller's transient
    working files + per-run archives + inline-cred scripts aren't committed,
    while session logs / ledgers / config stay tracked. Best-effort; never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gi = STATE_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text(_STATE_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    _seed_state_gitignore()
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
