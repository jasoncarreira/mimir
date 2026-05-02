"""System + turn prompt assembly (SPEC §9).

Phase 2 wires in:
- ``## Core memory`` — every ``memory/core/*.md`` block, lexicographic order
- ``## Memory index`` — ``memory/INDEX.md`` body (NOT the state index)
- ``## Recent activity`` — within-channel + cross-channel author pull (§5.4)

Tool catalog and skills catalog (§9.1 ``## Available tools`` / ``## Available
skills``) come in later phases when the registry is dynamic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .history import Message, render_identity_context, render_recent_activity
from .memory import CoreBlock, render_core_section
from .models import AgentEvent

_DEFAULT_PERSONA = """You are Mimir, a memory-centric agent built on the Claude Agent SDK.
You communicate through channels (Slack, Discord, Bluesky, web, benchmark
stdout). You can use bash and file-op tools to organize your own notes
under memory/, search them via the file_search skill, and call SAGA
through the saga skill for semantic memory."""

# Heartbeat-tick body when the scheduler.yaml job didn't set its own
# ``prompt:``. Brief — the heartbeat skill carries the full instructions.
HEARTBEAT_DEFAULT_PROMPT = """This is a heartbeat tick — autonomous-work cadence, not a user message.

Run the heartbeat skill: librarian protocol first (state coherence,
drift, re-anchor to current date), then pick ONE item from
state/heartbeat-backlog.md and do it. End the turn silently when done.
If something genuinely needs operator attention, route through the
operator alert channel; otherwise no user-visible message."""


_DEFAULT_CONVENTIONS = """## Conventions

- Always-in-context blocks live under memory/core/, ordered by numeric prefix
  (00-, 10-, 20-, ...). To insert at position N, name the file N-<topic>.md.
  Renumber with `mv` if gaps close.
- Anything else under memory/ is non-core: organize it however helps you.
  It is listed in memory/INDEX.md and is searchable via the file_search skill.
- Bulk verbatim content goes in state/. state/INDEX.md is NOT in the system
  prompt — read it directly with `read_file <home>/state/INDEX.md` when you
  want an overview, or use the file_search skill to find files by topic.
- Each file's first line should be: <!-- desc: short description -->.
  If absent, the indexes fall back to the file's first sentence and prefix
  the entry with [auto].
- The INDEX.md files are auto-generated; do not hand-edit them.
- Edit memory blocks with bash and file-op tools — no dedicated memory-block
  tools exist."""


def build_system_prompt(
    *,
    persona: str | None = None,
    conventions: str | None = None,
    core_blocks: list[CoreBlock] | None = None,
    memory_index_body: str | None = None,
    operator_alert_channel: str = "",
) -> str:
    """Assemble the system prompt. ``state/INDEX.md`` is intentionally absent —
    it's read on demand (SPEC §9.1).

    ``operator_alert_channel`` (v0.4 §6) — when set, append a one-line
    Operator config section so the agent knows the channel id to use for
    high-priority signals (the alert skill teaches *when*; this teaches
    *what*)."""
    parts: list[str] = [persona or _DEFAULT_PERSONA]

    if core_blocks:
        rendered = render_core_section(core_blocks)
        if rendered:
            parts.append("## Core memory\n\n" + rendered)

    if memory_index_body:
        parts.append("## Memory index\n\n" + memory_index_body.rstrip())

    parts.append(conventions or _DEFAULT_CONVENTIONS)

    if operator_alert_channel:
        parts.append(
            "## Operator config\n\n"
            f"Operator alert channel: {operator_alert_channel}"
        )

    return "\n\n".join(parts)


def build_turn_prompt(
    event: AgentEvent,
    *,
    recent_messages: Iterable[Message] | None = None,
    saga_block: str | None = None,
    subagent_block: str | None = None,
    recent_message_chars: int = 0,
    resolver: object | None = None,
    feedback_block: str | None = None,
    session_summaries_block: str | None = None,
    usage_block: str | None = None,
) -> str:
    """Assemble the turn prompt: known identities, recent activity, SAGA
    atom hits, subagent completion notifications (from prior turns), event
    header + body.

    ``recent_message_chars`` (>0) caps each Recent-activity message's
    rendered content with ``…[truncated]``; protects against single huge
    inbounds blowing the context (SPEC §5.4).

    ``resolver`` (FUTURE_WORK §6.1) — when present, identity records for
    any author in the recent window or the inbound event are surfaced as
    a 'Known identities' preamble at the top of the prompt, and the
    Recent activity render uses the canonical's display_name. None
    falls back to the no-identity-reconciliation rendering."""
    sections: list[str] = []

    # Materialize once if we need to scan it twice (identity + render).
    recent_list: list[Message] | None = None
    if recent_messages is not None:
        recent_list = list(recent_messages)

    if resolver is not None:
        identity_block = render_identity_context(
            recent_list or [], event.author, resolver
        )
        if identity_block:
            sections.append("## Known identities\n\n" + identity_block)

    # Algedonic channel (v0.4 §2): self-feedback signals between identities
    # and recent activity, so the agent reads its own pain/pleasure data
    # before it reads the conversation it's about to act on.
    if feedback_block:
        sections.append("## Recent feedback signals\n\n" + feedback_block.rstrip())

    # Recent session summaries (v0.4 §3): one rung wider than the message-
    # level recent activity. Placed before Recent activity so the agent
    # reads the session-level context first.
    if session_summaries_block:
        sections.append(
            "## Recent session summaries\n\n" + session_summaries_block.rstrip()
        )

    # Resource usage: cost / cache hit rate / context utilization across
    # rolling windows. Same priority as the algedonic feedback channel —
    # it's data about the agent's own state — placed near the top so the
    # agent reads it before the conversation it's about to act on.
    if usage_block:
        sections.append("## Resource usage\n\n" + usage_block.rstrip())

    if recent_list:
        rendered = render_recent_activity(
            recent_list, max_chars=recent_message_chars, resolver=resolver
        )
        if rendered:
            sections.append("## Recent activity\n\n" + rendered)

    if saga_block:
        sections.append("## Possibly relevant memories (from SAGA)\n\n" + saga_block.rstrip())

    if subagent_block:
        sections.append("## Subagent updates\n\n" + subagent_block.rstrip())

    ts = datetime.now(tz=timezone.utc).isoformat()
    if event.trigger == "scheduled_tick":
        # Heartbeat / cron-fired tick: header omits author (the scheduler is
        # the implicit caller), body is whatever the schedule.yaml entry
        # provided as ``prompt:``, falling back to HEARTBEAT_DEFAULT_PROMPT
        # when the entry was a bare scheduled tick with no instructions.
        header = f"[scheduled_tick: {event.channel_id}, ts: {ts}]"
        body = event.content or HEARTBEAT_DEFAULT_PROMPT
    else:
        # Prefer the canonical's display name (or the event's author_display)
        # over the raw matching key in the header — the agent reads "alice",
        # not "discord-99".
        header_author = event.author_display
        if not header_author and resolver is not None and event.author:
            header_author = resolver.display_name(event.author)
        if not header_author:
            header_author = event.author or "-"
        header = (
            f"[event_kind: {event.trigger}, channel: {event.channel_id}, "
            f"author: {header_author}, ts: {ts}]"
        )
        body = event.content or "(no content)"
    sections.append(f"{header}\n{body}")

    return "\n\n".join(sections)
