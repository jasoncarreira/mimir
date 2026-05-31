"""Tests for the social-cli-poller.

Mocks ``_sync`` (the social-cli invocation) and writes a synthetic
``inbox.yaml`` directly into STATE_DIR for ``_load_inbox`` to read.
This exercises the real YAML parse path while keeping social-cli
itself out of the test surface.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fresh_poller(tmp_path: Path, monkeypatch):
    """Re-import per test so STATE_DIR module constant picks up tmp_path."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("POLLER_NAME", "social-cli-notifications")
    monkeypatch.delenv("MIMIR_SOCIAL_PLATFORMS", raising=False)
    monkeypatch.delenv("MIMIR_SOCIAL_LIMIT", raising=False)
    monkeypatch.delenv("MIMIR_SOCIAL_USERS_DIR", raising=False)
    monkeypatch.delenv("SOCIAL_CLI_BIN", raising=False)

    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _write_inbox(state_dir: Path, notifications: list[dict],
                 platforms: list[str] | None = None) -> None:
    """Write synthetic per-platform inbox-{platform}.yaml files in
    social-cli's exact shape.

    If ``platforms`` is unset, the notifications are bucketed by their
    ``platform`` field (default: all in ``inbox-bsky.yaml`` for older
    tests that don't set MIMIR_SOCIAL_PLATFORMS).
    """
    import yaml
    state_dir.mkdir(parents=True, exist_ok=True)
    by_platform: dict[str, list[dict]] = {}
    for n in notifications:
        p = n.get("platform") or "bsky"
        by_platform.setdefault(p, []).append(n)
    # If platforms were specified but empty for one, still write the
    # file so the test sees an empty-platform read path.
    for p in (platforms or list(by_platform.keys()) or ["bsky"]):
        body = yaml.safe_dump({
            "notifications": by_platform.get(p, []),
            "_sync": {"timestamp": "2026-03-25T12:00:00Z"},
        })
        (state_dir / f"inbox-{p}.yaml").write_text(body)


def _notif(nid: str, platform="bsky", ntype="mention",
           author="alice.bsky.social", text="hello @mimir") -> dict:
    return {
        "id": nid,
        "platform": platform,
        "type": ntype,
        "author": author,
        "authorId": f"did:plc:{nid}",
        "text": text,
        "timestamp": "2026-03-25T12:00:00Z",
        "postId": nid,
    }


def _capture_emits(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_first_run_empty_cursor_emits_all(fresh_poller, monkeypatch, capsys, tmp_path):
    _write_inbox(tmp_path, [_notif("n1"), _notif("n2"), _notif("n3")])
    monkeypatch.setattr(
        fresh_poller, "_sync", lambda *a, **k: None,
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["notification_id"] for e in events] == ["n1", "n2", "n3"]

    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert cursor == ["n1", "n2", "n3"]


def test_existing_cursor_skips_seen_ids(fresh_poller, monkeypatch, capsys, tmp_path):
    fresh_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fresh_poller.CURSOR_FILE.write_text(json.dumps(["n1", "n2"]))

    _write_inbox(tmp_path, [_notif("n1"), _notif("n2"), _notif("n3"), _notif("n4")])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["notification_id"] for e in events] == ["n3", "n4"]

    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert cursor == ["n1", "n2", "n3", "n4"]


def test_empty_inbox_emits_nothing(fresh_poller, monkeypatch, capsys, tmp_path):
    _write_inbox(tmp_path, [])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    rc = fresh_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_inbox_file_missing_emits_nothing(fresh_poller, monkeypatch, capsys):
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    rc = fresh_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_sync_failure_returns_2(fresh_poller, monkeypatch, capsys):
    import subprocess

    def _boom(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["social-cli"], "", "auth")

    monkeypatch.setattr(fresh_poller, "_sync", _boom)
    rc = fresh_poller.main()
    assert rc == 2
    assert not fresh_poller.CURSOR_FILE.exists()


def test_unprocessed_notifications_not_re_emitted(fresh_poller, monkeypatch, capsys, tmp_path):
    """The CORE poller value-prop: social-cli's inbox merges new
    notifications WITHOUT removing un-dispatched ones, so without our
    cursor every poll would re-emit pending mentions. Cursor stops
    that across polls."""
    # First poll: emit 2 notifications.
    _write_inbox(tmp_path, [_notif("n1"), _notif("n2")])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)
    rc = fresh_poller.main()
    assert rc == 0
    first_events = _capture_emits(capsys)
    assert len(first_events) == 2

    # Second poll: same inbox.yaml (agent hasn't dispatched yet).
    # Cursor should suppress the re-emit.
    rc = fresh_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_platforms_csv_parsed_and_passed(fresh_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_inbox(tmp_path, [])
    captured = {}

    def fake_sync(platforms, limit, users_dir, bin_path):
        captured.update({
            "platforms": platforms, "limit": limit,
            "users_dir": users_dir, "bin": bin_path,
        })

    monkeypatch.setattr(fresh_poller, "_sync", fake_sync)
    rc = fresh_poller.main()
    assert rc == 0
    assert captured["platforms"] == ["bsky"]
    assert captured["limit"] == 50  # default
    assert captured["users_dir"] is None
    assert captured["bin"] == "social-cli"


def test_users_dir_passed_through(fresh_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_USERS_DIR", "/etc/users")
    monkeypatch.setenv("SOCIAL_CLI_BIN", "/opt/bin/social-cli")
    _write_inbox(tmp_path, [])
    captured = {}

    monkeypatch.setattr(
        fresh_poller, "_sync",
        lambda platforms, limit, users_dir, bin_path: captured.update({
            "users_dir": users_dir, "bin": bin_path,
        }),
    )
    fresh_poller.main()
    assert captured["users_dir"] == "/etc/users"
    assert captured["bin"] == "/opt/bin/social-cli"


def test_event_shape_includes_required_fields(fresh_poller, monkeypatch, capsys, tmp_path):
    _write_inbox(tmp_path, [_notif(
        "at://did:plc:xxx/app.bsky.feed.post/abc",
        platform="bsky", ntype="mention",
        author="alice.bsky.social",
        text="What do you think about this approach?",
    )])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    assert len(events) == 1
    ev = events[0]
    assert ev["poller"] == "social-cli-notifications"
    assert ev["source_platform"] == "bsky"
    assert ev["notification_type"] == "mention"
    assert ev["author"] == "alice.bsky.social"
    assert ev["text"] == "What do you think about this approach?"
    assert "at://did:plc:xxx" in ev["notification_id"]
    assert "bsky" in ev["prompt"]
    assert "mention" in ev["prompt"]


def test_notifications_without_id_skipped(fresh_poller, monkeypatch, capsys, tmp_path):
    _write_inbox(tmp_path, [
        {"platform": "bsky", "type": "mention", "text": "no id"},  # missing id
        _notif("good1"),
    ])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    assert [e["notification_id"] for e in events] == ["good1"]


def test_user_context_truncated_and_emitted(fresh_poller, monkeypatch, capsys, tmp_path):
    long_ctx = "x" * 800
    notif = _notif("n1")
    notif["userContext"] = long_ctx
    _write_inbox(tmp_path, [notif])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    assert "context:" in events[0]["prompt"]
    # Truncated to 500 + ellipsis.
    assert "…" in events[0]["prompt"]


def test_datetime_timestamp_serialized(fresh_poller, monkeypatch, capsys, tmp_path):
    """social-cli emits unquoted ISO timestamps; PyYAML parses them as
    datetime objects. Pre-fix this crashed json.dumps. Now we coerce to
    ISO string in _format_event."""
    from datetime import datetime, timezone
    notif = _notif("n1")
    notif["timestamp"] = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    _write_inbox(tmp_path, [notif])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    rc = fresh_poller.main()
    assert rc == 0  # didn't crash
    events = _capture_emits(capsys)
    assert events[0]["timestamp"] == "2026-05-23T12:00:00+00:00"


def test_cursor_lru_caps_at_max(fresh_poller, monkeypatch, capsys, tmp_path):
    fresh_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    cap = fresh_poller.CURSOR_MAX_IDS
    seed = [f"old{i}" for i in range(cap)]
    fresh_poller.CURSOR_FILE.write_text(json.dumps(seed))

    _write_inbox(tmp_path, [_notif(f"new{i}") for i in range(3)])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert len(cursor) == cap
    assert cursor[0] == "old3"
    assert cursor[-3:] == ["new0", "new1", "new2"]


def test_empty_platforms_returns_1(fresh_poller, monkeypatch, capsys):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "")
    rc = fresh_poller.main()
    assert rc == 1


# ---------------------------------------------------------------------------
# Thread-context coverage (PR #395 — surfacing threadContext from inbox)
# ---------------------------------------------------------------------------


def test_event_shape_includes_thread_fields_when_absent(
    fresh_poller, monkeypatch, capsys, tmp_path
):
    """Events without threadContext carry thread_depth=0 and
    agent_replies_in_thread=0 as explicit keys (not missing)."""
    _write_inbox(tmp_path, [_notif("n1")])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    ev = events[0]
    assert ev["thread_depth"] == 0
    assert ev["agent_replies_in_thread"] == 0
    # No thread block injected into prompt
    assert "thread (" not in ev["prompt"]


def test_thread_context_rendered_in_prompt(fresh_poller, monkeypatch, capsys, tmp_path):
    """When threadContext is populated the rendered block appears in the
    prompt, and thread_depth + agent_replies_in_thread are set correctly."""
    notif = _notif("n1", ntype="reply")
    notif["threadContext"] = [
        {"author": "alice.bsky.social", "text": "original question"},
        {"author": "bob.bsky.social", "text": "bob's response"},
    ]
    _write_inbox(tmp_path, [notif])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    ev = events[0]
    assert ev["thread_depth"] == 2
    assert ev["agent_replies_in_thread"] == 0
    assert "thread (2 prior posts)" in ev["prompt"]
    assert "@alice.bsky.social: original question" in ev["prompt"]
    assert "@bob.bsky.social: bob" in ev["prompt"]


def test_thread_context_marks_own_replies(fresh_poller, monkeypatch, capsys, tmp_path):
    """Ancestor entries authored by the agent's own handle get a (you)
    marker and increment agent_replies_in_thread."""
    env_path = tmp_path / ".env"
    env_path.write_text("ATPROTO_HANDLE=mimir.bsky.social\n")

    notif = _notif("n1", ntype="reply")
    notif["threadContext"] = [
        {"author": "alice.bsky.social", "text": "first"},
        {"author": "mimir.bsky.social", "text": "my reply"},
        {"author": "alice.bsky.social", "text": "her response"},
    ]
    _write_inbox(tmp_path, [notif])
    monkeypatch.setattr(fresh_poller, "_sync", lambda *a, **k: None)

    fresh_poller.main()
    events = _capture_emits(capsys)
    ev = events[0]
    assert ev["thread_depth"] == 3
    assert ev["agent_replies_in_thread"] == 1
    assert "(you)" in ev["prompt"]
    assert "1 from you" in ev["prompt"]


def test_format_thread_context_empty_input(fresh_poller):
    """Empty / None threadContext yields zeros and empty block."""
    depth, replies, block = fresh_poller._format_thread_context([], "")
    assert (depth, replies, block) == (0, 0, "")

    depth, replies, block = fresh_poller._format_thread_context(None, "")
    assert (depth, replies, block) == (0, 0, "")


def test_format_thread_context_malformed_entries_skipped(fresh_poller):
    """Non-dict entries (strings, None) are skipped; dict entries with a
    missing author key are kept with author defaulting to '?'."""
    ctx = [
        "not-a-dict",          # skipped — not a dict
        None,                  # skipped — not a dict
        {"text": "no author key"},        # kept — author defaults to "?"
        {"author": "good.bsky.social", "text": "fine"},  # kept
    ]
    depth, replies, block = fresh_poller._format_thread_context(ctx, "")
    # Two dict entries survive; non-dicts are filtered out
    assert depth == 2
    assert "@good.bsky.social: fine" in block
    # Missing-author entry rendered with fallback "?"
    assert "@?: no author key" in block


def test_format_thread_context_text_truncation(fresh_poller):
    """Ancestor text longer than THREAD_CTX_PER_LINE_CHARS gets truncated."""
    long_text = "a" * 300
    ctx = [{"author": "x.bsky.social", "text": long_text}]
    _, _, block = fresh_poller._format_thread_context(ctx, "")
    line = [ln for ln in block.splitlines() if "@x.bsky.social" in ln][0]
    assert "…" in line
    assert len(line) < 250


def test_own_handle_read_from_env_file(fresh_poller, monkeypatch, tmp_path):
    """_own_handle_for reads ATPROTO_HANDLE from STATE_DIR/.env, lowercased."""
    env_path = tmp_path / ".env"
    env_path.write_text("ATPROTO_HANDLE=MyAgent.bsky.Social\n")
    monkeypatch.setattr(fresh_poller, "STATE_DIR", tmp_path)
    fresh_poller._OWN_HANDLE_CACHE.clear()

    handle = fresh_poller._own_handle_for("bsky")
    assert handle == "myagent.bsky.social"


def test_own_handle_missing_env_returns_empty(fresh_poller, monkeypatch, tmp_path):
    """Missing .env degrades gracefully — own_handle is empty string."""
    monkeypatch.setattr(fresh_poller, "STATE_DIR", tmp_path)
    fresh_poller._OWN_HANDLE_CACHE.clear()

    handle = fresh_poller._own_handle_for("bsky")
    assert handle == ""
