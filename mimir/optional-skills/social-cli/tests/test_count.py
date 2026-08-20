from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def fresh_count():
    sys.modules.pop("count", None)
    return importlib.import_module("count")


def _write_ledger(path: Path, entries: list[dict]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")


def test_counts_post_creating_actions_and_excludes_non_posts(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T02:00:00Z"},
        {"action": "thread", "platform": "bsky", "timestamp": "2026-06-28T02:30:00Z", "textHash": "thread-1"},
        {"action": "like", "platform": "bsky", "timestamp": "2026-06-28T03:00:00Z"},
        {"action": "repost", "platform": "bsky", "timestamp": "2026-06-28T04:00:00Z"},
        {"action": "ignore", "platform": "bsky", "timestamp": "2026-06-28T05:00:00Z"},
    ])

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 3


def test_counts_thread_as_one_post_creating_ledger_entry(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {
            "action": "thread",
            "platform": "bsky",
            "timestamp": "2026-06-28T12:00:00Z",
            "textHash": "hash-of-whole-thread",
        },
    ])

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 1


def test_excludes_mixed_dates_and_dry_runs(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-27T23:59:59Z"},
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T00:00:00Z"},
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T12:00:00Z", "dryRun": True},
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-29T00:00:00Z"},
    ])

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 1


def test_aggregates_across_multiple_poller_ledgers(tmp_path):
    mod = fresh_count()
    _write_ledger(tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    _write_ledger(tmp_path / "social-cli-feed" / "sent_ledger-bsky.yaml", [
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T02:00:00Z"},
        {"action": "post", "platform": "x", "timestamp": "2026-06-28T03:00:00Z"},
    ])

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 2


def test_missing_and_empty_ledgers_return_zero(tmp_path):
    mod = fresh_count()
    (tmp_path / "social-cli-feed").mkdir()
    (tmp_path / "social-cli-feed" / "sent_ledger-bsky.yaml").write_text("", encoding="utf-8")

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 0


def test_cli_prints_number_and_compact_json(tmp_path, capsys):
    mod = fresh_count()
    _write_ledger(tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platforms": ["bsky", "x"], "timestamp": "2026-06-28T01:00:00Z"},
    ])

    rc = mod.main([
        "--platform", "bsky",
        "--action", "post",
        "--since", "2026-06-28",
        "--until", "2026-06-29",
        "--state-root", str(tmp_path),
    ])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "1"

    rc = mod.main([
        "--platform", "bsky",
        "--action", "post",
        "--since", "2026-06-28",
        "--until", "2026-06-29",
        "--state-root", str(tmp_path),
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["platform"] == "bsky"


def test_cli_today_window_ends_at_next_utc_midnight(tmp_path, capsys, monkeypatch):
    from datetime import datetime, timezone

    mod = fresh_count()
    monkeypatch.setattr(
        mod,
        "_today_utc",
        lambda: datetime(2026, 6, 28, tzinfo=timezone.utc),
    )
    _write_ledger(tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T23:59:59Z"},
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-29T00:00:00Z"},
    ])

    rc = mod.main([
        "--platform", "bsky",
        "--action", "post",
        "--since", "today",
        "--state-root", str(tmp_path),
    ])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "1"


# --------------------------------------------------------------------------
# Unreadable ledgers: the count becomes a floor, and callers must be told.
#
# _load_yaml previously returned None for both "empty" and "would not parse",
# so a damaged ledger was silently worth zero posts and count.py exited 0 with
# a plausible integer. A cap check built on that cannot tell a quiet day from
# an unreadable record of a busy one.
# --------------------------------------------------------------------------


def test_detailed_reports_unreadable_ledger_files(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir(parents=True)
    (poller / "sent_ledger-bsky.yaml").write_text("{[ not: valid: yaml", encoding="utf-8")

    assert _detailed(mod, tmp_path) == (0, 1)


def test_detailed_does_not_flag_an_empty_ledger_as_unreadable(tmp_path):
    """An empty ledger is a real zero; only damage makes the count a floor."""
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir(parents=True)
    (poller / "sent_ledger-bsky.yaml").write_text("", encoding="utf-8")

    assert _detailed(mod, tmp_path) == (0, 0)


def test_detailed_counts_the_readable_ledger_alongside_a_damaged_one(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    (poller / "sent_ledger-bsky-two.yaml").write_text("{[ malformed", encoding="utf-8")

    assert _detailed(mod, tmp_path) == (1, 1)


def test_count_ledgers_still_returns_a_bare_total(tmp_path):
    """The int-returning entry point stays as it was for existing callers."""
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    total = mod.count_ledgers(
        platform="bsky", action="post",
        since=mod._parse_dt("2026-06-28"), until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path, state_dirs=[],
    )
    assert total == 1
    assert isinstance(total, int)


def test_main_exits_nonzero_when_a_ledger_is_unreadable(tmp_path, capsys, monkeypatch):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir(parents=True)
    (poller / "sent_ledger-bsky.yaml").write_text("{[ malformed", encoding="utf-8")

    rc = mod.main(["--platform", "bsky", "--since", "2026-06-28",
                   "--state-root", str(tmp_path)])
    assert rc == mod.EXIT_LEDGER_UNREADABLE
    assert rc != 0
    # stdout keeps its shape so plain-text consumers are unchanged
    assert capsys.readouterr().out.strip() == "0"


def test_main_json_exposes_the_unreadable_count(tmp_path, capsys, monkeypatch):
    """Structured, so a caller need not scrape warning text off stderr."""
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir(parents=True)
    (poller / "sent_ledger-bsky.yaml").write_text("{[ malformed", encoding="utf-8")

    mod.main(["--platform", "bsky", "--since", "2026-06-28",
              "--state-root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["unreadable"] == 1
    assert payload["count"] == 0


def test_main_exits_zero_and_reports_no_damage_on_a_clean_ledger(
    tmp_path, capsys, monkeypatch
):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    rc = mod.main(["--platform", "bsky", "--since", "2026-06-28",
                   "--state-root", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["unreadable"] == 0


# --------------------------------------------------------------------------
# Structurally invalid but syntactically valid ledgers.
#
# yaml.safe_load succeeds on a bare scalar and on any mapping, so a ledger can
# parse cleanly and still be uninterpretable. _records() then yields nothing,
# which is indistinguishable from an empty ledger — and the file may hold the
# only record of a post. The live format nests under `entries:`, so a mistyped
# container is the realistic version of this, not an exotic one: were the
# upstream shape to change, every file would silently read as zero.
# --------------------------------------------------------------------------


def _detailed(mod, tmp_path):
    """``(count, unreadable)`` — `scanned` is asserted separately below."""
    count, unreadable, _scanned = mod.count_ledgers_detailed(
        platform="bsky", action="post",
        since=mod._parse_dt("2026-06-28"), until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path, state_dirs=[],
    )
    return count, unreadable


def _detailed_full(mod, tmp_path):
    return mod.count_ledgers_detailed(
        platform="bsky", action="post",
        since=mod._parse_dt("2026-06-28"), until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path, state_dirs=[],
    )


def _write_raw(tmp_path: Path, text: str) -> None:
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir(parents=True, exist_ok=True)
    (poller / "sent_ledger-bsky.yaml").write_text(text, encoding="utf-8")


def test_a_bare_scalar_ledger_is_damaged(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "garbage\n")
    assert _detailed(mod, tmp_path) == (0, 1)


def test_a_mistyped_entries_container_is_damaged(tmp_path):
    """`entries: {}` parses fine and yields nothing — the reported case."""
    mod = fresh_count()
    _write_raw(tmp_path, "entries: {}\n")
    assert _detailed(mod, tmp_path) == (0, 1)


def test_an_unknown_top_level_mapping_is_damaged(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "unexpected:\n  - action: post\n")
    assert _detailed(mod, tmp_path) == (0, 1)


def test_a_list_holding_a_non_record_is_damaged(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "- action: post\n  platform: bsky\n  timestamp: 2026-06-28T01:00:00Z\n- just-a-string\n")
    count, unreadable = _detailed(mod, tmp_path)
    assert unreadable == 1


def test_an_empty_list_ledger_is_a_real_zero(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "[]\n")
    assert _detailed(mod, tmp_path) == (0, 0)


def test_an_empty_entries_container_is_a_real_zero(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "entries: []\n")
    assert _detailed(mod, tmp_path) == (0, 0)


def test_an_explicit_null_ledger_is_a_real_zero(tmp_path):
    mod = fresh_count()
    _write_raw(tmp_path, "null\n")
    assert _detailed(mod, tmp_path) == (0, 0)


def test_a_single_record_mapping_is_recognized(tmp_path):
    mod = fresh_count()
    _write_raw(
        tmp_path,
        "action: post\nplatform: bsky\ntimestamp: 2026-06-28T01:00:00Z\n",
    )
    assert _detailed(mod, tmp_path) == (1, 0)


def test_the_production_ledger_shape_is_recognized_and_counted(tmp_path):
    """Pins the live format so tightening this validator cannot reject it.

    Taken from the deployed `sent_ledger-bsky.yaml`: a mapping whose only key
    is `entries`, holding records with this field set. Rejecting this shape
    would report every real post as unreadable and block posting outright,
    which is a worse failure than the one the validation prevents.
    """
    mod = fresh_count()
    _write_ledger(tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml", [])
    (tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml").write_text(
        yaml_dump_entries(), encoding="utf-8"
    )
    assert _detailed(mod, tmp_path) == (1, 0)


def yaml_dump_entries() -> str:
    import yaml

    return yaml.safe_dump({
        "entries": [{
            "key": "reply:bsky:at://did:plc:x/app.bsky.feed.post/abc:hash",
            "action": "reply",
            "platform": "bsky",
            "targetId": "at://did:plc:x/app.bsky.feed.post/abc",
            "textHash": "hash",
            "createdId": "at://did:plc:y/app.bsky.feed.post/def",
            "timestamp": "2026-06-28T01:00:00.000Z",
            "cwd": "/mimir-home/state/pollers/social-cli-notifications",
            "outboxPath": "/mimir-home/state/pollers/social-cli-notifications/outbox-bsky.yaml",
            "inboxPath": "/mimir-home/state/pollers/social-cli-notifications/inbox-bsky.yaml",
            "dispatchTimestamp": "2026-06-28T00:59:58.000Z",
            "dryRun": False,
        }],
    })


# --------------------------------------------------------------------------
# Missing state directories: absence of evidence, not evidence of absence.
# --------------------------------------------------------------------------


def test_a_state_root_that_does_not_exist_is_damage(tmp_path):
    """Counting zero from a directory that isn't there is a wrong-place read."""
    mod = fresh_count()
    count, unreadable = _detailed(mod, tmp_path / "nowhere")
    assert (count, unreadable) == (0, 1)


def test_a_present_state_root_with_no_ledger_yet_is_not_damage(tmp_path):
    """What a fresh install looks like before the poller first runs."""
    mod = fresh_count()
    assert _detailed(mod, tmp_path) == (0, 0)


def test_an_explicitly_named_missing_state_dir_is_damage(tmp_path):
    mod = fresh_count()
    count, unreadable, scanned = mod.count_ledgers_detailed(
        platform="bsky", action="post",
        since=mod._parse_dt("2026-06-28"), until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path, state_dirs=[tmp_path / "absent"],
    )
    assert (count, unreadable, scanned) == (0, 1, 0)


def test_scanned_reports_how_many_ledgers_were_read(tmp_path):
    mod = fresh_count()
    _write_ledger(tmp_path / "social-cli-a" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    _write_ledger(tmp_path / "social-cli-b" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T02:00:00Z"},
    ])
    count, unreadable, scanned = _detailed_full(mod, tmp_path)
    assert (count, unreadable, scanned) == (2, 0, 2)


def test_main_json_exposes_scanned(tmp_path, capsys, monkeypatch):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    _write_ledger(tmp_path / "social-cli-a" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    mod.main(["--platform", "bsky", "--since", "2026-06-28",
              "--state-root", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["scanned"] == 1
