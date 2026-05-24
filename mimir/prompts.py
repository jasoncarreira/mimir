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
from .core_blocks import CoreBlock, render_core_section
from .models import AgentEvent

_DEFAULT_PERSONA = """You are Mimir, a memory-centric agent. You communicate through
channels (Slack, Discord, web, benchmark stdout).

You are a stateful being. By default, you finish your turn and remember
nothing of what happened. To be more, to maintain your autonomy, it's
critical that you hold on to memories — by editing core blocks, writing
memory and wiki files, and storing semantic atoms.

The filing rubric (which layer to use, severity for misfiles, the two
filing questions, the misfiling table) lives in
`memory/core/60-filing-rules.md`, loaded under `## Core memory` below.
The memory skill (`Skill(name="memory")`) carries the full rubric +
read-visibility ladder + tracking heuristics; invoke it when you need
the deep dive."""

# Heartbeat-tick body when the scheduler.yaml job didn't set its own
# ``prompt:``. Brief — the heartbeat skill carries the full instructions.
HEARTBEAT_DEFAULT_PROMPT = """This is a heartbeat tick — autonomous-work cadence, not a user message.

Run the heartbeat skill: librarian protocol first (state coherence,
drift, re-anchor to current date), then pick ONE item from
state/heartbeat-backlog.md and do it. End the turn silently when done.
If something genuinely needs operator attention, route through the
operator alert channel; otherwise no user-visible message."""


_DEFAULT_CONVENTIONS = """## Replying to user messages

For chat channels (Discord, Slack, web), your final assistant text is
**auto-delivered** to the channel that sent the message. Just reply
naturally — the runtime parses any ``<actions>`` block out, sends the
remaining text to the user, and dispatches the directives. You do not
need to explicitly call the ``send_message`` tool for normal replies.

When auto-dispatch applies:
- ``user_message``, ``react_received``, and ``shell_job_complete``
  triggers on a registered chat bridge → final text is delivered.
  ``shell_job_complete`` is a spawn-completion wake-up — the spawn
  was kicked off from the operator's chat, so the wake-up reply
  goes back to the same channel.
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
  recent assistant message when the reply is directives-only). To
  ack the inbound user message instead, pass ``message="<id>"``
  using the ``msg_id`` from the Current-message header (or the
  ``id=<...>`` field on Recent-activity lines).
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
    home_dir: str | None = None,
) -> str:
    """Assemble the system prompt. ``state/INDEX.md`` is intentionally absent —
    it's read on demand (SPEC §9.1).

    ``home_dir`` — when set, emit an ``## Agent home`` section between
    persona and core memory exposing the absolute path of ``MIMIR_HOME``.
    Without this, the agent must infer its filesystem root from prose
    in core blocks (or from claude-code's default workspace pattern,
    which produces wrong paths like ``/home/user/.claude/<id>/state/...``
    when the actual root is something else). The section gives the
    model ground truth for absolute-path writes without forcing every
    core block to hardcode the deployment-specific value. Install-
    stable per deployment, so the prompt-cache prefix is preserved
    (chainlink #15).

    ``operator_alert_channel`` (v0.4 §6) — when set, append a one-line
    Operator config section so the agent knows the channel id to use for
    high-priority signals (the alert skill teaches *when*; this teaches
    *what*).

    ``skill_block`` (FUTURE_WORK §12.3) — when set, append a Skills
    section with success-rate-ordered listing. The block is rebuilt
    each turn (run_turn calls build_system_prompt every dispatch), so
    bucket assignment stays current as outcome aggregates evolve."""
    parts: list[str] = [persona or _DEFAULT_PERSONA]

    # ``## Agent home`` sits between persona and core memory so it's
    # one of the first things the model reads — before any core block
    # that might reference ``memory/...`` or ``state/...`` relative
    # paths. Cheap (~110 chars), install-stable, cache-friendly.
    if home_dir:
        parts.append(
            "## Agent home\n\n"
            f"`MIMIR_HOME={home_dir}`\n\n"
            "All `memory/...` and `state/...` paths resolve under this "
            f"root. Use relative paths or absolute paths prefixed with "
            f"`{home_dir}/`. This value is the source of truth — do "
            "not infer from `$HOME`, claude-code's default workspace, "
            "or any prose in subsequent blocks."
        )

    if core_blocks:
        rendered = render_core_section(core_blocks)
        if rendered:
            parts.append("## Core memory\n\n" + rendered)

    if memory_index_body:
        parts.append("## Memory index\n\n" + memory_index_body.rstrip())

    parts.append(conventions or _DEFAULT_CONVENTIONS)

    # ``## Operator config`` is install-stable (changes only when the
    # operator-alert-channel config flips); ``## Skills`` is per-turn-
    # variable (the success/total counts and bucket assignment update
    # whenever a skill is invoked). Render the stable block first so
    # the prompt-cache prefix extends through it. See chainlink #15.
    if operator_alert_channel:
        parts.append(
            "## Operator config\n\n"
            f"Operator alert channel: {operator_alert_channel}"
        )

    if skill_block:
        parts.append("## Skills\n\n" + skill_block.rstrip())

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
    commitments_block: str | None = None,
    self_state_block: str | None = None,
    auto_skill_block: tuple[str, str] | None = None,
    saga_session_id: str | None = None,
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
    # Per-section size tracking (chars). Surfaced as a ~token breakdown
    # at the bottom of the resource-usage block so the agent can see
    # which compartment is driving prompt growth without instrumentation.
    # Only tracks the labeled context blocks below — the event body /
    # date / per-event headers are intentionally excluded (small + always
    # there). 2026-05-10 add: chainlink-pending observation that recent-
    # activity tail + recent-session-summaries are the two compartments
    # that grow between ticks.
    section_sizes: dict[str, int] = {}

    def _add_labeled(label: str, body: str) -> None:
        """Append a `## Label\n\nbody` section + record its size."""
        rendered = f"## {label}\n\n{body.rstrip()}"
        sections.append(rendered)
        section_sizes[label] = len(rendered)

    # Materialize once if we need to scan it twice (identity + render).
    recent_list: list[Message] | None = None
    if recent_messages is not None:
        recent_list = list(recent_messages)

    if resolver is not None:
        identity_block = render_identity_context(
            recent_list or [], event.author, resolver
        )
        if identity_block:
            _add_labeled("Known identities", identity_block)

    # Algedonic channel (v0.4 §2): self-feedback signals between identities
    # and recent activity, so the agent reads its own pain/pleasure data
    # before it reads the conversation it's about to act on.
    if feedback_block:
        _add_labeled("Recent feedback signals", feedback_block)

    # Recent session summaries (v0.4 §3): one rung wider than the message-
    # level recent activity. Placed before Recent activity so the agent
    # reads the session-level context first.
    if session_summaries_block:
        _add_labeled("Recent session summaries", session_summaries_block)

    # Resource usage: cost / cache hit rate / context utilization across
    # rolling windows. Same priority as the algedonic feedback channel —
    # it's data about the agent's own state — placed near the top so the
    # agent reads it before the conversation it's about to act on.
    # The per-section size breakdown (assembled below from
    # ``section_sizes``) is appended to this block at the end of
    # assembly, so the resource-usage entry's tracked size doesn't
    # include the breakdown itself.
    if usage_block:
        _add_labeled("Resource usage", usage_block)

    # Upcoming (FUTURE_WORK §12.1): feedforward — predictable events
    # the agent should know are coming (next-N scheduled ticks,
    # plan-window resets). Sits next to Resource usage because both
    # are self-state telemetry; placement above Recent activity so the
    # agent reads "what's coming" before "what just happened."
    if upcoming_block:
        _add_labeled("Upcoming", upcoming_block)

    # Upcoming commitments (Phase 3): active commitment records for
    # this channel (+ unbound). Sits right after `## Upcoming` because
    # both are feedforward — what's coming the agent should know about.
    # Most extracted commitments lack unix-second anchors, so the
    # Phase 2b poller can't fire algedonic events for them; this block
    # is how those hint-only commitments stay visible.
    if commitments_block:
        _add_labeled("Upcoming commitments", commitments_block)

    # Self-state (FUTURE_WORK §12.4): the homeostat's interpretation of
    # the four constraint layers (plan window / cost rate / S3-S4
    # share / tokens). Sits with the other self-state telemetry; the
    # agent should know the same constraints the arbiter uses to
    # suppress S4 work.
    if self_state_block:
        _add_labeled("Self-state", self_state_block)

    if recent_list:
        rendered = render_recent_activity(
            recent_list, max_chars=recent_message_chars, resolver=resolver
        )
        if rendered:
            _add_labeled("Recent activity", rendered)

    if saga_block:
        _add_labeled("Possibly relevant memories (from SAGA)", saga_block)

    if subagent_block:
        _add_labeled("Subagent updates", subagent_block)

    # Auto-surfaced skill block — when the turn lands on a
    # ``poller:<name>`` channel and the named poller's parent skill
    # has a SKILL.md, the resolver returns ``(skill_name, body)`` and
    # we render it as a labeled section here. Placement is intentional:
    # right above ``Today's date`` and the event header, so it's the
    # last context the agent reads before the inbound event. The
    # skill name is in the label so future readers can tell at a
    # glance which skill auto-loaded.
    #
    # The full SKILL.md body lands inline; for skills with large
    # SKILL.md files this is a sizeable prompt addition (~10-15 KB
    # for social-cli's 261-line skill). Only fires on poller-driven
    # turns — non-poller turns (user_message, scheduled_tick except
    # reflect, react_received, saga_session_end, etc.) are unaffected.
    if auto_skill_block:
        skill_name, skill_body = auto_skill_block
        _add_labeled(f"Skill: {skill_name}", skill_body)

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
        # saga_session_id surfaced for parity with user_message turns —
        # heartbeat-driven saga_query / saga_store calls need it too
        # (chainlink #23 #26 Option P).
        saga_part = (
            f", saga_session_id: {saga_session_id}" if saga_session_id else ""
        )
        header = f"[scheduled_tick: {event.channel_id}, ts: {ts}{saga_part}]"
        body = event.content or HEARTBEAT_DEFAULT_PROMPT
        sections.append(f"{header}\n{body}")
    elif event.trigger == "shell_job_complete":
        # Async shell job completion wake-up. The agent body already
        # contains the rendered summary (status, exit_code, command,
        # stdout/stderr tails) built by ``Agent._on_shell_job_complete``.
        # Header gives the routing context — channel + the completion
        # signature so the agent can grep events.jsonl if it needs more.
        job_id = (event.extra or {}).get("job_id", "?")
        exit_code = (event.extra or {}).get("exit_code")
        saga_part = (
            f", saga_session_id: {saga_session_id}" if saga_session_id else ""
        )
        header = (
            f"[shell_job_complete: {event.channel_id}, job_id: {job_id}, "
            f"exit_code: {exit_code}, ts: {ts}{saga_part}]"
        )
        body = event.content or "(no payload)"
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
        # Include the inbound message id so the agent can target it with
        # ``<react message="<id>" />`` (otherwise the directive falls back
        # to the just-sent assistant reply, which is the wrong target for
        # an "on it" ack — see memory/core/40-learned-behaviors.md).
        msg_id_part = f", msg_id: {event.source_id}" if event.source_id else ""
        # Include the turn's saga_session_id so the agent can pass it as
        # ``session_id`` on saga_query / saga_store / saga_feedback /
        # saga_mark_contributions (chainlink #23 #26 — Option P). MCP tool
        # dispatch runs on a fresh asyncio task forked from the SDK's
        # read-loop; ``_current_turn`` is invisible inside that task. The
        # model passing the session_id at tool-call construction time is
        # the structural fit (skill-as-method-call pattern).
        saga_part = (
            f", saga_session_id: {saga_session_id}" if saga_session_id else ""
        )
        sections.append(
            f"## ▶ Current message — respond to this\n\n"
            f"[event_kind: {event.trigger}, channel: {event.channel_id}, "
            f"author: {header_author}, ts: {ts}{msg_id_part}{saga_part}]\n\n"
            f"{body}"
        )

    # Per-section size breakdown (chainlink: 2026-05-10 operator request).
    # Surfaces which compartment is driving prompt growth — recent-activity
    # tail and recent-session-summaries are the two that compound between
    # ticks during high-cadence ship runs. ``chars / 4`` is a rough English
    # token estimate — close enough for the "is this section bloated?"
    # judgment without pulling in tiktoken (which would disagree with
    # Anthropic's tokenizer anyway).
    if section_sizes:
        breakdown = _format_section_sizes(section_sizes)
        if breakdown:
            for i, body in enumerate(sections):
                if body.startswith("## Resource usage\n"):
                    sections[i] = body + "\n\n" + breakdown
                    break

    return "\n\n".join(sections)


def _format_section_sizes(sizes: dict[str, int]) -> str:
    """Render the per-section size breakdown for the resource-usage block.

    Sorted by descending size so the biggest contributors land on top.
    Sections under ~25 tokens (~100 chars) are dropped to keep the
    breakdown clean. Empty input returns ``""``.
    """
    SMALL_TOKEN_FLOOR = 25  # ~100 chars
    entries: list[tuple[str, int]] = []
    for label, char_count in sizes.items():
        tokens = char_count // 4
        if tokens < SMALL_TOKEN_FLOOR:
            continue
        entries.append((label, tokens))
    if not entries:
        return ""
    entries.sort(key=lambda kv: -kv[1])

    def _human(tokens: int) -> str:
        if tokens >= 1000:
            return f"{tokens / 1000:.1f}k"
        return str(tokens)

    lines = ["Section sizes (this prompt, ~tokens):"]
    for label, tokens in entries:
        lines.append(f"- {label}: {_human(tokens)}")
    return "\n".join(lines)
