"""v0.4 §4: reflection skill — bundling, scaffolding, policy starter.

The skill *behavior* (how the agent decides to propose vs. apply) is
exercised at runtime, not in unit tests. These tests cover the seams
that have to be right for the skill to even run: skill registration,
setup_home file scaffolding, the policy-file shape the skill reads at
the start of every reflection turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.cli import (
    DEFAULT_LEARNED_BEHAVIORS,
    DEFAULT_PROPOSED_CHANGES,
    DEFAULT_REFLECTION_POLICY,
    setup_home,
)
from mimir.skill_defs import _bundled_skill_names


# ---- Skill bundling ------------------------------------------------------


def test_reflection_skill_is_bundled():
    assert "reflection" in _bundled_skill_names()


def test_reflection_skill_documents_both_tracks():
    skill_path = (
        Path(__file__).parent.parent
        / "mimir"
        / "skills"
        / "reflection"
        / "SKILL.md"
    )
    body = skill_path.read_text()
    assert body.startswith("---\n")
    assert "name: reflection" in body
    # Two tracks the skill must teach.
    assert "behavioral" in body.lower()
    assert "memory architecture review" in body.lower()
    # Promotion criteria — load-bearing for atom-to-core decisions.
    for criterion in ("Recurrence", "Generality", "Stability", "Cost of forgetting"):
        assert criterion in body, f"missing promotion criterion: {criterion}"


def test_reflection_skill_references_bundled_script():
    """The skill must point at the most-retrieved CLI subcommand — that's
    how the agent gets atom-to-core promotion candidates."""
    skill_path = (
        Path(__file__).parent.parent
        / "mimir"
        / "skills"
        / "reflection"
        / "SKILL.md"
    )
    body = skill_path.read_text()
    assert "mimir reflection most-retrieved" in body
    assert "--contributed-only" in body


# ---- setup_home additions -----------------------------------------------


def test_setup_writes_reflection_policy(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    policy = home / "memory" / "core" / "30-reflection-policy.md"
    assert policy.is_file()
    body = policy.read_text()
    # First line is the desc-comment so memory/INDEX.md stays clean.
    assert body.splitlines()[0].startswith("<!-- desc:")
    # Two policy sections — autonomous and propose-only.
    assert "## Autonomous" in body
    assert "## Propose-only" in body
    # Conservative defaults: persona / skill creation / deletions are HITL.
    assert "Persona block edits" in body
    assert "Skill creation" in body
    assert "Memory file deletions" in body


def test_setup_writes_learned_behaviors_starter(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    learned = home / "memory" / "core" / "40-learned-behaviors.md"
    assert learned.is_file()
    body = learned.read_text()
    assert body.splitlines()[0].startswith("<!-- desc:")
    assert "# Learned Behaviors" in body


def test_setup_writes_proposed_changes_starter(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    proposed = home / "state" / "proposed-changes.md"
    assert proposed.is_file()
    body = proposed.read_text()
    assert "# Proposed Changes" in body
    # The three buckets the skill writes into and the operator moves between.
    assert "## Pending" in body
    assert "## Applied" in body
    assert "## Rejected" in body


def test_setup_files_are_idempotent(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    # User edits the policy.
    policy = home / "memory" / "core" / "30-reflection-policy.md"
    user_body = "<!-- desc: my custom policy -->\n# My Policy\n"
    policy.write_text(user_body)
    setup_home(home)
    assert policy.read_text() == user_body  # not clobbered


def test_setup_scheduler_yaml_documents_reflect_entry(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    body = (home / "scheduler.yaml").read_text()
    assert "reflect" in body
    assert "0 4 * * 0" in body  # Sunday 04:00 UTC


# ---- Constant content sanity --------------------------------------------


def test_default_reflection_policy_has_required_sections():
    body = DEFAULT_REFLECTION_POLICY
    assert "## Autonomous" in body
    assert "## Propose-only" in body
    assert "MSAM atom decay" in body
    assert "Persona block edits" in body


def test_default_learned_behaviors_starts_with_desc_comment():
    assert DEFAULT_LEARNED_BEHAVIORS.startswith("<!-- desc:")


def test_default_proposed_changes_documents_format():
    body = DEFAULT_PROPOSED_CHANGES
    assert "Source:" in body
    assert "Proposal:" in body
    assert "Rationale:" in body
