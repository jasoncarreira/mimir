"""Tests for the social-cli feed poller.

Mocks ``_fetch`` (the social-cli invocation) and writes a synthetic
``feed-{platform}.yaml`` directly into STATE_DIR for ``_load_feed``
to read. This exercises the real YAML parse path while keeping
social-cli itself out of the test surface.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fresh_feed_poller(tmp_path: Path, monkeypatch):
    """Re-import per test so STATE_DIR module constant picks up tmp_path."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("POLLER_NAME", "social-cli-feed")
    monkeypatch.delenv("MIMIR_SOCIAL_PLATFORMS", raising=False)
    monkeypatch.delenv("MIMIR_SOCIAL_FEED_LIMIT", raising=False)
    monkeypatch.delenv("SOCIAL_CLI_BIN", raising=False)

    sys.modules.pop("feed_poller", None)
    return importlib.import_module("feed_poller")


def _write_feed(state_dir: Path, platform: str, posts: list[dict]) -> Path:
    """Write a synthetic feed-{platform}.yaml in social-cli's exact shape.

    Note: feed.yaml is a flat top-level list (unlike inbox.yaml which
    wraps in {notifications: [...]}).
    """
    import yaml
    state_dir.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(posts)
    path = state_dir / f"feed-{platform}.yaml"
    path.write_text(body)
    return path


def _post(pid: str, platform="bsky", author="alice.bsky.social",
          text="some post", likes=0, replies=0, reposts=0) -> dict:
    return {
        "id": pid,
        "platform": platform,
        "author": author,
        "authorId": f"did:plc:{pid}",
        "text": text,
        "timestamp": "2026-05-23T12:00:00Z",
        "likeCount": likes,
        "replyCount": replies,
        "repostCount": reposts,
    }


def _capture_emits(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _stub_fetch(monkeypatch, fresh_feed_poller):
    """No-op _fetch — feed YAMLs are pre-written by the test fixture."""
    monkeypatch.setattr(
        fresh_feed_poller, "_fetch",
        lambda platform, *a, **k: Path(fresh_feed_poller.STATE_DIR) / f"feed-{platform}.yaml",
    )


def test_first_run_empty_cursor_emits_all(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_feed(tmp_path, "bsky", [_post("p1"), _post("p2"), _post("p3")])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    rc = fresh_feed_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["post_id"] for e in events] == ["p1", "p2", "p3"]

    cursor = json.loads(fresh_feed_poller.CURSOR_FILE.read_text())
    assert cursor == ["p1", "p2", "p3"]


def test_existing_cursor_skips_seen_ids(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    fresh_feed_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fresh_feed_poller.CURSOR_FILE.write_text(json.dumps(["p1", "p2"]))

    _write_feed(tmp_path, "bsky", [_post(f"p{i}") for i in range(1, 5)])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    rc = fresh_feed_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["post_id"] for e in events] == ["p3", "p4"]

    cursor = json.loads(fresh_feed_poller.CURSOR_FILE.read_text())
    assert cursor == ["p1", "p2", "p3", "p4"]


def test_empty_feed_emits_nothing(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_feed(tmp_path, "bsky", [])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    rc = fresh_feed_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_feed_file_missing_emits_nothing(fresh_feed_poller, monkeypatch, capsys):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _stub_fetch(monkeypatch, fresh_feed_poller)

    rc = fresh_feed_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_fetch_failure_one_platform_doesnt_block_others(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    """If bsky fetch fails but x succeeds, we still emit x posts."""
    import subprocess
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky,x")
    _write_feed(tmp_path, "x", [_post("xp1", platform="x")])

    def selective_fetch(platform, *a, **k):
        if platform == "bsky":
            raise subprocess.CalledProcessError(1, ["social-cli"], "", "auth")
        return Path(fresh_feed_poller.STATE_DIR) / f"feed-{platform}.yaml"

    monkeypatch.setattr(fresh_feed_poller, "_fetch", selective_fetch)
    rc = fresh_feed_poller.main()
    assert rc == 0
    events = _capture_emits(capsys)
    assert [e["post_id"] for e in events] == ["xp1"]


def test_posts_without_id_skipped(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_feed(tmp_path, "bsky", [
        {"platform": "bsky", "text": "no id"},  # missing id
        _post("good1"),
    ])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    fresh_feed_poller.main()
    events = _capture_emits(capsys)
    assert [e["post_id"] for e in events] == ["good1"]


def test_event_shape_includes_stats(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_feed(tmp_path, "bsky", [_post(
        "at://did:plc:xxx/app.bsky.feed.post/abc",
        author="alice.bsky.social", text="interesting take",
        likes=42, replies=7, reposts=3,
    )])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    fresh_feed_poller.main()
    events = _capture_emits(capsys)
    assert len(events) == 1
    ev = events[0]
    assert ev["poller"] == "social-cli-feed"
    assert ev["source_platform"] == "bsky"
    assert ev["author"] == "alice.bsky.social"
    assert ev["text"] == "interesting take"
    assert ev["like_count"] == 42
    assert ev["reply_count"] == 7
    assert ev["repost_count"] == 3
    assert "likes:42 replies:7 reposts:3" in ev["prompt"]


def test_unseen_posts_persist_in_cursor(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    """First poll surfaces posts; second poll on the same yaml should
    not re-emit them — the cursor prevents that."""
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    _write_feed(tmp_path, "bsky", [_post("p1"), _post("p2")])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    rc = fresh_feed_poller.main()
    assert rc == 0
    assert len(_capture_emits(capsys)) == 2

    # Second run, same feed file → cursor suppresses re-emit.
    rc = fresh_feed_poller.main()
    assert rc == 0
    assert _capture_emits(capsys) == []


def test_cursor_lru_caps_at_max(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    fresh_feed_poller.CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    cap = fresh_feed_poller.CURSOR_MAX_IDS
    seed = [f"old{i}" for i in range(cap)]
    fresh_feed_poller.CURSOR_FILE.write_text(json.dumps(seed))

    _write_feed(tmp_path, "bsky", [_post(f"new{i}") for i in range(3)])
    _stub_fetch(monkeypatch, fresh_feed_poller)

    fresh_feed_poller.main()
    cursor = json.loads(fresh_feed_poller.CURSOR_FILE.read_text())
    assert len(cursor) == cap
    assert cursor[0] == "old3"
    assert cursor[-3:] == ["new0", "new1", "new2"]


def test_empty_platforms_returns_1(fresh_feed_poller, monkeypatch, capsys):
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "")
    rc = fresh_feed_poller.main()
    assert rc == 1


def test_feed_limit_env_clamped(fresh_feed_poller, monkeypatch, capsys, tmp_path):
    """MIMIR_SOCIAL_FEED_LIMIT is clamped 1–200 like the notifications limit."""
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    monkeypatch.setenv("MIMIR_SOCIAL_FEED_LIMIT", "500")
    _write_feed(tmp_path, "bsky", [])
    captured = {}

    def fake_fetch(platform, limit, bin_path):
        captured["limit"] = limit
        return Path(fresh_feed_poller.STATE_DIR) / f"feed-{platform}.yaml"

    monkeypatch.setattr(fresh_feed_poller, "_fetch", fake_fetch)
    fresh_feed_poller.main()
    assert captured["limit"] == 200  # clamped from 500
