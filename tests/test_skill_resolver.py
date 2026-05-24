"""Tests for ``mimir/skill_resolver.py`` — channel→skill auto-resolution."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from mimir.skill_resolver import (
    _strip_frontmatter,
    find_skill_for_channel,
)


# ─── Fixture helper: build a synthetic skill on disk ──────────────────


def _seed_skill(
    base: Path,
    name: str,
    *,
    pollers: list[str] | None = None,
    skill_md_body: str = "Body.",
    frontmatter: str | None = "name: synthetic\ndescription: test",
) -> Path:
    """Create ``<base>/<name>/`` with optional pollers.json + SKILL.md."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if pollers is not None:
        (skill_dir / "pollers.json").write_text(
            json.dumps({"pollers": [{"name": p} for p in pollers]}),
            encoding="utf-8",
        )
    if frontmatter is None:
        text = skill_md_body
    else:
        text = f"---\n{frontmatter}\n---\n\n{skill_md_body}"
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


# ─── _strip_frontmatter ───────────────────────────────────────────────


def test_strip_frontmatter_with_yaml_block():
    text = "---\nname: foo\ndescription: bar\n---\n\nBody content here.\n"
    assert _strip_frontmatter(text) == "Body content here.\n"


def test_strip_frontmatter_no_block():
    text = "# Just a heading\n\nBody.\n"
    assert _strip_frontmatter(text) == text


def test_strip_frontmatter_empty_input():
    assert _strip_frontmatter("") == ""


def test_strip_frontmatter_unterminated_block():
    """Malformed (no closing ``---``) → return whole text rather than
    swallowing the file."""
    text = "---\nname: foo\nno closing\n"
    assert _strip_frontmatter(text) == text


def test_strip_frontmatter_preserves_internal_dashes():
    """``---`` separators inside the body must NOT terminate parsing."""
    text = "---\nname: foo\n---\n\nIntro.\n\n---\n\nMore body.\n"
    out = _strip_frontmatter(text)
    assert out.startswith("Intro.")
    assert "More body." in out


# ─── find_skill_for_channel ───────────────────────────────────────────


def test_returns_none_for_empty_channel(tmp_path: Path):
    assert find_skill_for_channel("", [tmp_path]) is None
    assert find_skill_for_channel(None, [tmp_path]) is None


def test_returns_none_for_non_poller_channel(tmp_path: Path):
    """user_message / scheduler / react channels are NOT mapped."""
    assert find_skill_for_channel("discord-123", [tmp_path]) is None
    assert find_skill_for_channel("scheduler:reflect", [tmp_path]) is None
    assert find_skill_for_channel("scheduler:heartbeat", [tmp_path]) is None


def test_returns_none_when_poller_has_no_matching_skill(tmp_path: Path):
    """``poller:unknown`` with no skill declaring it → None."""
    _seed_skill(tmp_path, "other-skill", pollers=["other-poller"])
    assert find_skill_for_channel(
        "poller:nonexistent", [tmp_path],
    ) is None


def test_matches_skill_by_poller_name(tmp_path: Path):
    """A skill whose pollers.json declares the matching poller →
    returns its (name, body)."""
    _seed_skill(
        tmp_path,
        "social-cli",
        pollers=["social-cli-notifications", "social-cli-feed"],
        skill_md_body="# social-cli\n\nThe outbox + dispatch loop.\n",
    )
    result = find_skill_for_channel(
        "poller:social-cli-notifications", [tmp_path],
    )
    assert result is not None
    name, body = result
    assert name == "social-cli"
    assert "outbox + dispatch loop" in body


def test_matches_second_poller_in_same_skill(tmp_path: Path):
    """A skill with multiple pollers declared in pollers.json — any
    of them should resolve back to the same skill body."""
    _seed_skill(
        tmp_path,
        "social-cli",
        pollers=["social-cli-notifications", "social-cli-feed"],
        skill_md_body="# social-cli\n\nThe outbox + dispatch loop.\n",
    )
    result = find_skill_for_channel(
        "poller:social-cli-feed", [tmp_path],
    )
    assert result is not None
    assert result[0] == "social-cli"


def test_operator_skills_shadow_bundled(tmp_path: Path):
    """When both an operator-installed and a bundled skill declare
    the same poller, the operator copy wins (passed first in
    skills_dirs ordering)."""
    operator_dir = tmp_path / "operator"
    bundled_dir = tmp_path / "bundled"
    operator_dir.mkdir()
    bundled_dir.mkdir()
    _seed_skill(
        operator_dir, "social-cli",
        pollers=["social-cli-notifications"],
        skill_md_body="OPERATOR VERSION",
    )
    _seed_skill(
        bundled_dir, "social-cli",
        pollers=["social-cli-notifications"],
        skill_md_body="BUNDLED VERSION",
    )
    # Operator-first ordering means operator wins.
    result = find_skill_for_channel(
        "poller:social-cli-notifications",
        [operator_dir, bundled_dir],
    )
    assert result is not None
    assert "OPERATOR VERSION" in result[1]


def test_frontmatter_stripped_from_body(tmp_path: Path):
    """The returned body must have YAML frontmatter removed —
    that's metadata, not operator-facing content."""
    _seed_skill(
        tmp_path,
        "social-cli",
        pollers=["social-cli-notifications"],
        frontmatter="name: social-cli\ndescription: long desc here",
        skill_md_body="# social-cli\n\nUse outbox.yaml.\n",
    )
    result = find_skill_for_channel(
        "poller:social-cli-notifications", [tmp_path],
    )
    assert result is not None
    name, body = result
    # Body shouldn't contain the frontmatter.
    assert "description:" not in body
    assert "Use outbox.yaml" in body


def test_skill_md_missing(tmp_path: Path):
    """A skill dir with pollers.json but NO SKILL.md → returns None
    (incomplete skill)."""
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        json.dumps({"pollers": [{"name": "broken-poller"}]}),
        encoding="utf-8",
    )
    # No SKILL.md.
    assert find_skill_for_channel(
        "poller:broken-poller", [tmp_path],
    ) is None


def test_malformed_pollers_json(tmp_path: Path):
    """A skill dir with un-parseable pollers.json → silently skipped
    during scan (debug log only)."""
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text("not-valid-json{{", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    # No exception, just no match.
    assert find_skill_for_channel(
        "poller:any-name", [tmp_path],
    ) is None


def test_non_dict_pollers_value(tmp_path: Path):
    """pollers.json shape with non-list ``pollers`` field → skipped."""
    skill_dir = tmp_path / "weird"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        json.dumps({"pollers": "not-a-list"}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    assert find_skill_for_channel(
        "poller:anything", [tmp_path],
    ) is None


def test_missing_skills_dirs(tmp_path: Path):
    """Non-existent skills_dirs paths are silently skipped."""
    missing = tmp_path / "does-not-exist"
    real = tmp_path / "real"
    _seed_skill(real, "social-cli", pollers=["social-cli-notifications"])
    result = find_skill_for_channel(
        "poller:social-cli-notifications",
        [missing, real],
    )
    assert result is not None
    assert result[0] == "social-cli"


def test_empty_body_returns_none(tmp_path: Path):
    """SKILL.md present but body is empty after frontmatter strip →
    returns None (no useful content to surface)."""
    skill_dir = tmp_path / "empty"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        json.dumps({"pollers": [{"name": "empty-poller"}]}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("---\nname: empty\n---\n\n   ", encoding="utf-8")
    assert find_skill_for_channel(
        "poller:empty-poller", [tmp_path],
    ) is None


def test_poller_with_whitespace_in_channel(tmp_path: Path):
    """Empty poller name after strip → None (defensive)."""
    assert find_skill_for_channel("poller:", [tmp_path]) is None
    assert find_skill_for_channel("poller:   ", [tmp_path]) is None
