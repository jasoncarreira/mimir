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


# ──────────────────────────────────────────────────────────────────
# Structured config.json (multi-account, per-account prompt routing)
# ──────────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path, accounts: list[dict]) -> None:
    """Drop a ``config.json`` at the STATE_DIR root."""
    (tmp_path / "config.json").write_text(
        json.dumps({"accounts": accounts}), encoding="utf-8",
    )


def _write_prompt(home: Path, name: str, body: str) -> Path:
    """Write a prompt file under ``<home>/prompts/`` and return its path."""
    prompts_dir = home / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    p = prompts_dir / name
    p.write_text(body, encoding="utf-8")
    return p


def test_config_json_multi_account_each_uses_own_prompt(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """Two accounts, two different ``prompt-file`` entries. Each
    account's messages must carry that account's prompt body + the
    account email/name fields in extras."""
    mimir_home = tmp_path / "mimir-home"
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    _write_prompt(mimir_home, "home.md", "HOME ACCOUNT PROMPT")
    _write_prompt(mimir_home, "work.md", "WORK ACCOUNT PROMPT")
    _write_config(tmp_path, [
        {"name": "home", "email": "me@gmail.com", "prompt-file": "home.md"},
        {"name": "work", "email": "me@employer.com", "prompt-file": "work.md"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)

    def fake_search(account, query, max_fetch):
        if account == "me@gmail.com":
            return [_msg("home-msg-1", sender="alice@example.com")]
        if account == "me@employer.com":
            return [_msg("work-msg-1", sender="bob@employer.com")]
        return []

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)
    rc = fresh_poller.main()
    assert rc == 0

    events = _capture_emits(capsys)
    assert len(events) == 2
    by_id = {e["message_id"]: e for e in events}

    # Each account's prompt body is present as instructions, alongside that
    # message's per-item detail (from/subject) — not a replacement for it.
    assert "HOME ACCOUNT PROMPT" in by_id["home-msg-1"]["prompt"]
    assert "alice@example.com" in by_id["home-msg-1"]["prompt"]
    assert by_id["home-msg-1"]["account"] == "me@gmail.com"
    assert by_id["home-msg-1"]["account_name"] == "home"

    assert "WORK ACCOUNT PROMPT" in by_id["work-msg-1"]["prompt"]
    assert "bob@employer.com" in by_id["work-msg-1"]["prompt"]
    assert by_id["work-msg-1"]["account"] == "me@employer.com"
    assert by_id["work-msg-1"]["account_name"] == "work"


def test_config_json_inline_prompt(fresh_poller, tmp_path, monkeypatch, capsys):
    """Inline ``prompt`` field is used verbatim — no file lookup."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "agent", "email": "agent@bot.ai",
         "prompt": "Triage agent-account email."},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda account, q, m: [_msg("m1")] if account == "agent@bot.ai" else [],
    )
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    # Inline prompt is included as instructions, after the per-message detail.
    assert "Triage agent-account email." in events[0]["prompt"]
    assert events[0]["prompt"].startswith("[gmail] new message")


def test_config_json_prompt_file_wins_over_inline(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """When BOTH ``prompt-file`` and ``prompt`` are set, the file wins
    and a warning lands on stderr."""
    mimir_home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    _write_prompt(mimir_home, "wins.md", "FILE WINS")
    _write_config(tmp_path, [
        {"name": "x", "email": "x@y.com",
         "prompt-file": "wins.md",
         "prompt": "this should be overridden"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    # Capture stdout + stderr in one readout — both share the buffer.
    captured = capsys.readouterr()
    events = [json.loads(l) for l in captured.out.splitlines() if l.strip()]
    assert "FILE WINS" in events[0]["prompt"]
    assert "this should be overridden" not in events[0]["prompt"]
    assert "both prompt-file and prompt" in captured.err


def test_config_json_missing_prompt_file_falls_back_to_inline(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """``prompt-file`` points at a nonexistent file but ``prompt`` is
    also set — the inline value takes over."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "x", "email": "x@y.com",
         "prompt-file": "no-such-file.md",
         "prompt": "inline fallback"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert "inline fallback" in events[0]["prompt"]


def test_config_json_no_prompt_fields_uses_default_template(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """An account with NEITHER ``prompt-file`` nor ``prompt`` falls
    back to the built-in default prompt template (the legacy shape)."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "default", "email": "d@e.com"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1", sender="alice@example.com", subject="Hi")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    # Default template includes the "[gmail] new message from" prefix.
    assert events[0]["prompt"].startswith("[gmail] new message from alice@example.com")


def test_config_json_custom_prompt_still_includes_per_message_detail(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """Regression: a custom prompt body must NOT drop the per-message detail.

    Previously ``prompt = account.prompt_body or _default_prompt(...)`` meant a
    configured prompt *replaced* the from/subject/url, so a batch rendered as N
    copies of the instructions with no idea which emails arrived (the fields
    rode along only as event extras, which ``_render_batch`` never renders)."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "agent", "email": "agent@bot.ai", "prompt": "TRIAGE INSTRUCTIONS"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda account, q, m: (
            [_msg("m1", sender="alice@example.com", subject="Invoice")]
            if account == "agent@bot.ai" else []
        ),
    )
    rc = fresh_poller.main()
    assert rc == 0
    prompt = _capture_emits(capsys)[0]["prompt"]
    # Both the per-message detail AND the instructions — detail first.
    assert "alice@example.com" in prompt and "Invoice" in prompt
    assert "TRIAGE INSTRUCTIONS" in prompt
    assert prompt.index("alice@example.com") < prompt.index("TRIAGE INSTRUCTIONS")


def test_config_json_prompt_file_path_traversal_rejected(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """``prompt-file`` containing ``..`` must NOT resolve outside
    ``<MIMIR_HOME>/prompts/`` — falls back to default template +
    warns on stderr."""
    mimir_home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    (mimir_home / "prompts").mkdir(parents=True)
    # Decoy file outside the prompts/ dir that we DO NOT want loaded.
    (mimir_home / "secret.md").write_text("SECRET")
    _write_config(tmp_path, [
        {"name": "x", "email": "x@y.com",
         "prompt-file": "../secret.md"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    captured = capsys.readouterr()
    events = [json.loads(l) for l in captured.out.splitlines() if l.strip()]
    assert "SECRET" not in events[0]["prompt"]
    assert "escapes" in captured.err


def test_config_json_one_account_failure_others_still_emit(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """Per-account gog failure must not kill the whole poll — the other
    accounts still emit, cursor advances for those, exit 0."""
    import subprocess as _sp
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "broken", "email": "broken@x.com",
         "prompt": "broken account prompt"},
        {"name": "working", "email": "working@x.com",
         "prompt": "working account prompt"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)

    def fake_search(account, q, m):
        if account == "broken@x.com":
            raise _sp.CalledProcessError(1, ["gog"], "", "boom")
        return [_msg("ok-1")]

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert len(events) == 1
    assert events[0]["account_name"] == "working"


def test_config_json_all_accounts_failed_returns_2(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """When EVERY account errors AND we emitted nothing, exit 2 so the
    framework drops any partial events."""
    import subprocess as _sp
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "a", "email": "a@x.com", "prompt": "a"},
        {"name": "b", "email": "b@x.com", "prompt": "b"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)

    def fake_search(*_a, **_k):
        raise _sp.CalledProcessError(1, ["gog"], "", "auth")

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)
    rc = fresh_poller.main()
    assert rc == 2
    assert _capture_emits(capsys) == []


def test_config_json_partial_failure_with_empty_inbox_exits_0(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """Regression for the Mimir PR #234 nit: one account errors, the
    OTHER succeeds but returns zero new messages (empty inbox is the
    normal silence-as-filter case).

    Pre-fix this incorrectly returned exit 2 because the condition
    was ``any_account_failed and not new_ids`` — true when EITHER
    side of the AND holds, even though intent was "all accounts
    failed AND nothing emitted." Post-fix uses ``successful_accounts``
    counter so this case correctly exits 0.
    """
    import subprocess as _sp
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "broken", "email": "broken@x.com", "prompt": "p1"},
        {"name": "empty-inbox", "email": "empty@x.com", "prompt": "p2"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)

    def fake_search(account, q, m):
        if account == "broken@x.com":
            raise _sp.CalledProcessError(1, ["gog"], "", "auth")
        # 'empty-inbox' account succeeds but returns no messages.
        return []

    monkeypatch.setattr(fresh_poller, "_gog_search", fake_search)
    rc = fresh_poller.main()
    assert rc == 0, (
        "partial failure with at least one successful (empty) account "
        "should exit 0; pre-fix this was wrongly exit 2"
    )
    assert _capture_emits(capsys) == []


def test_config_json_empty_accounts_list_exits_1(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """config.json with empty accounts list — no usable source."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    rc = fresh_poller.main()
    assert rc == 1


def test_config_json_malformed_does_not_fall_back_to_gog_account(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """A malformed config.json yields an empty accounts list — caller
    sees the file as 'present but unusable' and exits 1 (does NOT
    silently fall back to GOG_ACCOUNT — that would hide an operator
    config error).

    (Previously named ``..._falls_through_to_gog_account`` — the
    body's assertion contradicts that wording; Mimir PR #234 nit.)
    """
    (tmp_path / "config.json").write_text("not valid json {")
    monkeypatch.setenv("GOG_ACCOUNT", "fallback@x.com")
    rc = fresh_poller.main()
    assert rc == 1


def test_cursor_shared_across_accounts(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """Same message id arriving on two accounts emits only once
    (cursor is global). Defends the invariant that gmail message IDs
    are unique across accounts."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    _write_config(tmp_path, [
        {"name": "a", "email": "a@x.com", "prompt": "a"},
        {"name": "b", "email": "b@x.com", "prompt": "b"},
    ])
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    # Both accounts return the SAME message id (shouldn't happen in
    # practice, but defends the dedup contract).
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("shared-id")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert len(events) == 1
    # Cursor has just the one ID.
    cursor = json.loads(fresh_poller.CURSOR_FILE.read_text())
    assert cursor == ["shared-id"]


def test_legacy_gog_account_still_works(
    fresh_poller, tmp_path, monkeypatch, capsys,
):
    """No config.json + GOG_ACCOUNT set → legacy single-account mode.
    The emitted prompt uses the built-in default template; account
    fields point at the legacy account / 'default' name."""
    # config.json absent — fresh_poller fixture's tmp_path has no file.
    monkeypatch.setenv("GOG_ACCOUNT", "legacy@x.com")
    monkeypatch.setattr(
        fresh_poller, "_gog_search",
        lambda *_a, **_k: [_msg("m1", sender="bob@y.com")],
    )
    rc = fresh_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert events[0]["account"] == "legacy@x.com"
    assert events[0]["account_name"] == "default"
    assert events[0]["prompt"].startswith("[gmail] new message from bob@y.com")


def test_seeds_state_gitignore(fresh_poller, tmp_path):
    """Ignores the transient cursor but keeps config.json (operator config) tracked."""
    fresh_poller._seed_state_gitignore()
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    active = [
        ln.strip() for ln in gi.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "cursor.json" in active
    # operator config (config.json) must NOT be an active ignore rule
    assert not any(ln.startswith("config") for ln in active)
    gi.write_text("operator-custom\n")
    fresh_poller._seed_state_gitignore()
    assert gi.read_text() == "operator-custom\n"
