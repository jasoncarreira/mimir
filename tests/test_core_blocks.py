"""Core block loading + description extraction (SPEC §3.1, §3.4, §5.1)."""

from __future__ import annotations

from pathlib import Path

from mimir.core_blocks import (
    check_core_blocks_health,
    describe_file,
    extract_desc_comment,
    first_sentence_fallback,
    load_core,
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
