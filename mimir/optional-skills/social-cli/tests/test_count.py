from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def fresh_count():
    sys.modules.pop("count", None)
    return importlib.import_module("count")


def _write_ledger(path: Path, entries: list[dict]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")


def _archive(poller: Path, posts: list[str], stamp: str = "2026-06-28T02-29-59-000Z") -> None:
    import yaml

    folder = poller / "outbox_archive"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stamp}_outbox-bsky.yaml").write_text(
        yaml.safe_dump({"dispatch": [{"thread": {"platform": "bsky", "posts": posts}}]}),
        encoding="utf-8",
    )


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
    _archive(poller, ["first", "second", "third"])

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 5


def test_counts_thread_per_published_post(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {
            "action": "thread",
            "platform": "bsky",
            "timestamp": "2026-06-28T12:00:00Z",
            "dispatchTimestamp": "2026-06-28T12:00:02Z",
            "textHash": "hash-of-whole-thread",
        },
    ])
    _archive(poller, ["one", "two", "three"], "2026-06-28T11-59-59-000Z")

    total = mod.count_ledgers(
        platform="bsky",
        action="post",
        since=mod._parse_dt("2026-06-28"),
        until=mod._parse_dt("2026-06-29"),
        state_root=tmp_path,
        state_dirs=[],
    )

    assert total == 3


def test_nested_thread_ledger_resolves_poller_outbox_archive(tmp_path):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / ".social-cli/state/sent_ledger-bsky.yaml", [
        {"action": "thread", "platform": "bsky", "timestamp": "2026-06-28T02:30:00Z",
         "createdId": "thread-nested"},
    ])
    _archive(poller, ["one", "two", "three"])
    assert mod.count_ledgers(platform="bsky", action="post", since=mod._parse_dt("2026-06-28"),
                             until=mod._parse_dt("2026-06-29"), state_root=tmp_path, state_dirs=[]) == 3


def test_unrelated_state_parent_cannot_supply_thread_archive(tmp_path):
    mod = fresh_count()
    custom = tmp_path / "other/state"
    _write_ledger(custom / "sent_ledger-bsky.yaml", [
        {"action": "thread", "platform": "bsky", "timestamp": "2026-06-28T02:30:00Z",
         "createdId": "thread-custom"},
    ])
    _archive(tmp_path, ["one", "two", "three"])
    with pytest.raises(mod.UnresolvedThreadError, match="thread-custom"):
        mod.count_ledgers(platform="bsky", action="post", since=mod._parse_dt("2026-06-28"),
                          until=mod._parse_dt("2026-06-29"), state_root=tmp_path,
                          state_dirs=[custom])


def test_unresolved_thread_fails_closed_in_library_and_cli(tmp_path, capsys):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z"},
        {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T01:30:00Z"},
        {"action": "thread", "platform": "bsky", "timestamp": "2026-06-28T02:30:00Z", "createdId": "thread-abc"},
    ])
    args = dict(platform="bsky", action="post", since=mod._parse_dt("2026-06-28"),
                until=mod._parse_dt("2026-06-29"), state_root=tmp_path, state_dirs=[])
    with pytest.raises(mod.UnresolvedThreadError, match="thread-abc"):
        mod.count_ledgers(**args)
    rc = mod.main(["--platform", "bsky", "--since", "2026-06-28",
                   "--until", "2026-06-29", "--state-root", str(tmp_path)])
    output = capsys.readouterr()
    assert rc == mod.EXIT_CAP_UNKNOWN == 3
    assert output.out == ""
    assert "CAP UNKNOWN" in output.err and "thread-abc" in output.err


@pytest.mark.parametrize("stamp, archive_platform", [
    ("2026-06-28T02-20-00-000Z", "bsky"),
    ("2026-06-28T02-29-59-000Z", "x"),
])
def test_unrelated_archive_does_not_resolve_thread(tmp_path, stamp, archive_platform):
    import yaml

    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    _write_ledger(poller / "sent_ledger-bsky.yaml", [
        {"action": "thread", "platform": "bsky", "timestamp": "2026-06-28T02:30:00Z",
         "createdId": "unresolved-thread"},
    ])
    _archive(poller, ["one", "two", "three"], stamp)
    archive = next((poller / "outbox_archive").iterdir())
    if archive_platform == "x":
        archive.write_text(yaml.safe_dump({"dispatch": [
            {"thread": {"platform": "x", "posts": ["one", "two", "three"]}},
        ]}))
    with pytest.raises(mod.UnresolvedThreadError, match="unresolved-thread"):
        mod.count_ledgers(platform="bsky", action="post", since=mod._parse_dt("2026-06-28"),
                          until=mod._parse_dt("2026-06-29"), state_root=tmp_path, state_dirs=[])


@pytest.mark.parametrize("duplicate, expected", [(False, 2), (True, 1)])
def test_nested_ledger_and_created_id_dedup(tmp_path, capsys, duplicate, expected):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    first = {"action": "reply", "platform": "bsky", "timestamp": "2026-06-28T01:00:00Z", "createdId": "post-1"}
    second = {**first, "createdId": "post-1" if duplicate else "post-2",
              "timestamp": "2026-06-28T02:00:00Z"}
    _write_ledger(poller / "sent_ledger-bsky.yaml", [first])
    _write_ledger(poller / ".social-cli/state/sent_ledger-bsky.yaml", [second])
    rc = mod.main(["--platform", "bsky", "--since", "2026-06-28",
                   "--until", "2026-06-29", "--state-root", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(expected)


@pytest.mark.parametrize("body", [
    "", " \n", "[", "null", "42", "{}", "unknown: []",
    "[not-a-mapping]", "entries: [not-a-mapping]",
    'entries: [{action: post, platform: bsky, timestamp: "2026-06-28T01:00:00Z"}, 42]',
    "sent: []\nentries: not-a-list",
    "entries: [{action: post, platform: bsky}]",
    "action: reply\nplatform: bsky\ntimestamp: not-a-date",
])
@pytest.mark.parametrize("json_output", [False, True])
def test_untrusted_existing_ledger_fails_closed(tmp_path, capsys, body, json_output):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir()
    path = poller / "sent_ledger-bsky.yaml"
    path.write_text(body)
    with pytest.raises(mod.LedgerUnreadableError):
        mod._load_ledger(path)
    args = ["--platform", "bsky", "--since", "2026-06-28", "--state-root", str(tmp_path)]
    if json_output:
        args.append("--json")
    assert mod.main(args) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "CAP UNKNOWN" in output.err


def test_unreadable_ledger_fails_closed(tmp_path, monkeypatch, capsys):
    mod = fresh_count()
    path = tmp_path / "social-cli-notifications/sent_ledger-bsky.yaml"
    _write_ledger(path, [])
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture: unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(mod.LedgerUnreadableError):
        mod._load_ledger(path)
    assert mod.main(["--platform", "bsky", "--state-root", str(tmp_path)]) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "CAP UNKNOWN" in output.err


@pytest.mark.parametrize("key", ["entries", "ledger", "sent", "items", "results", "dispatch"])
def test_all_ledger_list_keys_validate_every_item(tmp_path, key):
    mod = fresh_count()
    path = tmp_path / "ledger.yaml"
    path.write_text(f"{key}: [42]")
    with pytest.raises(mod.LedgerUnreadableError, match="non-mapping"):
        mod._load_ledger(path)
    path.write_text(f"{key}: not-a-list\nsent: []" if key != "sent" else "sent: not-a-list\nentries: []")
    with pytest.raises(mod.LedgerUnreadableError, match="not a list"):
        mod._load_ledger(path)


def test_canonical_entries_mapping_counts_posts(tmp_path, capsys):
    mod = fresh_count()
    poller = tmp_path / "social-cli-notifications"
    poller.mkdir()
    (poller / "sent_ledger-bsky.yaml").write_text(
        'entries: [{action: post, platform: bsky, timestamp: "2026-06-28T01:00:00Z"}]'
    )
    assert mod.main(["--platform", "bsky", "--since", "2026-06-28", "--state-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "1\n"


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


def test_missing_and_empty_list_ledgers_return_zero(tmp_path):
    mod = fresh_count()
    (tmp_path / "social-cli-feed").mkdir()
    assert mod._load_ledger(tmp_path / "missing.yaml") == []
    (tmp_path / "social-cli-feed" / "sent_ledger-bsky.yaml").write_text("[]", encoding="utf-8")

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
