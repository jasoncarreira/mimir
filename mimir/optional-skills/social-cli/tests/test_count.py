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


def _detailed_full(mod, tmp_path, state_dirs=None):
    """The whole LedgerCount."""
    return mod.count_ledgers_detailed(
        platform="bsky", action="post",
        since=mod._parse_dt("2026-06-28"), until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path, state_dirs=state_dirs or [],
    )


def _detailed(mod, tmp_path):
    """``(count, unreadable)`` — the other fields are asserted separately."""
    r = _detailed_full(mod, tmp_path)
    return r.count, r.unreadable


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
    r = _detailed_full(mod, tmp_path, state_dirs=[tmp_path / "absent"])
    assert (r.count, r.unreadable, r.scanned) == (0, 1, 0)


def test_scanned_reports_how_many_ledgers_were_read(tmp_path):
    mod = fresh_count()
    _write_ledger(tmp_path / "social-cli-a" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    _write_ledger(tmp_path / "social-cli-b" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T02:00:00Z"},
    ])
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.unreadable, r.scanned) == (2, 0, 2)


def test_main_json_exposes_scanned(tmp_path, capsys, monkeypatch):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    _write_ledger(tmp_path / "social-cli-a" / "sent_ledger-bsky.yaml", [
        {"action": "post", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
    ])
    mod.main(["--platform", "bsky", "--since", "2026-06-28",
              "--state-root", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["scanned"] == 1


# --------------------------------------------------------------------------
# Record-level interpretability.
#
# The distinction that bounds this: a record we *chose* not to count is a
# normal skip, and flagging those would make every `like` look like damage. A
# record we *could not read* is different — if the action, timestamp or
# platform is missing or uninterpretable, it may have been today's post.
# --------------------------------------------------------------------------


def _one_record(tmp_path, record):
    _write_ledger(tmp_path / "social-cli-notifications" / "sent_ledger-bsky.yaml", [record])


def test_a_record_without_an_action_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 1)
    assert r.is_floor


def test_a_record_without_a_timestamp_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "bsky"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 1)


def test_a_record_with_an_uninterpretable_timestamp_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "bsky", "timestamp": "not-a-date"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 1)


def test_a_record_without_a_platform_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 1)


def test_a_like_is_a_normal_skip_not_damage(tmp_path):
    """Bounds the rule: filtering by action must not read as damage.

    Without this, every non-post record in a real ledger — 3681 likes in the
    deployed data — would make the count a floor and the guard would never
    establish headroom again.
    """
    mod = fresh_count()
    _one_record(tmp_path, {"action": "like", "platform": "bsky",
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 0)
    assert not r.is_floor


def test_another_platform_is_a_normal_skip_not_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "mastodon",
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 0)


def test_a_timestamp_outside_the_window_is_a_normal_skip_not_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "bsky",
                           "timestamp": "2020-01-01T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 0)


def test_a_dry_run_is_a_normal_skip_not_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "bsky", "dryRun": True,
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 0)


def test_a_datetime_timestamp_is_interpretable(tmp_path):
    """The production form. YAML parses an unquoted ISO timestamp to datetime.

    Every deployed record arrives this way, so a validator that type-checked
    for `str` would mark all 543 of them damaged and block posting outright.
    """
    import datetime as _dt

    mod = fresh_count()
    _one_record(tmp_path, {
        "action": "post", "platform": "bsky",
        "timestamp": _dt.datetime(2026, 6, 28, 1, 0, 0, tzinfo=_dt.timezone.utc),
    })
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (1, 0)


def test_the_platforms_list_form_is_interpretable(tmp_path):
    """`_platform_matches` also accepts a `platforms` list or CSV."""
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platforms": ["bsky", "mastodon"],
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (1, 0)


def test_main_exits_nonzero_on_an_uninterpretable_record(tmp_path, capsys, monkeypatch):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    _one_record(tmp_path, {"platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"})
    rc = mod.main(["--platform", "bsky", "--since", "2026-06-28",
                   "--state-root", str(tmp_path), "--json"])
    assert rc == mod.EXIT_LEDGER_UNREADABLE
    assert json.loads(capsys.readouterr().out)["damagedRecords"] == 1


def test_main_json_reports_state_dirs(tmp_path, capsys, monkeypatch):
    mod = fresh_count()
    monkeypatch.delenv("STATE_DIR", raising=False)
    _one_record(tmp_path, {"action": "post", "platform": "bsky",
                           "timestamp": "2026-06-28T01:00:00Z"})
    mod.main(["--platform", "bsky", "--since", "2026-06-28",
              "--state-root", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["stateDirs"] == 1


# --------------------------------------------------------------------------
# Malformed-present fields.
#
# Presence was not enough. `_platform_matches` stringifies whatever it finds,
# so `platform: []` compares as the literal "[]" and the record is filed as
# another platform's traffic; and any non-empty action string was accepted, so
# a corrupted spelling read as a legitimate non-post. Both silently withhold a
# record that may be today's only post.
# --------------------------------------------------------------------------


def test_an_empty_platform_string_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": "",
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_a_list_valued_platform_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": [],
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_a_mapping_valued_platform_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platform": {"name": "bsky"},
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_a_mapping_valued_platforms_container_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platforms": {"bsky": True},
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_an_empty_platforms_list_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platforms": [],
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_a_platforms_list_holding_a_non_name_is_damage(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platforms": ["bsky", {}],
                           "timestamp": "2026-06-28T01:00:00Z"})
    assert _detailed_full(mod, tmp_path).damaged_records == 1


def test_a_comma_separated_platforms_string_is_interpretable(tmp_path):
    mod = fresh_count()
    _one_record(tmp_path, {"action": "post", "platforms": "bsky, mastodon",
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (1, 0)


def test_an_unknown_action_is_damage(tmp_path):
    """A corrupted spelling is indistinguishable from a new upstream verb.

    Either way it cannot be classified, so the count becomes a floor rather
    than quietly treating the record as a non-post that does not count.
    """
    mod = fresh_count()
    _one_record(tmp_path, {"action": "pst", "platform": "bsky",
                           "timestamp": "2026-06-28T01:00:00Z"})
    r = _detailed_full(mod, tmp_path)
    assert (r.count, r.damaged_records) == (0, 1)
    assert r.is_floor


def test_every_action_seen_in_deployed_data_is_known(tmp_path):
    """Guards the cost of the vocabulary check against real traffic.

    These are the verbs present in the deployed ledgers and outbox archives.
    If the vocabulary ever stops covering them, every such record becomes
    damage and the guard reports an unestablished count for good.
    """
    mod = fresh_count()
    for action in ("post", "reply", "thread", "like", "repost", "ignore", "follow", "quote"):
        assert action in mod.KNOWN_ACTIONS, action


def test_known_non_post_actions_stay_normal_skips(tmp_path):
    mod = fresh_count()
    for action in ("like", "repost", "ignore", "follow"):
        _one_record(tmp_path, {"action": action, "platform": "bsky",
                               "timestamp": "2026-06-28T01:00:00Z"})
        r = _detailed_full(mod, tmp_path)
        assert (r.count, r.damaged_records) == (0, 0), action
