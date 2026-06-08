"""Core-memory template seeding (chainlink #347).

Fresh setup used to hold the core-memory defaults as Python constants in
mimir.commands.setup. Keeping those defaults as package data files makes
them git-mergeable for the version-triggered upgrade proposal path.
"""

from __future__ import annotations

from pathlib import Path

from mimir.memory_templates import (
    DEFAULT_ACTION_BOUNDARIES,
    DEFAULT_FILING_RULES,
    DEFAULT_HEARTBEAT_PATTERNS,
    DEFAULT_IDENTITY_MD,
    DEFAULT_LEARNED_BEHAVIORS,
    DEFAULT_NON_GOALS,
    DEFAULT_REFLECTION_POLICY,
    DEFAULT_VSM_TERMS,
    core_template_text,
    seed_core_memory,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_TEMPLATE_ROOT = REPO_ROOT / "mimir" / "memory_templates" / "core"
CORE_TEMPLATE_NAMES = (
    "00-identity.md",
    "05-non-goals.md",
    "06-action-boundaries.md",
    "20-vsm-terms.md",
    "30-reflection-policy.md",
    "40-learned-behaviors.md",
    "50-heartbeat-patterns.md",
    "60-filing-rules.md",
)


def test_core_memory_templates_are_bundled_files() -> None:
    for name in CORE_TEMPLATE_NAMES:
        path = CORE_TEMPLATE_ROOT / name
        assert path.is_file(), f"missing bundled core-memory template: {path}"
        assert path.read_text(encoding="utf-8").startswith("<!-- desc:")


def test_core_memory_non_goals_default_contains_frame_check() -> None:
    text = core_template_text("05-non-goals.md")

    assert "Don't accept the source frame uncritically" in text
    assert "Sycophancy is the soft version" in text
    assert "unexamined frame\nacceptance" in text
    assert "Before I implement: is X the right thing?" in text


def test_core_memory_learned_behaviors_default_contains_frame_check_procedure() -> None:
    text = core_template_text("40-learned-behaviors.md")

    assert "frame-check before design work" in text
    assert "procedural counterpart to the\nnon-goal" in text
    assert "Does Y actually want X?" in text
    assert "how should we implement X" in text


def test_backward_compatible_default_constants_read_template_files() -> None:
    assert DEFAULT_IDENTITY_MD == core_template_text("00-identity.md")
    assert DEFAULT_NON_GOALS == core_template_text("05-non-goals.md")
    assert DEFAULT_ACTION_BOUNDARIES == core_template_text("06-action-boundaries.md")
    assert DEFAULT_VSM_TERMS == core_template_text("20-vsm-terms.md")
    assert DEFAULT_REFLECTION_POLICY == core_template_text("30-reflection-policy.md")
    assert DEFAULT_LEARNED_BEHAVIORS == core_template_text("40-learned-behaviors.md")
    assert DEFAULT_HEARTBEAT_PATTERNS == core_template_text("50-heartbeat-patterns.md")
    assert DEFAULT_FILING_RULES == core_template_text("60-filing-rules.md")


def test_seed_core_memory_copies_missing_templates(tmp_path: Path) -> None:
    home = tmp_path / "agent"

    status = seed_core_memory(home)

    assert set(status) == set(CORE_TEMPLATE_NAMES)
    assert all(value == "created" for value in status.values())
    for name in CORE_TEMPLATE_NAMES:
        seeded = home / "memory" / "core" / name
        assert seeded.read_text(encoding="utf-8") == core_template_text(name)


def test_seed_core_memory_preserves_existing_files(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    custom = home / "memory" / "core" / "00-identity.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("<!-- desc: custom -->\n# Custom Identity\n", encoding="utf-8")

    status = seed_core_memory(home)

    assert status["00-identity.md"] == "present"
    assert custom.read_text(encoding="utf-8") == "<!-- desc: custom -->\n# Custom Identity\n"
    assert status["06-action-boundaries.md"] == "created"
