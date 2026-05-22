"""v0.4 §1: heartbeat foundation.

Skill bundling, setup_home file scaffolding, and the prompt-switch for
``trigger=scheduled_tick`` events. The skill *content* (librarian
protocol, backlog selection) is exercised by the agent runtime, not
unit tests."""

from __future__ import annotations

from pathlib import Path

from mimir.cli import (
    DEFAULT_HEARTBEAT_BACKLOG,
    DEFAULT_HEARTBEAT_PATTERNS,
    setup_home,
)
from mimir.models import AgentEvent
from mimir.prompts import HEARTBEAT_DEFAULT_PROMPT, build_turn_prompt
# ---- Heartbeat prompt template (no longer a bundled skill) ---------------


def test_heartbeat_prompt_template_is_bundled():
    """Post-2026-05-22: heartbeat's workflow ships as a prompt template,
    not a bundled skill. The template gets seeded to ``<home>/prompts/``
    on first setup; the scheduler's heartbeat entry points at it."""
    template = (
        Path(__file__).parent.parent
        / "mimir"
        / "prompt_templates"
        / "heartbeat.md"
    )
    assert template.is_file(), f"heartbeat template missing at {template}"


def test_heartbeat_prompt_template_has_required_sections():
    body = (
        Path(__file__).parent.parent
        / "mimir"
        / "prompt_templates"
        / "heartbeat.md"
    ).read_text()
    # Core sections of the cadence.
    for header in (
        "Mode: autonomous",
        "Librarian Protocol",
        "Backlog protocol",
        "End silently",
    ):
        assert header in body, (
            f"heartbeat prompt template missing section: {header!r}"
        )


# ---- setup_home additions -----------------------------------------------


def test_setup_writes_heartbeat_backlog_and_patterns(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)

    backlog = home / "state" / "heartbeat-backlog.md"
    patterns = home / "memory" / "core" / "50-heartbeat-patterns.md"
    assert backlog.is_file()
    assert patterns.is_file()

    backlog_body = backlog.read_text()
    # Format documentation + the two section headers the skill expects.
    assert "# Heartbeat Backlog" in backlog_body
    assert "## Active Backlog" in backlog_body
    assert "## Standing Tasks" in backlog_body
    assert "Frequency:" in backlog_body  # format hint
    assert "Last completed:" in backlog_body  # format hint

    patterns_body = patterns.read_text()
    # Core block convention: first line is desc comment for INDEX.md.
    assert patterns_body.splitlines()[0].startswith("<!-- desc:")

    # Status report mentions both files when newly created.
    files = status["files_created"]
    assert "state/heartbeat-backlog.md" in files
    assert "memory/core/50-heartbeat-patterns.md" in files


def test_setup_heartbeat_files_are_idempotent(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    # User edits the backlog with their own seed items.
    backlog = home / "state" / "heartbeat-backlog.md"
    user_body = "# Heartbeat Backlog\n\nMy own items.\n"
    backlog.write_text(user_body)

    setup_home(home)
    assert backlog.read_text() == user_body  # not clobbered


def test_setup_scheduler_yaml_includes_default_recurring_ticks(tmp_path: Path):
    """The default scheduler.yaml ships heartbeat + reflect ticks enabled
    out of the box. The §12.4 homeostat suppresses fires when the plan
    window saturates, so an hourly heartbeat is safe by default."""
    home = tmp_path / "agent"
    setup_home(home)
    body = (home / "scheduler.yaml").read_text()
    assert "heartbeat" in body
    assert "scheduled_tick" in body
    # Heartbeat hourly + reflect Sunday 06:00 UTC.
    assert "0 * * * *" in body
    assert "0 6 * * 0" in body
    # Both jobs declared (not commented out).
    assert "- name: heartbeat" in body
    assert "- name: reflect" in body


# ---- Constant content sanity --------------------------------------------


def test_default_heartbeat_backlog_constant_matches_format():
    """Guard against accidental edits dropping the schema documentation
    that the skill expects to find on first read."""
    assert "# Heartbeat Backlog" in DEFAULT_HEARTBEAT_BACKLOG
    assert "## Active Backlog" in DEFAULT_HEARTBEAT_BACKLOG
    assert "## Standing Tasks" in DEFAULT_HEARTBEAT_BACKLOG


def test_default_heartbeat_patterns_starts_with_desc_comment():
    assert DEFAULT_HEARTBEAT_PATTERNS.startswith("<!-- desc:")


# ---- build_turn_prompt switch -------------------------------------------


def _scheduled_event(content: str = "") -> AgentEvent:
    return AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        author=None,
        content=content,
    )


def test_turn_prompt_uses_heartbeat_header_for_scheduled_tick():
    prompt = build_turn_prompt(_scheduled_event(content="custom prompt"))
    assert "[scheduled_tick: scheduler:heartbeat" in prompt
    assert "custom prompt" in prompt
    # The default user-message header shape is gone.
    assert "[event_kind: scheduled_tick" not in prompt
    assert "author:" not in prompt


def test_turn_prompt_falls_back_to_default_when_no_content():
    prompt = build_turn_prompt(_scheduled_event())
    assert HEARTBEAT_DEFAULT_PROMPT in prompt
    # Sanity: no "(no content)" placeholder leaked through.
    assert "(no content)" not in prompt


def test_turn_prompt_keeps_default_header_for_user_message():
    user_event = AgentEvent(
        trigger="user_message",
        channel_id="slack-eng",
        author="alice",
        content="hello",
    )
    prompt = build_turn_prompt(user_event)
    assert "[event_kind: user_message" in prompt
    assert "author: alice" in prompt
    assert HEARTBEAT_DEFAULT_PROMPT not in prompt
