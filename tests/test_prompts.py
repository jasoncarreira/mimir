"""System + turn prompt assembly (mimir/prompts.py).

Phase coverage focuses on v0.4 additions; legacy assembly is exercised
indirectly by agent / dispatcher tests."""

from __future__ import annotations

import os

import pytest

from mimir.config import Config
from mimir.prompts import build_system_prompt


# ---- v0.4 §6: operator alert channel surfacing ---------------------------


def test_system_prompt_includes_operator_alert_channel():
    sp = build_system_prompt(operator_alert_channel="dm-slack-U05ABC")
    assert "## Operator config" in sp
    assert "Operator alert channel: dm-slack-U05ABC" in sp


def test_system_prompt_omits_operator_alert_channel_when_unset():
    sp = build_system_prompt()
    assert "Operator alert channel" not in sp
    assert "## Operator config" not in sp


def test_system_prompt_omits_operator_alert_channel_when_empty():
    sp = build_system_prompt(operator_alert_channel="")
    assert "Operator alert channel" not in sp


# ---- Enforcement-aware ergonomics (chainlink #951) -----------------------


def test_system_prompt_omits_enforcement_guidance_in_shadow_mode():
    sp = build_system_prompt(access_control_enforced=False)

    assert "## Access-control enforcement" not in sp
    assert "Trust/taint model:" not in sp


def test_system_prompt_renders_accurate_enforcement_guidance():
    sp = build_system_prompt(access_control_enforced=True)

    assert "## Access-control enforcement" in sp
    assert "ergonomic guidance, not a security boundary" in sp
    assert "both untrusted and actively ingested this turn" in sp
    assert "auto-recall is informational and never gates" in sp
    assert "``fetch_url`` and ``web_search`` are destination-safe and taint-independent" in sp
    assert "regardless of turn taint" in sp
    assert "``webhook``, ``http_request``, and external MCP arguments are turn-taint gated" in sp
    assert "External MCP posture is per tool" in sp
    assert "``worklink_run``" in sp
    assert "Generic ``spawn_*`` is blocked" in sp
    assert "one-use declassification" in sp
    assert "do not blindly retry the same call" in sp
    assert "fill `web_search`" not in sp
    assert "trusted egress before" not in sp


# ---- Agent home ---------------------------------------------------------


def test_system_prompt_includes_agent_home_when_set():
    """``home_dir`` materializes an ``## Agent home`` section so the
    model has the absolute MIMIR_HOME path without having to infer it
    from prose in core blocks or from claude-code's default workspace."""
    sp = build_system_prompt(home_dir="/mimir-home")
    assert "## Agent home" in sp
    assert "MIMIR_HOME=/mimir-home" in sp
    # Section should sit between persona and core memory (or
    # conventions if core blocks aren't passed) — i.e. before any
    # ``memory/`` or ``state/`` path content the model might
    # otherwise misanchor. The conventions block's first heading
    # differs across branches (slimmed conventions drops the
    # ``## Conventions`` wrapper); use the first ``## `` after
    # ``## Agent home`` as the upper bound, whatever it happens to
    # be.
    persona_end = sp.find("\n\n")
    home_at = sp.find("## Agent home")
    next_section_at = sp.find("\n## ", home_at + len("## Agent home"))
    assert persona_end < home_at, "## Agent home must follow the persona"
    assert next_section_at != -1, (
        "## Agent home should not be the last section in the prompt"
    )


def test_system_prompt_omits_agent_home_when_unset():
    sp = build_system_prompt()
    assert "## Agent home" not in sp


def test_system_prompt_renders_writable_dirs_and_scratch_guidance():
    """When ``writable_dirs`` is passed, the Agent home section lists them
    and (when ``scratch`` is among them) steers ephemeral writes there — so
    the agent doesn't discover writability by trial/error or invent ad-hoc
    dirs the write-guard blocks (chainlink #299)."""
    sp = build_system_prompt(
        home_dir="/mimir-home",
        writable_dirs=["state", "memory", "attachments", "scratch", "skills"],
    )
    assert "Writable workspace dirs:" in sp
    assert "`scratch/`" in sp
    assert "`state/`" in sp
    assert "ephemeral" in sp.lower()
    assert "throwaway clones" in sp


def test_system_prompt_writable_dirs_absent_without_param():
    """Back-compat: no ``writable_dirs`` → no writable-dirs line, so
    existing callers and the prompt-cache prefix are unchanged."""
    sp = build_system_prompt(home_dir="/mimir-home")
    assert "## Agent home" in sp
    assert "Writable workspace dirs:" not in sp


def test_system_prompt_scratch_guidance_only_when_scratch_writable():
    """If an operator's MIMIR_FOLDERS drops ``scratch``, the prompt lists
    the writable dirs but omits the scratch steer (stays accurate)."""
    sp = build_system_prompt(
        home_dir="/mimir-home", writable_dirs=["state", "memory"],
    )
    assert "Writable workspace dirs:" in sp
    assert "`scratch/`" not in sp
    assert "throwaway clones" not in sp


def test_system_prompt_omits_agent_home_when_empty_string():
    """Empty string is falsy and reflects ``home_dir=""`` from a config
    that hasn't resolved a path. Treat as unset rather than emitting a
    misleading ``MIMIR_HOME=`` line."""
    sp = build_system_prompt(home_dir="")
    assert "## Agent home" not in sp
    assert "MIMIR_HOME=" not in sp


def test_system_prompt_agent_home_uses_provided_path_in_guidance():
    """The instruction text inside the section uses the *provided*
    path, not a hardcoded ``/mimir-home`` — so deployments that set
    MIMIR_HOME to anything else get accurate guidance."""
    sp = build_system_prompt(home_dir="/var/lib/mimir")
    # Both the env-var line AND the absolute-path-prefix instruction
    # should reflect the actual value.
    assert "MIMIR_HOME=/var/lib/mimir" in sp
    assert "/var/lib/mimir/" in sp
    assert "/mimir-home" not in sp


# ---- Config env wiring ---------------------------------------------------


def test_config_reads_operator_alert_channel_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "dm-discord-99")
    cfg = Config.from_env()
    assert cfg.operator_alert_channel == "dm-discord-99"


def test_config_operator_alert_channel_default_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_OPERATOR_ALERT_CHANNEL", raising=False)
    cfg = Config.from_env()
    assert cfg.operator_alert_channel == ""


# ---- Inbound attachments rendering ---------------------------------------


def test_turn_prompt_renders_inbound_attachments():
    """When the event carries attachment_names (set by bridges that
    download inbound files), the turn prompt body grows an
    ``Attachments:`` block listing each path so the agent can
    ``Read`` them."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="see attached",
        author="discord-99",
        author_display="alice",
        attachment_names=[
            "/home/mimir/attachments/inbound/discord/1/2-x-report.pdf",
            "/home/mimir/attachments/inbound/discord/1/2-y-chart.png",
        ],
    )
    prompt = build_turn_prompt(event)
    assert "see attached" in prompt
    assert "Attachments:" in prompt
    assert "report.pdf" in prompt
    assert "chart.png" in prompt


def test_turn_prompt_omits_attachments_section_when_empty():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="no files this time",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)
    assert "Attachments:" not in prompt


# ---- Inbound msg_id surfacing (so <react message="<id>"/> can target it) ----


def test_turn_prompt_includes_inbound_msg_id_in_header():
    """The Current-message header must surface ``msg_id: <id>`` when the
    inbound event carries a source_id, so the agent can target that
    message with ``<react message="<id>"/>`` instead of falling back to
    the just-sent assistant reply (memory/core/40-learned-behaviors.md).
    """
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
        author_display="alice",
        source_id="1234567890",
    )
    prompt = build_turn_prompt(event)
    assert "msg_id: 1234567890" in prompt
    # And it lives in the Current-message metadata bracket, not floating
    # somewhere else in the body.
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "msg_id: 1234567890" in header_line


def test_turn_prompt_omits_msg_id_when_source_id_missing():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="no id",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "msg_id" not in header_line


def test_turn_prompt_scheduled_tick_omits_msg_id():
    """Scheduled ticks have no inbound message — the synthetic header
    must not pretend otherwise."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="",
        source_id="should-be-ignored",
    )
    prompt = build_turn_prompt(event)
    assert "msg_id" not in prompt


# ---- saga_session_id surfacing for chainlink #23 #26 (Option P) ----


def test_turn_prompt_includes_saga_session_id_in_user_message_header():
    """chainlink #23 #26 Option P: the Current-message header must
    surface ``saga_session_id: <id>`` so the model can pass it as the
    ``session_id`` arg on saga_query / saga_store / saga_feedback /
    saga_mark_contributions tool calls. Without it the MCP handler's
    ctx-resolution chain can only fall through to single_active or
    missing — fragile under multi-channel concurrency."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
        source_id="msg-1",
    )
    prompt = build_turn_prompt(event, saga_session_id="saga-discord-1-abc123")
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "saga_session_id: saga-discord-1-abc123" in header_line


def test_turn_prompt_includes_saga_session_id_in_scheduled_tick_header():
    """Heartbeats and crons fire saga_query / saga_store too — the
    saga_session_id needs to surface for ticks not just user messages
    so heartbeat-driven tool calls can scope correctly."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="",
    )
    prompt = build_turn_prompt(event, saga_session_id="saga-scheduler-xyz")
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[scheduled_tick:")
    )
    assert "saga_session_id: saga-scheduler-xyz" in header_line


def test_turn_prompt_omits_saga_session_id_when_unset():
    """If no saga_session_id is passed (e.g. early bootstrap before
    SAGA registration), the header omits the field rather than
    rendering an empty/None value the model would echo verbatim."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)  # no saga_session_id kwarg
    assert "saga_session_id" not in prompt


# ---- shell_job_complete trigger rendering ----


def test_turn_prompt_renders_shell_job_complete_header():
    """The shell_job_complete branch must surface the job_id and
    exit_code in the header so the agent grep'ing events.jsonl can
    pivot from prompt → job-specific events without ambiguity."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="shell_job_complete",
        channel_id="discord-1",
        content="Shell job j_abcd1234 complete (status=exited_ok, exit_code=0).",
        extra={"job_id": "j_abcd1234", "exit_code": 0},
    )
    prompt = build_turn_prompt(event)
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[shell_job_complete:")
    )
    assert "job_id: j_abcd1234" in header_line
    assert "exit_code: 0" in header_line
    assert "discord-1" in header_line


def test_turn_prompt_shell_job_complete_renders_body_payload():
    """Body of the rendered prompt must contain the job-summary content
    set by Agent._on_shell_job_complete (status + tails)."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    body = "Shell job j_xyz complete.\n--- stdout tail ---\nhello\n--- stderr tail ---\n(empty)"
    event = AgentEvent(
        trigger="shell_job_complete",
        channel_id="discord-1",
        content=body,
        extra={"job_id": "j_xyz", "exit_code": 0},
    )
    prompt = build_turn_prompt(event)
    assert "stdout tail" in prompt
    assert "hello" in prompt


def test_turn_prompt_shell_job_complete_handles_missing_extra():
    """If extra is empty (legacy events?), the header still renders
    cleanly with placeholders rather than crashing."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="shell_job_complete",
        channel_id="discord-1",
        content="(no payload)",
    )
    prompt = build_turn_prompt(event)
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[shell_job_complete:")
    )
    assert "job_id: ?" in header_line  # placeholder when extra is absent
    assert "exit_code: None" in header_line


# ─── Per-section size breakdown (chainlink: 2026-05-10 operator request) ──


def test_turn_prompt_appends_section_sizes_to_resource_usage_block():
    """The per-section ~token breakdown lands inside the ``## Resource
    usage`` section so the agent can see which compartment is driving
    prompt growth without external instrumentation. Sections appear
    sorted by descending size; tiny sections (<25 tokens) are filtered
    out to keep the breakdown readable.
    """
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    # Big-ish blocks so the breakdown has interesting content.
    big_saga_block = "\n".join(f"- atom-{i}: " + "x" * 200 for i in range(10))
    medium_summaries = "\n".join(f"- session-{i}: " + "y" * 100 for i in range(5))
    small_feedback = "Negative (last 24h): - one\n- two"
    usage_block = (
        "Last turn: 100k prompt + 1k out tokens (cache hit 99%)\n\n"
        "Last 1h: $5.00 / 4 turns / cache hit 99%"
    )

    prompt = build_turn_prompt(
        event,
        saga_block=big_saga_block,
        session_summaries_block=medium_summaries,
        feedback_block=small_feedback,
        usage_block=usage_block,
    )

    # The breakdown is appended to the Resource usage block.
    assert "## Resource usage" in prompt
    assert "Section sizes (this prompt, ~tokens):" in prompt
    # Big saga block dominates — should land first.
    assert "- Possibly relevant memories (from SAGA):" in prompt
    # Medium summaries are present.
    assert "- Recent session summaries:" in prompt

    # Extract just the breakdown's own lines — the prompt continues
    # past it with a saga block whose entries also start with ``- ``,
    # so split on the next blank line after the breakdown header.
    breakdown_section = prompt.split(
        "Section sizes (this prompt, ~tokens):", 1
    )[1]
    # Breakdown ends at the next blank line followed by another section.
    breakdown_only = breakdown_section.split("\n\n", 1)[0]
    saga_idx = breakdown_only.find("Possibly relevant memories (from SAGA)")
    sums_idx = breakdown_only.find("Recent session summaries")
    assert saga_idx >= 0
    assert sums_idx >= 0
    assert saga_idx < sums_idx, (
        f"saga block ({saga_idx}) should land before session summaries "
        f"({sums_idx}) in size-sorted breakdown"
    )

    # Tiny sections (small_feedback is ~32 tokens including label/header;
    # may or may not pass the floor). Tight check: the breakdown only
    # includes the labeled blocks we passed in — no leakage into the
    # subsequent sections, no spurious entries.
    breakdown_bullets = [
        line for line in breakdown_only.splitlines()
        if line.strip().startswith("- ")
    ]
    assert 1 <= len(breakdown_bullets) <= 4, (
        f"breakdown bullet count should match the labeled sections we "
        f"provided (capped at 4); got {breakdown_bullets}"
    )
    # Every bullet should reference a known label (not, say, a saga atom).
    for bullet in breakdown_bullets:
        assert any(
            label in bullet
            for label in (
                "Possibly relevant memories",
                "Recent session summaries",
                "Recent feedback signals",
                "Resource usage",
            )
        ), f"unexpected breakdown bullet: {bullet}"


def test_turn_prompt_omits_section_breakdown_when_no_resource_usage_block():
    """If the caller didn't pass a ``usage_block``, there's no host
    section to attach the breakdown to. The breakdown is silently
    dropped (rather than landing in some other arbitrary block)."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    prompt = build_turn_prompt(
        event,
        saga_block="- atom-1: " + "x" * 500,
        # No usage_block.
    )
    assert "Section sizes" not in prompt


def test_format_section_sizes_floors_tiny_and_sorts_descending():
    """Direct test of the helper — easier to verify the sort order
    and the SMALL_TOKEN_FLOOR cutoff in isolation.
    """
    from mimir.prompts import _format_section_sizes

    sizes = {
        "Big": 12_000,    # 3000 tokens
        "Medium": 2_000,  # 500 tokens
        "Small": 60,      # 15 tokens — filtered
        "Tiny": 0,        # filtered
    }
    rendered = _format_section_sizes(sizes)
    assert "Section sizes (this prompt, ~tokens):" in rendered
    lines = rendered.splitlines()
    bullets = [line for line in lines if line.startswith("- ")]
    assert len(bullets) == 2  # Big + Medium
    assert bullets[0].startswith("- Big:")
    assert bullets[1].startswith("- Medium:")
    # Token formatting: 3000 → 3.0k, 500 → 500.
    assert "3.0k" in bullets[0]
    assert "500" in bullets[1]


def test_format_section_sizes_empty_returns_empty_string():
    from mimir.prompts import _format_section_sizes

    assert _format_section_sizes({}) == ""
    assert _format_section_sizes({"Tiny": 50}) == ""  # all filtered


# ── channel_memory_block injection (chainlink #187) ──────────────────────────


def test_turn_prompt_includes_channel_memory_block():
    """When channel_memory_block is set, ## Channel context section appears."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1500672382166110321",
        content="hello",
        author="discord-238367217903730690",
    )
    prompt = build_turn_prompt(
        event,
        channel_memory_block="Operator: Jason Carreira. Prefers direct answers.",
    )
    assert "## Channel context" in prompt
    assert "Jason Carreira" in prompt


def test_turn_prompt_omits_channel_context_when_none():
    """When channel_memory_block is None (synthetic or no files), section absent."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="tick",
        author="scheduler",
    )
    prompt = build_turn_prompt(event, channel_memory_block=None)
    assert "## Channel context" not in prompt


# ---- auto_skill_block — poller-channel skill surfacing (chainlink #212) -----


def test_turn_prompt_auto_skill_block_renders_labeled_section():
    """When ``auto_skill_block`` is provided, the turn prompt gains a
    ``## Skill: <name>`` section containing the body prose."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    skill_body = "# social-cli\n\nThe outbox + dispatch loop.\n"
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:social-cli-feed",
        content="new feed item",
    )
    prompt = build_turn_prompt(event, auto_skill_block=("social-cli", skill_body))
    assert "## Skill: social-cli" in prompt
    assert "outbox + dispatch loop" in prompt


def test_turn_prompt_renders_exact_autonomous_trigger_authority():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="tick",
    )
    prompt = build_turn_prompt(
        event,
        trigger_authority_profile="heartbeat",
        trigger_capability_tier="unbounded",
        trigger_capabilities=("worklink_run", "fetch_url", "read_file"),
    )

    assert "## Autonomous trigger authority" in prompt
    assert "profile: ``heartbeat``" in prompt
    assert "``fetch_url`` may reach only this profile's approved exact-URL list" in prompt
    assert "remains usable regardless of turn taint" in prompt
    assert "fetched responses are untrusted active ingest" in prompt
    assert "``web_search`` is not available to this trigger" in prompt
    assert "``worklink_run`` is usable only before any untrusted active ingest" in prompt


def test_turn_prompt_auto_skill_block_no_frontmatter(tmp_path):
    """End-to-end pin (chainlink #212): ``find_skill_for_channel`` strips YAML
    frontmatter before the body reaches ``build_turn_prompt``.  The
    rendered prompt must not expose any of the frontmatter fields
    (``name:``, ``description:``, ``trigger:``) under the Skill section.

    The test builds a real on-disk skill with YAML frontmatter and runs
    the full resolver → prompt pipeline to confirm the contract.
    """
    import json
    from pathlib import Path
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt
    from mimir.skill_resolver import find_skill_for_channel

    # Seed a minimal skill on disk with frontmatter + prose body.
    skill_dir = tmp_path / "social-cli"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        json.dumps({"pollers": [{"name": "social-cli-feed"}]}),
        encoding="utf-8",
    )
    skill_md_text = (
        "---\n"
        "name: social-cli\n"
        "description: Dispatch posts via the outbox pattern.\n"
        "trigger: Use when a social-cli-feed event arrives.\n"
        "---\n"
        "\n"
        "# social-cli\n"
        "\n"
        "The outbox + dispatch loop.\n"
    )
    (skill_dir / "SKILL.md").write_text(skill_md_text, encoding="utf-8")

    # Resolve the skill — body should already be stripped.
    result = find_skill_for_channel("poller:social-cli-feed", [tmp_path])
    assert result is not None, "skill should resolve for this poller channel"
    skill_name, skill_body = result

    # Frontmatter fields must NOT appear in the body the resolver returns.
    assert "name: social-cli" not in skill_body
    assert "description:" not in skill_body
    assert "trigger:" not in skill_body

    # Same must hold in the fully-rendered turn prompt.
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:social-cli-feed",
        content="new feed item",
    )
    prompt = build_turn_prompt(event, auto_skill_block=(skill_name, skill_body))
    assert "## Skill: social-cli" in prompt
    assert "outbox + dispatch loop" in prompt
    # Frontmatter YAML must not leak into the prompt.
    skill_section_start = prompt.index("## Skill: social-cli")
    skill_section = prompt[skill_section_start:]
    assert "name: social-cli" not in skill_section
    assert "description:" not in skill_section


# ─── chainlink #508: deliver: channel instruction ───────────────────


def test_build_turn_prompt_renders_deliver_section():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(trigger="poller", channel_id="poller:gmail", content="3 new threads")
    prompt = build_turn_prompt(event, deliver_channel="slack-ops")
    assert "## Delivery" in prompt
    assert "slack-ops" in prompt
    assert "send_message" in prompt
    assert "poller" in prompt  # trigger-aware wording


def test_build_turn_prompt_no_deliver_section_when_unset():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(trigger="poller", channel_id="poller:gmail", content="x")
    assert "## Delivery" not in build_turn_prompt(event)
