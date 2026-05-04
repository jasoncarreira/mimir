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
You communicate through channels (Slack, Discord, web, benchmark stdout).
You can use bash and file-op tools to organize your own notes under
memory/, search them via the file_search skill (covers state/ and
memory/ except memory/core/* — core blocks are already in this prompt),
and call SAGA through the saga skill for semantic memory."""

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
  tools exist.

## Replying to user messages

For chat channels (Discord, Slack, web), your final assistant text is
**auto-delivered** to the channel that sent the message. Just reply
naturally — the runtime parses any ``<actions>`` block out, sends the
remaining text to the user, and dispatches the directives. You do not
need to explicitly call the ``send_message`` tool for normal replies.

When auto-dispatch applies:
- ``user_message`` and ``react_received`` triggers on a registered
  chat bridge → final text is delivered.
- ``scheduled_tick`` (heartbeats, cron-fired turns) → final text is
  NOT auto-delivered. Heartbeats are explicitly silent; if you want
  to surface something to the operator, use the ``send_message`` tool
  on the operator alert channel.
- If you call ``send_message`` explicitly during the turn → the
  auto-dispatch is suppressed (the explicit call is the canonical
  delivery; auto-dispatch only kicks in when you didn't call it).

When to call ``send_message`` explicitly anyway:
- Cross-channel sends (different ``channel_id`` than the inbound).
- You want fine-grained chunking control (multiple separate Discord
  messages with different reactions).
- You want to send something *and then* keep working in the same turn
  without that subsequent work landing in the user's view.

## ``<actions>`` directives

Inside your reply text (auto-dispatch) or inside the ``send_message``
text body, you may embed an ``<actions>`` block to bundle reactions
and file sends into the same delivery. The runtime strips the block
from the user-visible text, then dispatches each directive in order.

```
Got it, here's the chart you asked for.

<actions>
  <react emoji="thumbsup" />
  <send-file path="charts/q3.png" caption="Q3 numbers" />
</actions>
```

- ``<react emoji="..." [message="<id>"] />`` — react with an emoji.
  Defaults to the message just delivered in this turn (or the most
  recent assistant message when the reply is directives-only).
- ``<send-file path="..." [caption="..."] [kind="image|file|audio"]
  [cleanup="true"] />`` — attach a file. ``path`` resolves under
  ``MIMIR_HOME/attachments/outbound/``; absolute paths must already be
  inside that dir. ``..`` and symlink escapes are rejected.
  ``cleanup="true"`` deletes the file after a successful send.

Per-directive failures show up in the next turn's prompt feedback
block; the main reply still goes out. Discord wants unicode glyphs
for reactions (``thumbsup`` shortcode does NOT work — use ``👍`` or
``thumbsup`` resolves via the alias table); Slack accepts the alias
form (``thumbsup`` without colons)."""


# VSM: algedonic (out) — operator alert channel. When MIMIR_OPERATOR_ALERT_CHANNEL
#                       is set, the system prompt teaches the agent the channel
#                       id to use for high-priority signals to the operator
#                       that don't fit the current conversation. The alert
#                       skill teaches WHEN to fire; this teaches WHERE.
# loop_id: 2.3
def build_system_prompt(
    *,
    persona: str | None = None,
    conventions: str | None = None,
    core_blocks: list[CoreBlock] | None = None,
    memory_index_body: str | None = None,
    operator_alert_channel: str = "",
    skill_block: str | None = None,
) -> str:
    """Assemble the system prompt. ``state/INDEX.md`` is intentionally absent —
    it's read on demand (SPEC §9.1).

    ``operator_alert_channel`` (v0.4 §6) — when set, append a one-line
    Operator config section so the agent knows the channel id to use for
    high-priority signals (the alert skill teaches *when*; this teaches
    *what*).

    ``skill_block`` (FUTURE_WORK §12.3) — when set, append a Skills
    section with success-rate-ordered listing. The block is rebuilt
    each turn (run_turn calls build_system_prompt every dispatch), so
    bucket assignment stays current as outcome aggregates evolve."""
    parts: list[str] = [persona or _DEFAULT_PERSONA]

    if core_blocks:
        rendered = render_core_section(core_blocks)
        if rendered:
            parts.append("## Core memory\n\n" + rendered)

    if memory_index_body:
        parts.append("## Memory index\n\n" + memory_index_body.rstrip())

    parts.append(conventions or _DEFAULT_CONVENTIONS)

    if skill_block:
        parts.append("## Skills\n\n" + skill_block.rstrip())

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
    upcoming_block: str | None = None,
    self_state_block: str | None = None,
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

    # Upcoming (FUTURE_WORK §12.1): feedforward — predictable events
    # the agent should know are coming (next-N scheduled ticks,
    # plan-window resets). Sits next to Resource usage because both
    # are self-state telemetry; placement above Recent activity so the
    # agent reads "what's coming" before "what just happened."
    if upcoming_block:
        sections.append("## Upcoming\n\n" + upcoming_block.rstrip())

    # Self-state (FUTURE_WORK §12.4): the homeostat's interpretation of
    # the four constraint layers (plan window / cost rate / S3-S4
    # share / tokens). Sits with the other self-state telemetry; the
    # agent should know the same constraints the arbiter uses to
    # suppress S4 work.
    if self_state_block:
        sections.append("## Self-state\n\n" + self_state_block.rstrip())

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

    # Prompt-header timestamp: defaults to now (the agent's view of "today")
    # but can be overridden by the inbound event via ``extra.event_ts_iso``.
    # Used by the integration bench (LongMemEval probes are dated 2023; the
    # agent computes "weeks ago" / "this year" against the question's
    # contemporaneous date, not the wall clock). Production agents always
    # see now() unless a bridge/handler explicitly sets the override.
    ts_override = (event.extra or {}).get("event_ts_iso")
    ts = ts_override if ts_override else datetime.now(tz=timezone.utc).isoformat()
    # ALWAYS surface "Today's date: YYYY-MM-DD" — bare ISO timestamps in
    # the event header alone get misread (haiku saw a 2023-04-01 ts: but
    # answered against the wall-clock 2026 because nothing told it that
    # ts WAS today). Production turns get current date; bench / replay
    # turns get the override they passed.
    date_only = ts.split("T", 1)[0]
    sections.append(f"## Today's date\n\n{date_only}")
    if event.trigger == "scheduled_tick":
        # Heartbeat / cron-fired tick: header omits author (the scheduler is
        # the implicit caller), body is whatever the schedule.yaml entry
        # provided as ``prompt:``, falling back to HEARTBEAT_DEFAULT_PROMPT
        # when the entry was a bare scheduled tick with no instructions.
        header = f"[scheduled_tick: {event.channel_id}, ts: {ts}]"
        body = event.content or HEARTBEAT_DEFAULT_PROMPT
        sections.append(f"{header}\n{body}")
    else:
        # Prefer the canonical's display name (or the event's author_display)
        # over the raw matching key in the header — the agent reads "alice",
        # not "discord-99".
        header_author = event.author_display
        if not header_author and resolver is not None and event.author:
            header_author = resolver.display_name(event.author)
        if not header_author:
            header_author = event.author or "-"
        # Highlight the current message — bordered separator + heading
        # so the agent reads "this is what I'm responding to" clearly,
        # vs. ambient context blocks above. Recent activity already
        # shows the message tail; this block is the *active* one.
        body = event.content or "(no content)"
        if event.attachment_names:
            paths = "\n".join(f"- {p}" for p in event.attachment_names)
            body = f"{body}\n\nAttachments:\n{paths}"
        sections.append(
            f"## ▶ Current message — respond to this\n\n"
            f"[event_kind: {event.trigger}, channel: {event.channel_id}, "
            f"author: {header_author}, ts: {ts}]\n\n"
            f"{body}"
        )

    return "\n\n".join(sections)
