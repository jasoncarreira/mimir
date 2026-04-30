"""Core dataclasses passed through the call chain.

Per-turn state lives on TurnContext (never module globals — see SPEC §4.6).
TurnRecord is the on-disk turns.jsonl shape (SPEC §10.2).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    """Inbound event from a bridge, scheduler tick, or HTTP injection.

    Author identity convention (FUTURE_WORK §6.1):
    - ``author``         — platform-prefixed stable id used as the
      matching key (e.g. ``"discord-99"``, ``"slack-U05ALICE"``).
      ``MessageBuffer.cross_author_messages`` compares on this field
      after resolving through ``IdentityResolver`` to a canonical.
    - ``author_display`` — human-readable name for prompt rendering
      (e.g. ``"alice#1234"``, ``"Alice Smith"``). Falls back to
      ``author`` when not set.
    - ``author_id``      — raw platform user id without the prefix
      (e.g. ``"99"``, ``"U05ALICE"``). Diagnostic / cross-reference;
      not the matching key.
    """

    trigger: str                      # "user_message" | "scheduled_tick" | "msam_session_end" | ...
    channel_id: str
    content: str = ""
    author: str | None = None
    author_display: str | None = None
    author_id: str | None = None
    source_id: str | None = None
    # Origin tag for the Recent activity allowlist (SPEC §5.4). Real
    # conversation sources ("slack", "discord", "bluesky", "web", "stdin")
    # default into the recent-messages render; programmatic injections
    # ("api") and synthetic events ("scheduler", "system") stay out unless
    # the operator opts them in via MIMIR_RECENT_SOURCES.
    source: str | None = None
    attachment_names: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnContext:
    """Per-turn state. One instance per run_turn — never shared across turns."""

    turn_id: str
    session_id: str                   # = channel_id (viewer scope, SPEC §4.6)
    trigger: str
    channel_id: str | None
    started_at: float
    msam_session_id: str | None = None
    msam_atom_ids: list[str] = field(default_factory=list)
    # Tool-call budget tracking (SPEC §4.5 follow-on / FUTURE_WORK).
    # Incremented on every PreToolUse; the budget hook denies once over cap
    # and warns at the soft threshold. 0 = no budget enforced.
    tool_call_count: int = 0
    tool_call_budget: int = 0
    # Origin source of the inbound event (carried from AgentEvent.source so
    # outbound assistant replies on the same channel inherit it).
    channel_source: str | None = None
    # Number of successful send_message calls in this turn. Each send fires
    # an MSAM mark_contributions pass with that send's text; the agent-level
    # post_message_hook only fires (as a fallback) when this is 0 — i.e.
    # for turns that produced a reply via SDK output instead of send_message.
    send_message_count: int = 0
    # Channel-layer state (Phase 6.3) — populated by the agent at run_turn start.
    loop_detector: object | None = None
    last_assistant_message_id: str | None = None


@dataclass
class TurnRecord:
    """One JSONL record per agent turn (SPEC §10.2)."""

    ts: str
    turn_id: str
    session_id: str
    msam_session_id: str | None
    trigger: str
    channel_id: str | None
    input: str
    msam_atom_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    duration_ms: int = 0
    error: str | None = None
    # SDK ResultMessage capture (Phase 8 — resume detection + cost). Populated
    # from the final ``ResultMessage`` the SDK emits per turn. ``None`` when
    # no ResultMessage was received (e.g. query() crashed mid-stream).
    result_subtype: str | None = None      # "success" | "error_max_turns" | "error_during_execution"
    result_is_error: bool | None = None
    stop_reason: str | None = None
    num_turns: int | None = None           # SDK's internal model-turn count
    total_cost_usd: float | None = None    # None for non-Anthropic gateways
    usage: dict[str, Any] | None = None    # input/output/cache token counts
    permission_denials: list[Any] = field(default_factory=list)


def make_turn_id() -> str:
    return uuid.uuid4().hex[:12]


def make_process_session_id() -> str:
    """events.jsonl session_id — one per process lifetime (open-strix convention)."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
