#!/usr/bin/env python3
"""Gmail inbox poller backed by the `gog` CLI (Google Workspace).

Watches `in:inbox` (or a caller-supplied search) across one or more
Gmail accounts and emits one JSONL event per never-before-seen
message ID to stdout. Each account can supply its own prompt
(inline or via file under ``<home>/prompts/``) so the agent can
route handling per account (work vs personal vs agent-only).

Cursor is a SET of message IDs, not a timestamp — Gmail can deliver
out of order (server-side spam filters re-route, late-arriving items
backdate), so a timestamp cursor would either miss or double-emit.
A rolling set keyed on the stable Gmail message ID is correct
regardless of order. Cursor is shared across accounts because Gmail
message IDs are globally unique.

Cursor size cap: 500 IDs (LRU evict — oldest emission first). 5
minutes between polls × 500 IDs covers ~40 hours of busy email; a
weeklong vacation backlog flushes through naturally.

──────────────────────────────────────────────────────────────────
Configuration (operator-edited)
──────────────────────────────────────────────────────────────────

Two modes, picked at startup:

  1. STRUCTURED (preferred). Drop a ``config.json`` next to this
     poller at ``<STATE_DIR>/config.json`` with:

       {
         "accounts": [
           {
             "name":        "home",                  // friendly label
             "email":       "you@gmail.com",          // gog --account
             "prompt-file": "email-home.md"           // <home>/prompts/<file>
           },
           {
             "name":   "work",
             "email":  "you@employer.com",
             "prompt": "Inline prompt body for work email triage..."
           }
         ]
       }

     Each entry needs ``name`` + ``email``. Prompt resolution per
     account is precedence-ordered: ``prompt-file`` (resolved
     against ``<MIMIR_HOME>/prompts/``) > inline ``prompt`` >
     built-in default template. If both ``prompt-file`` and
     ``prompt`` are present, the file wins and a warning is logged.
     A missing ``prompt-file`` falls through to ``prompt`` if set
     else to the default — never crashes the poll.

  2. LEGACY (single account, no config.json). Reads ``GOG_ACCOUNT``
     env var, uses the built-in default prompt template. Preserved
     for back-compat with earlier installs.

Environment:
    STATE_DIR              - Persistent cursor dir + config.json
                             location (set by framework)
    POLLER_NAME            - This poller's name
    MIMIR_HOME             - Agent home root, used to resolve
                             ``prompt-file`` paths under
                             ``<MIMIR_HOME>/prompts/``
    GOG_ACCOUNT            - Single-account mode only. Required
                             when config.json is absent.
    MIMIR_GMAIL_QUERY      - Optional. Gmail search query override.
                             Default: "in:inbox newer_than:1d".
                             Applies to every configured account.
    MIMIR_GMAIL_MAX_FETCH  - Optional. Per-account fetch cap.
                             Default: 50.

Requires `gog` binary (https://gogcli.sh) installed and authed for
every account listed in config.json. See SKILL.md install section.

Output contract:
    stdout: JSONL — {"poller": str, "prompt": str, ...extras} per event
            (extras include ``account`` + ``account_name``)
    stderr: diagnostic logging
    exit 0: success (zero events = silence-as-filter, nothing new)
    non-zero: error (framework drops any events emitted by this run)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
CURSOR_FILE = STATE_DIR / "cursor.json"
CONFIG_FILE = STATE_DIR / "config.json"
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


@dataclass(frozen=True)
class Account:
    """One Gmail account configured for this poller.

    ``prompt_body`` is the resolved prompt string (file-loaded, inline,
    or default fallback). Resolution happens once at config load so
    the per-message hot path is just a dict lookup.
    """
    name: str
    email: str
    prompt_body: str


def _resolve_prompt(
    entry: dict,
    *,
    mimir_home: Path | None,
    account_label: str,
) -> str | None:
    """Resolve one account entry's prompt body, with the precedence:
    ``prompt-file`` > inline ``prompt`` > None (caller uses default).

    Returns ``None`` when neither field is set OR when ``prompt-file``
    points at a missing/unreadable file AND no fallback inline
    ``prompt`` is supplied. Callers fall through to the default
    template in that case.

    ``account_label`` is the account's ``name`` field — used only in
    log lines so operator can grep "prompt-file missing for <name>".
    """
    fname = entry.get("prompt-file")
    inline = entry.get("prompt")
    if fname and inline:
        _eprint(
            f"gmail-poller: account {account_label!r} has both "
            f"prompt-file and prompt; prompt-file wins."
        )
    if fname:
        if not isinstance(fname, str) or not fname.strip():
            _eprint(
                f"gmail-poller: account {account_label!r} prompt-file "
                f"must be a non-empty string."
            )
        elif mimir_home is None:
            _eprint(
                f"gmail-poller: account {account_label!r} requests "
                f"prompt-file {fname!r} but MIMIR_HOME is unset; "
                f"cannot resolve."
            )
        else:
            # Reject path-traversal — keep all loads under
            # <MIMIR_HOME>/prompts/. The framework's scheduler
            # applies the same containment for its own prompt-file
            # field; we mirror.
            prompts_root = (mimir_home / "prompts").resolve()
            try:
                target = (prompts_root / fname).resolve()
                target.relative_to(prompts_root)  # raises if escape
            except (OSError, ValueError):
                _eprint(
                    f"gmail-poller: account {account_label!r} prompt-file "
                    f"{fname!r} escapes <home>/prompts/; ignoring."
                )
            else:
                if target.is_file():
                    try:
                        return target.read_text(encoding="utf-8").strip()
                    except OSError as exc:
                        _eprint(
                            f"gmail-poller: account {account_label!r} "
                            f"prompt-file read failed ({exc}); falling back."
                        )
                else:
                    _eprint(
                        f"gmail-poller: account {account_label!r} "
                        f"prompt-file {fname!r} missing at {target}; "
                        f"falling back."
                    )
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    return None


def _load_accounts() -> list[Account] | None:
    """Read ``<STATE_DIR>/config.json`` and return resolved accounts.

    Returns ``None`` when config.json doesn't exist (caller falls back
    to single-account GOG_ACCOUNT mode). Returns ``[]`` when the file
    exists but is malformed / empty — distinct from "no config" so the
    caller can decide whether to error or continue.

    Schema:
        {"accounts": [{"name": str, "email": str,
                        "prompt-file"?: str, "prompt"?: str}, ...]}
    """
    if not CONFIG_FILE.is_file():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _eprint(f"gmail-poller: config.json load failed ({exc})")
        return []

    if not isinstance(data, dict):
        _eprint("gmail-poller: config.json must be an object")
        return []
    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list):
        _eprint("gmail-poller: config.json.accounts must be a list")
        return []

    mimir_home_env = os.environ.get("MIMIR_HOME", "").strip()
    mimir_home = Path(mimir_home_env) if mimir_home_env else None

    accounts: list[Account] = []
    seen_emails: set[str] = set()
    for i, entry in enumerate(raw_accounts):
        if not isinstance(entry, dict):
            _eprint(f"gmail-poller: accounts[{i}] is not an object; skipping")
            continue
        name = (entry.get("name") or "").strip()
        email = (entry.get("email") or "").strip()
        if not name or not email:
            _eprint(
                f"gmail-poller: accounts[{i}] missing name or email; skipping"
            )
            continue
        if email in seen_emails:
            _eprint(
                f"gmail-poller: accounts[{i}] duplicate email {email!r}; "
                f"first entry wins."
            )
            continue
        seen_emails.add(email)
        prompt_body = _resolve_prompt(entry, mimir_home=mimir_home, account_label=name)
        accounts.append(
            Account(name=name, email=email, prompt_body=prompt_body or "")
        )
    return accounts


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


def _default_prompt(sender: str, subject: str, snippet: str, web_url: str, msg_id: str) -> str:
    """Built-in fallback prompt — the original gmail-poller format,
    used when an account has no ``prompt-file`` / ``prompt`` configured
    (or when in legacy single-account mode)."""
    snippet_line = f"\n  > {snippet}" if snippet else ""
    return (
        f"[gmail] new message from {sender}: {subject!r}"
        f"{snippet_line}\n"
        f"  URL: {web_url}\n"
        f"  message_id: {msg_id}"
    )


def _format_event(msg: dict, account: Account) -> dict | None:
    """Build the JSONL event for one Gmail message scoped to ``account``.

    Returns None if the message lacks an ``id`` (we have nothing to
    cursor on — safer to skip than emit an un-deduplicable event).

    The emitted ``prompt`` is the account's resolved prompt body when
    one was supplied (per-account file or inline), else the built-in
    default template.
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

    # The per-message detail block (from / subject / snippet / URL /
    # message_id) is ALWAYS included so the agent sees what each email is.
    # A custom account prompt body is triage *instructions* — it augments,
    # it does not replace, the detail. (Chronic bug: ``prompt =
    # account.prompt_body or _default_prompt(...)`` meant a configured
    # prompt_body dropped the per-message fields from the prompt body — they
    # rode along only as event extras, which ``_render_batch`` never renders,
    # so a batch showed N copies of the account instructions with no idea
    # which emails arrived.)
    detail = _default_prompt(sender, subject, snippet, web_url, msg_id)
    prompt = f"{detail}\n\n{account.prompt_body}" if account.prompt_body else detail
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
        "account": account.email,
        "account_name": account.name,
    }


def _resolve_accounts_or_exit() -> list[Account] | None:
    """Pick the source of accounts: structured ``config.json`` if
    present, else legacy single-account ``GOG_ACCOUNT`` env.

    Returns ``None`` (with a stderr message) if NEITHER is usable —
    main() treats that as an exit-1 misconfiguration.
    """
    accounts = _load_accounts()
    if accounts is not None:
        if not accounts:
            _eprint(
                "gmail-poller: config.json present but yielded no usable "
                "accounts; exiting"
            )
            return None
        return accounts

    legacy_account = os.environ.get("GOG_ACCOUNT", "").strip()
    if legacy_account:
        return [
            Account(
                name="default", email=legacy_account, prompt_body="",
            ),
        ]
    _eprint(
        "gmail-poller: neither config.json nor GOG_ACCOUNT is set; "
        "exiting"
    )
    return None


_STATE_GITIGNORE = """\
# Transient gmail-poller state — seeded by the gmail-poller skill
# (write-if-missing; edit freely). The timestamp cursor churns every poll;
# config.json (operator account config) is intentionally NOT ignored so it
# stays tracked via the home allowlist.
cursor.json
*.tmp
"""


def _seed_state_gitignore() -> None:
    """Seed STATE_DIR/.gitignore (only if absent) so the poller's transient
    cursor isn't committed to the home repo, while config.json stays tracked.
    Best-effort; never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gi = STATE_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text(_STATE_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    _seed_state_gitignore()
    accounts = _resolve_accounts_or_exit()
    if accounts is None:
        return 1

    query = os.environ.get("MIMIR_GMAIL_QUERY", "").strip() or DEFAULT_QUERY
    try:
        max_fetch = int(os.environ.get("MIMIR_GMAIL_MAX_FETCH", "").strip())
    except (TypeError, ValueError):
        max_fetch = DEFAULT_MAX_FETCH
    max_fetch = max(1, min(max_fetch, 200))  # clamp

    cursor = _load_cursor()
    seen = set(cursor)

    # Iterate per account so each one's resolved prompt + label stamps
    # onto its own messages. A failure on one account doesn't sink the
    # whole poll — log + continue so the remaining accounts still emit.
    # Track success/failure counts independently of ``new_ids`` so an
    # all-empty-inbox-but-one-account-failed run doesn't get
    # mis-classified as a catastrophic failure (Mimir's PR #234 nit).
    new_ids: list[str] = []
    successful_accounts = 0
    failed_accounts = 0
    for account in accounts:
        try:
            messages = _gog_search(account.email, query, max_fetch)
        except (subprocess.CalledProcessError, json.JSONDecodeError,
                subprocess.TimeoutExpired) as exc:
            _eprint(
                f"gmail-poller: search failed for account "
                f"{account.name!r} ({account.email}): {exc}"
            )
            failed_accounts += 1
            continue
        successful_accounts += 1

        for msg in messages:
            event = _format_event(msg, account)
            if event is None:
                continue
            mid = event["message_id"]
            if mid in seen:
                continue
            print(json.dumps(event), flush=True)
            new_ids.append(mid)
            seen.add(mid)

    if new_ids:
        # Update cursor: append new, then LRU-evict from the front.
        cursor.extend(new_ids)
        if len(cursor) > CURSOR_MAX_IDS:
            cursor = cursor[-CURSOR_MAX_IDS:]
        _save_cursor(cursor)

    # Exit code: 0 when at least one account's search succeeded — empty
    # inbox is a normal silence-as-filter result, not a failure, and a
    # partial failure where another account succeeded is still useful
    # (events from the surviving accounts get emitted; the failure was
    # reported on stderr). Only exit 2 when EVERY account's search
    # errored — at that point the run produced no signal at all and
    # the framework should treat it as a catastrophic poll.
    if failed_accounts > 0 and successful_accounts == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
