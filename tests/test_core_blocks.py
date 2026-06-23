"""Core block loading + description extraction (SPEC §3.1, §3.4, §5.1)."""

from __future__ import annotations

import logging
from pathlib import Path

from mimir.core_blocks import (
    _CHANNEL_MEMORY_OVER_CAP_REPORTED,
    check_core_blocks_health,
    describe_file,
    extract_desc_comment,
    first_sentence_fallback,
    load_channel_memory,
    load_core,
    read_text_lossy,
    render_core_section,
)


def test_extract_desc_comment_present():
    text = "<!-- desc: who I am -->\n# Persona\n\nI am Mimir."
    assert extract_desc_comment(text) == "who I am"


def test_extract_desc_comment_absent():
    text = "# Title\n\nbody"
    assert extract_desc_comment(text) is None


def test_first_sentence_fallback_skips_h1_and_desc():
    text = "<!-- desc: ... -->\n# Title\n\nThis is the first sentence. And another."
    assert first_sentence_fallback(text) == "This is the first sentence."


def test_first_sentence_fallback_truncates_at_120():
    body = "x" * 200
    text = f"# Title\n\n{body}"
    out = first_sentence_fallback(text)
    assert len(out) <= 120


def test_describe_file_marks_explicit_vs_auto():
    explicit = "<!-- desc: e -->\n# T\nbody."
    desc, is_auto = describe_file(explicit)
    assert desc == "e" and is_auto is False

    fallback = "# T\nfirst sentence here. ignored."
    desc, is_auto = describe_file(fallback)
    assert desc == "first sentence here." and is_auto is True


def test_load_core_orders_lexicographically(tmp_path: Path):
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    (core / "20-style.md").write_text("# style\nfoo.")
    (core / "00-persona.md").write_text("<!-- desc: persona -->\n# persona")
    (core / "10-procedures.md").write_text("# procedures\nbar.")

    blocks = load_core(tmp_path)
    paths = [b.path.name for b in blocks]
    assert paths == ["00-persona.md", "10-procedures.md", "20-style.md"]
    assert blocks[0].description == "persona"
    assert blocks[0].is_auto_description is False
    assert blocks[1].is_auto_description is True


def test_load_core_returns_empty_when_dir_missing(tmp_path: Path):
    assert load_core(tmp_path) == []


def test_read_text_lossy_replaces_non_utf8_and_logs(tmp_path: Path, caplog):
    """A stray non-UTF-8 byte is replacement-decoded (not raised) and logged
    with the file + position — chainlink #470."""
    p = tmp_path / "bad.md"
    # 0xa7 = '§' in cp1252; the exact byte/shape from the #470 heartbeat crash.
    p.write_bytes(b"# Heading\n\nclean prose then a stray \xa7 byte\n")
    with caplog.at_level(logging.WARNING, logger="mimir.core_blocks"):
        text = read_text_lossy(p)
    assert "�" in text  # replacement char in place of the bad byte
    assert "Heading" in text  # surrounding content preserved
    assert any("non-UTF-8" in r.getMessage() for r in caplog.records)


def test_read_text_lossy_strict_path_is_exact(tmp_path: Path):
    p = tmp_path / "ok.md"
    p.write_text("# ok\n\nclean — café ☕", encoding="utf-8")
    assert read_text_lossy(p) == "# ok\n\nclean — café ☕"


def test_load_core_survives_non_utf8_block(tmp_path: Path, caplog):
    """Regression #470: a core/*.md with a stray non-UTF-8 byte must NOT crash
    prompt assembly. UnicodeDecodeError is a ValueError, not OSError, so the old
    `except OSError` missed it and the whole turn died pre-tool. Both blocks load;
    the bad one is replacement-decoded, not dropped."""
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00-persona.md").write_text(
        "<!-- desc: persona -->\n# persona\nclean", encoding="utf-8"
    )
    (core / "10-bad.md").write_bytes(b"<!-- desc: bad -->\n# bad\n\xa7 stray cp1252 byte")
    with caplog.at_level(logging.WARNING, logger="mimir.core_blocks"):
        blocks = load_core(tmp_path)
    assert len(blocks) == 2
    assert any("non-UTF-8" in r.getMessage() for r in caplog.records)


def test_read_text_lossy_emits_actionable_event_once(tmp_path: Path):
    """A non-UTF-8 read emits a deduped ``non_utf8_home_file`` algedonic event
    so the agent is prompted to clean the file — once per file per process
    (read_text_lossy runs every turn during prompt assembly). chainlink #470."""
    import json

    import mimir.core_blocks as cb
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    events = tmp_path / "events.jsonl"
    init_logger(events, session_id="t-470")
    try:
        cb._NON_UTF8_REPORTED.clear()
        p = tmp_path / "core-bad.md"
        # 0xa7 lands at byte 52 — the exact position from the #470 incident.
        p.write_bytes(b"# bad\n" + b"x" * 46 + b"\xa7 tail")
        cb.read_text_lossy(p)
        cb.read_text_lossy(p)  # second read must NOT re-emit (dedupe)
    finally:
        _reset_logger_for_tests()

    lines = [
        json.loads(line)
        for line in events.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    emits = [e for e in lines if e.get("type") == "non_utf8_home_file"]
    assert len(emits) == 1
    assert emits[0]["path"] == str(p)
    assert emits[0]["byte"] == "0xa7"
    assert emits[0]["position"] == 52


def test_render_core_section_separates_blocks(tmp_path: Path):
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00-a.md").write_text("# A\nbody-a")
    (core / "10-b.md").write_text("# B\nbody-b")
    rendered = render_core_section(load_core(tmp_path))
    assert "body-a" in rendered
    assert "body-b" in rendered
    assert "---" in rendered


# ── S1-3: check_core_blocks_health ─────────────────────────────────────────


def test_core_blocks_health_clean(tmp_path: Path):
    """Enough healthy blocks → not degraded, no issues."""
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    for i in range(6):
        # Each file is well over 200 bytes.
        (core / f"{i:02d}-block.md").write_text("# Block\n" + "x" * 300)
    blocks = load_core(tmp_path)
    degraded, issues = check_core_blocks_health(blocks, min_count=5, min_bytes=200)
    assert not degraded
    assert issues == []


def test_core_blocks_health_too_few(tmp_path: Path):
    """Fewer blocks than min_count → degraded with under-count issue."""
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    for i in range(3):
        (core / f"{i:02d}-block.md").write_text("# Block\n" + "x" * 300)
    blocks = load_core(tmp_path)
    degraded, issues = check_core_blocks_health(blocks, min_count=5, min_bytes=200)
    assert degraded
    assert any("3" in msg and "minimum 5" in msg for msg in issues)


def test_core_blocks_health_undersized_file(tmp_path: Path):
    """A block below min_bytes → degraded with the filename in the issue."""
    core = tmp_path / "memory" / "core"
    core.mkdir(parents=True)
    for i in range(5):
        (core / f"{i:02d}-block.md").write_text("# Block\n" + "x" * 300)
    # One stub file well below 200 bytes.
    (core / "05-stub.md").write_text("stub")
    blocks = load_core(tmp_path)
    degraded, issues = check_core_blocks_health(blocks, min_count=5, min_bytes=200)
    assert degraded
    assert any("05-stub.md" in msg for msg in issues)


# ── load_channel_memory (chainlink #187) ────────────────────────────────────


def test_load_channel_memory_returns_content(tmp_path: Path):
    """Files under memory/channels/<id>/ are concatenated and returned."""
    ch_dir = tmp_path / "memory" / "channels" / "discord-1500672382166110321"
    ch_dir.mkdir(parents=True)
    (ch_dir / "jason.md").write_text("# Jason\nOperator: Jason Carreira.")

    result = load_channel_memory(tmp_path, "discord-1500672382166110321")
    assert result is not None
    assert "Jason Carreira" in result


def test_load_channel_memory_multiple_files_sorted(tmp_path: Path):
    """Multiple files are sorted lexicographically and separated by ---."""
    ch_dir = tmp_path / "memory" / "channels" / "slack-C1"
    ch_dir.mkdir(parents=True)
    (ch_dir / "20-prefs.md").write_text("Prefers bullet lists.")
    (ch_dir / "00-meta.md").write_text("Channel meta info.")

    result = load_channel_memory(tmp_path, "slack-C1")
    assert result is not None
    # 00- sorts before 20-
    assert result.index("Channel meta info") < result.index("Prefers bullet lists")
    assert "---" in result


def test_load_channel_memory_returns_none_for_missing_dir(tmp_path: Path):
    """Returns None when channel directory doesn't exist."""
    result = load_channel_memory(tmp_path, "discord-nonexistent")
    assert result is None


def test_load_channel_memory_returns_none_for_empty_channel_id(tmp_path: Path):
    """Returns None on empty channel_id."""
    result = load_channel_memory(tmp_path, "")
    assert result is None


def test_load_channel_memory_skips_scheduler_channels(tmp_path: Path):
    """Synthetic scheduler:* channels return None (not injected)."""
    ch_dir = tmp_path / "memory" / "channels" / "scheduler:heartbeat"
    ch_dir.mkdir(parents=True)
    (ch_dir / "meta.md").write_text("Should not be injected.")

    result = load_channel_memory(tmp_path, "scheduler:heartbeat")
    assert result is None


def test_load_channel_memory_skips_poller_channels(tmp_path: Path):
    """Synthetic poller:* channels return None (not injected)."""
    ch_dir = tmp_path / "memory" / "channels" / "poller:github-activity"
    ch_dir.mkdir(parents=True)
    (ch_dir / "meta.md").write_text("Should not be injected.")

    result = load_channel_memory(tmp_path, "poller:github-activity")
    assert result is None


def test_load_channel_memory_truncates_at_cap(tmp_path: Path, monkeypatch):
    """When combined content exceeds the byte cap, output is truncated with a note."""
    from mimir import core_blocks as cb
    monkeypatch.setattr(cb, "_CHANNEL_MEMORY_MAX_BYTES", 50)

    ch_dir = tmp_path / "memory" / "channels" / "discord-1"
    ch_dir.mkdir(parents=True)
    (ch_dir / "big.md").write_text("x" * 200)

    result = load_channel_memory(tmp_path, "discord-1")
    assert result is not None
    assert "truncated" in result
    assert len(result.encode("utf-8")) > 50  # truncation note adds chars
    # The actual file content portion is capped
    assert result.startswith("x" * 10)  # at least some content came through


def test_load_channel_memory_emits_over_cap_event_once(tmp_path: Path, monkeypatch):
    import json

    from mimir import core_blocks as cb
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    monkeypatch.setattr(cb, "_CHANNEL_MEMORY_MAX_BYTES", 50)
    events = tmp_path / "events.jsonl"
    init_logger(events, session_id="t-643")
    try:
        _CHANNEL_MEMORY_OVER_CAP_REPORTED.clear()
        ch_dir = tmp_path / "memory" / "channels" / "discord-1"
        ch_dir.mkdir(parents=True)
        (ch_dir / "a.md").write_text("x" * 60)
        (ch_dir / "b.md").write_text("y" * 10)

        load_channel_memory(tmp_path, "discord-1")
        load_channel_memory(tmp_path, "discord-1")
    finally:
        _reset_logger_for_tests()
        _CHANNEL_MEMORY_OVER_CAP_REPORTED.clear()

    lines = [
        json.loads(line)
        for line in events.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    emits = [e for e in lines if e.get("type") == "channel_memory_over_cap"]
    assert len(emits) == 1
    assert emits[0]["channel_id"] == "discord-1"
    assert emits[0]["path"] == str(ch_dir)
    assert emits[0]["bytes"] > emits[0]["cap_bytes"] == 50
    assert emits[0]["file_count"] == 2


def test_load_channel_memory_does_not_emit_for_synthetic_channel(
    tmp_path: Path, monkeypatch
):
    import json

    from mimir import core_blocks as cb
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    monkeypatch.setattr(cb, "_CHANNEL_MEMORY_MAX_BYTES", 50)
    events = tmp_path / "events.jsonl"
    init_logger(events, session_id="t-643-synthetic")
    try:
        _CHANNEL_MEMORY_OVER_CAP_REPORTED.clear()
        ch_dir = tmp_path / "memory" / "channels" / "scheduler:heartbeat"
        ch_dir.mkdir(parents=True)
        (ch_dir / "notes.md").write_text("x" * 100)

        assert load_channel_memory(tmp_path, "scheduler:heartbeat") is None
    finally:
        _reset_logger_for_tests()
        _CHANNEL_MEMORY_OVER_CAP_REPORTED.clear()

    content = events.read_text(encoding="utf-8") if events.exists() else ""
    records = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert [e for e in records if e.get("type") == "channel_memory_over_cap"] == []
