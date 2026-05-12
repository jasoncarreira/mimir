"""Tests for the gmail-poller.

Mocks ``_gog_search`` to return canned message lists and runs ``main()``
end-to-end. Captures stdout via ``capsys`` to verify the JSONL contract
and inspects the cursor file on disk to verify dedup / LRU semantics.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fresh_poller(tmp_path: Path, monkeypatch):
    """Import a fresh ``poller`` module each test so STATE_DIR /
    CURSOR_FILE module-level constants pick up the temp directory.

    Without re-import, the FIRST test's STATE_DIR would stick for the
    rest of the suite — Python caches modules in ``sys.modules``.
    """
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("POLLER_NAME", "gmail-inbox")
    monkeypatch.setenv("GOG_ACCOUNT", "test@example.com")
    monkeypatch.delenv("MIMIR_GMAIL_QUERY", raising=False)
    monkeypatch.delenv("MIMIR_GMAIL_MAX_FETCH", raising=False)

    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _msg(msg_id: str, sender="alice@example.com", subject="Hello",
         snippet="body preview", thread_id=None) -> dict:
    return {
        "id": msg_id,
        "from": sender,
        "subject": subject,
        "snippet": snippet,
        "threadId": thread_id or msg_id,
    }


def _capture_emits(capsys) -> list[dict]:
    """Parse stdout-as-JSONL captured by pytest's capsys fixture."""
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_first_run_empty_cursor_emits_all(fresh_poller, monkeypatch, capsys):
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda account, query, max_fetch: [_msg("m1"), _msg("m2"), _msg("m3")],
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["message_id"] for e in events] == ["m1", "m2", "m3"]

    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert cursor == ["m1", "m2", "m3"]


def test_existing_cursor_skips_seen_ids(fresh_poller, monkeypatch, capsys):
    fresh_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fresh_poller.CURSOR_FILE.write_text(json.dumps(["m1", "m2"]))

    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1"), _msg("m2"), _msg("m3"), _msg("m4")],
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["message_id"] for e in events] == ["m3", "m4"]

    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert cursor == ["m1", "m2", "m3", "m4"]


def test_no_new_messages_emits_nothing(fresh_poller, monkeypatch, capsys):
    fresh_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fresh_poller.CURSOR_FILE.write_text(json.dumps(["m1"]))

    monkeypatch.setattr(
        fresh_poller, "_gog_search", lambda *_a, **_k: [_msg("m1")],
    )

    rc = fresh_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_missing_account_returns_1(fresh_poller, monkeypatch, capsys):
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    rc = fresh_poller.main()
    assert rc == 1
    assert _capture_emits(capsys) == []


def test_search_failure_returns_2_no_events(fresh_poller, monkeypatch, capsys):
    """A failed gog invocation must NOT emit partial events. Framework
    treats non-zero exit as 'drop all events from this run.'"""
    import subprocess

    def _boom(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["gog"], "", "auth expired")

    monkeypatch.setattr(fresh_poller, "_gog_search", _boom)
    rc = fresh_poller.main()
    assert rc == 2
    # Cursor untouched on failure.
    assert not fresh_poller.CURSOR_FILE.exists()


def test_cursor_lru_caps_at_max(fresh_poller, monkeypatch, capsys):
    """Cursor never grows past CURSOR_MAX_IDS — oldest IDs drop."""
    fresh_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Seed cursor at the cap with synthetic IDs.
    cap = fresh_poller.CURSOR_MAX_IDS
    seed = [f"old{i}" for i in range(cap)]
    fresh_poller.CURSOR_FILE.write_text(json.dumps(seed))

    # New batch of 5 messages; cursor should shed the 5 oldest.
    new = [_msg(f"new{i}") for i in range(5)]
    monkeypatch.setattr(fresh_poller, "_gog_search", lambda *_a, **_k: new)

    rc = fresh_poller.main()
    assert rc == 0
    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert len(cursor) == cap
    # First 5 of seed should have been evicted.
    assert cursor[0] == "old5"
    assert cursor[-5:] == ["new0", "new1", "new2", "new3", "new4"]


def test_event_shape_includes_required_fields(fresh_poller, monkeypatch, capsys):
    """Each emitted event must have ``poller``, ``prompt`` (framework
    requires), plus the structured ``source_platform`` / ``message_id``
    / ``url`` extras callers downstream rely on."""
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg(
            "abc123",
            sender="Jason <jason@example.com>",
            subject="Re: PR review",
            snippet="Looked over your changes",
            thread_id="thread9",
        )],
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert len(events) == 1
    ev = events[0]
    assert ev["poller"] == "gmail-inbox"
    assert ev["source_platform"] == "gmail"
    assert ev["message_id"] == "abc123"
    assert ev["thread_id"] == "thread9"
    assert ev["from"] == "Jason <jason@example.com>"
    assert ev["subject"] == "Re: PR review"
    assert ev["snippet"] == "Looked over your changes"
    assert "thread9" in ev["url"]
    assert "jason@example.com" in ev["prompt"]
    assert "PR review" in ev["prompt"]


def test_messages_without_id_silently_skipped(fresh_poller, monkeypatch, capsys):
    """A malformed message with no ``id`` cannot be cursored — skip
    it rather than emit an un-deduplicable event."""
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [
            {"from": "x", "subject": "no-id-here"},  # missing id
            _msg("good1"),
        ],
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["message_id"] for e in events] == ["good1"]


def test_long_snippet_truncated(fresh_poller, monkeypatch, capsys):
    long_snippet = "x" * 500
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1", snippet=long_snippet)],
    )

    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert events[0]["snippet"].endswith("…")
    assert len(events[0]["snippet"]) <= fresh_poller.SNIPPET_PREVIEW_CHARS


def test_query_override_passed_to_gog(fresh_poller, monkeypatch, capsys):
    monkeypatch.setenv("MIMIR_GMAIL_QUERY", "is:unread label:starred")
    captured = {}

    def fake_search(account, query, max_fetch):
        captured.update({"account": account, "query": query, "max": max_fetch})
        return []

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)

    rc = fresh_poller.main()
    assert rc == 0
    assert captured["account"] == "test@example.com"
    assert captured["query"] == "is:unread label:starred"
    assert captured["max"] == fresh_poller.DEFAULT_MAX_FETCH


def test_max_fetch_clamped(fresh_poller, monkeypatch, capsys):
    monkeypatch.setenv("MIMIR_GMAIL_MAX_FETCH", "9999")
    captured = {}

    def fake_search(account, query, max_fetch):
        captured["max"] = max_fetch
        return []

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)
    fresh_poller.main()
    assert captured["max"] == 200  # upper clamp
