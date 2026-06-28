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

    assert total == 2


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
