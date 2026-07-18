"""Core dataclasses passed through the call chain.

Per-turn state lives on TurnContext (never module globals — see SPEC §4.6).
TurnRecord is the on-disk turns.jsonl shape (SPEC §10.2).
"""

from __future__ import annotations

import time
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnInteractivity(StrEnum):
    """Server-owned interactivity classification for a turn."""

    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non_interactive"


class FlowLabel(StrEnum):
    """Immutable information flow control labels (chainlink #871).

    These labels track data sensitivity through the turn lifecycle. Labels
    are monotonic - they can only be added, never removed (except via
    explicit audited admin declassification). This ensures that private/
    confidential data cannot leak to incompatible sinks.
    """

    PRIVATE = "private"
    CONFIDENTIAL = "confidential"
    INTERNAL = "internal"
    PUBLIC = "public"


@dataclass(frozen=True)
class SourceLabel:
    """Server-authoritative provenance for one protected input.

    ``authorized_principals`` is the effective read ACL. Derived service data
    must carry the intersection of its inputs' ACLs; an empty ACL is unknown,
    not public. All identity fields are required for ordinary channel egress.
    """

    principal: str | None
    domain: str | None
    resource_id: str | None
    bridge_instance: str | None
    sensitivity: str
    authorized_principals: frozenset[str] = frozenset()
    source_kind: str = "channel"

    @property
    def is_complete(self) -> bool:
        return bool(
            self.principal
            and self.domain
            and self.resource_id
            and self.bridge_instance
            and self.sensitivity in FlowLabel._value2member_map_
            and self.authorized_principals
        )

    @classmethod
    def derived(
        cls,
        inputs: frozenset["SourceLabel"],
        *,
        principal: str,
        domain: str,
        resource_id: str,
        bridge_instance: str,
        sensitivity: str,
        source_kind: str = "service",
    ) -> "SourceLabel":
        """Create service-derived provenance with the inputs' intersected ACL."""
        acl: frozenset[str] = frozenset()
        if inputs and all(source.is_complete for source in inputs):
            iterator = iter(inputs)
            acl = next(iterator).authorized_principals
            for source in iterator:
                acl &= source.authorized_principals
        return cls(
            principal=principal,
            domain=domain,
            resource_id=resource_id,
            bridge_instance=bridge_instance,
            sensitivity=sensitivity,
            authorized_principals=acl,
            source_kind=source_kind,
        )


@dataclass(frozen=True)
class InformationFlowLabels:
    """Immutable/monotonic information flow control labels (chainlink #871).

    Tracks data sensitivity from various sources to enforce the sink gate.
    Labels are monotonic - they can only be added, never removed except via
    explicit audited admin declassification action. Unknown labels fail closed.

    Sources:
    - inbound/folded messages
    - recent history
    - automatic memory/session/skill/file injection
    - attachments
    - continuation context
    - protected/partial tool/subagent results
    """

    labels: frozenset[str] = frozenset()
    source_channels: frozenset[str] = frozenset()
    sources: frozenset[SourceLabel] = frozenset()
    created_at: float = field(default_factory=time.monotonic)

    def with_label(self, label: str) -> "InformationFlowLabels":
        """Return new instance with added label (monotonic - only adds)."""
        if label in self.labels:
            return self
        return InformationFlowLabels(
            labels=self.labels | frozenset({label}),
            source_channels=self.source_channels,
            sources=self.sources,
            created_at=self.created_at,
        )

    def with_channel(self, channel: str) -> "InformationFlowLabels":
        """Return new instance with added source channel."""
        if channel in self.source_channels:
            return self
        return InformationFlowLabels(
            labels=self.labels,
            source_channels=self.source_channels | frozenset({channel}),
            sources=self.sources,
            created_at=self.created_at,
        )

    def with_source(self, source: SourceLabel) -> "InformationFlowLabels":
        """Return a carrier with one immutable source record added."""
        if source in self.sources:
            return self
        channels = self.source_channels
        if source.source_kind == "channel" and source.resource_id:
            channels |= frozenset({source.resource_id})
        return InformationFlowLabels(
            labels=self.labels | frozenset({source.sensitivity}),
            source_channels=channels,
            sources=self.sources | frozenset({source}),
            created_at=self.created_at,
        )

    def can_flow_to(self, sink: str, allowed_sinks: frozenset[str]) -> bool:
        """Check if labels permit flow to the given sink.

        Unknown labels fail closed (deny). Unknown sinks fail closed (deny).
        Same-principal/same-channel flows pass only when every label is
        destination-compatible.
        """
        if not self.labels:
            return True
        if not allowed_sinks:
            return False
        for label in self.labels:
            if label not in ("private", "confidential", "internal", "public"):
                return False
        return sink in allowed_sinks or "*" in allowed_sinks


@dataclass
class InformationFlowState:
    """Turn-local monotonic IFC state shared by frozen runtime carriers."""

    labels: InformationFlowLabels | None = None
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def current(self, fallback: InformationFlowLabels | None = None) -> InformationFlowLabels | None:
        with self._lock:
            return self.labels if self.labels is not None else fallback

    def merge(
        self,
        added: InformationFlowLabels,
        fallback: InformationFlowLabels | None = None,
    ) -> InformationFlowLabels:
        """Atomically union labels so concurrent tool results cannot attenuate state."""
        with self._lock:
            current = self.labels if self.labels is not None else fallback
            merged = InformationFlowLabels()
            for carrier in (current, added):
                if not isinstance(carrier, InformationFlowLabels):
                    continue
                for label in carrier.labels:
                    merged = merged.with_label(label)
                for channel in carrier.source_channels:
                    merged = merged.with_channel(channel)
                for source in carrier.sources:
                    merged = merged.with_source(source)
            self.labels = merged
            return merged


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

    trigger: str                      # "user_message" | "scheduled_tick" | "saga_session_end" | ...
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
    # Server-owned service identity. Only trusted internal event constructors
    # set this field; generic HTTP ingress deliberately never copies it from a
    # request body. ``create_auth_context`` validates it against the registered
    # principal for ``trigger`` before granting service authority.
    service_principal: str | None = None
    # Server-carried IFC state for continuations/resumed events. This must be
    # propagated from a trusted TurnContext; generic ingress must not accept a
    # client assertion as a declassification or authority signal.
    ifc_labels: "InformationFlowLabels | None" = None
    # ACL accumulated from the authoritative turns in a completed channel
    # session. Only the server-owned synthesis constructor sets this carrier.
    source_session_acl: "SessionACL | None" = None


@dataclass(frozen=True)
class SessionACL:
    """Immutable, monotonically intersected ACL for synthesis outputs."""

    owner_principal: str = "legacy_admin"
    origin_channel: str | None = None
    origin_domain: str | None = None
    visibility: str = "legacy_admin"
    provenance_complete: bool = False

    @classmethod
    def from_auth_context(
        cls,
        auth_context: "AuthContext | None",
        *,
        origin_domain: str | None,
        visibility: str,
    ) -> "SessionACL":
        if auth_context is None:
            return cls()
        owner = auth_context.canonical_principal or auth_context.principal
        channel = auth_context.channel_id
        if not owner or not channel or not origin_domain:
            return cls()
        if auth_context.is_service:
            owner = f"service:{owner}"
            visibility = "service"
        if visibility not in {"public", "private", "service"}:
            return cls()
        return cls(
            owner_principal=owner,
            origin_channel=channel,
            origin_domain=origin_domain,
            visibility=visibility,
            provenance_complete=True,
        )

    def intersect(self, other: "SessionACL") -> "SessionACL":
        """Return a no-wider ACL; ambiguous provenance permanently fails closed."""
        if not self.provenance_complete or not other.provenance_complete:
            return SessionACL()
        if (
            self.owner_principal != other.owner_principal
            or self.origin_channel != other.origin_channel
            or self.origin_domain != other.origin_domain
        ):
            return SessionACL()
        rank = {"public": 0, "private": 1, "service": 2, "legacy_admin": 3}
        visibility = max(
            (self.visibility, other.visibility), key=lambda value: rank.get(value, 3)
        )
        return SessionACL(
            owner_principal=self.owner_principal,
            origin_channel=self.origin_channel,
            origin_domain=self.origin_domain,
            visibility=visibility,
            provenance_complete=True,
        )


@dataclass(frozen=True)
class AuthContext:
    """Frozen, server-created authorization context (chainlink #864).

    This context carries immutable authorization state from the server's ingress
    point through the entire turn execution. It is created BEFORE model execution
    and CANNOT be widened or mutated by the model, tools, or downstream handlers.

    The key invariant: authority is derived ONLY from this frozen carrier, NOT from:
    - Model-passed session_id
    - ContextVar fallback heuristics
    - Single-active-turn heuristics

    Fields are immutable (frozen=True) to prevent post-creation widening.

    The ifc_labels field carries per-turn IFC labels on the durable carrier,
    ensuring sink-flow checks survive forked SDK/MCP tasks where the
    _current_turn ContextVar is lost (chainlink #891).
    """

    principal: str | None
    canonical_principal: str | None
    roles: tuple[str, ...]
    event_ingress: str | None
    trigger: str
    channel_id: str | None
    interactivity: "TurnInteractivity | None"
    policy_version: str | None = None
    is_service: bool = False
    enforcement_enabled: bool = False
    ifc_labels: "InformationFlowLabels | None" = None
    domain: str | None = None
    resource_id: str | None = None
    bridge_instance: str | None = None
    # Mutable only through its monotonic merge API. Keeping this cell on the
    # frozen carrier lets later forked requests observe post-tool taint without
    # making identity, roles, or any authority field mutable.
    ifc_state: InformationFlowState = field(
        default_factory=InformationFlowState, repr=False, compare=False,
    )
    # Resource ACL for outputs derived by a trusted synthesis turn. This does
    # not grant execution authority; it only attenuates durable output scope.
    source_session_acl: SessionACL | None = None


@dataclass
class TurnContext:
    """Per-turn state. One instance per run_turn — never shared across turns."""

    turn_id: str
    session_id: str                   # = channel_id (viewer scope, SPEC §4.6)
    trigger: str
    channel_id: str | None
    started_at: float
    # Logical agent name — sourced from ``Config.agent_id`` at run_turn
    # entry. Threaded into TurnRecord + emitted with every event so a
    # cross-process operator running two agents on the same hardware
    # (each in its own process) can filter the merged log streams by
    # agent. ``None`` only in tests that construct TurnContext directly
    # without going through Agent.
    agent_id: str | None = None
    saga_session_id: str | None = None
    saga_atom_ids: list[str] = field(default_factory=list)
    # chainlink #266 slice 6: skill-learning atom IDs injected into this
    # turn's prompt (poller auto_skill_block + non-poller read_file
    # middleware). run_turn folds these into the TurnRecord's
    # ``saga_atom_ids`` so the session-boundary synthesis turn votes them
    # via saga_feedback — but deliberately NOT into the per-turn
    # auto-feedback credit pass, which writes a weight-2.0 boost on every
    # cited atom each successful turn and would inflate every injected
    # learning uniformly (defeating activation ranking). Populated
    # best-effort; empty when no skill loads this turn.
    injected_skill_atom_ids: list[str] = field(default_factory=list)
    # Tool-call budget tracking (SPEC §4.5 follow-on / FUTURE_WORK).
    # Incremented on every ALLOWED PreToolUse; the budget hook denies
    # once at-cap (without incrementing) and warns once when the soft
    # threshold is first crossed. 0 = no budget enforced.
    tool_call_count: int = 0
    tool_call_budget: int = 0
    # Durable hard-denial markers for continuation/recovery paths.
    # Populated only when a NON-exempt tool is refused at/over the cap;
    # allowed calls leave them untouched. ``first_denied_at_count`` records
    # the already-used count seen at the first refusal (normally == budget
    # because denied calls do not increment ``tool_call_count``).
    tool_call_budget_exhausted: bool = False
    tool_call_budget_denied_count: int = 0
    tool_call_budget_denied_tools: list[str] = field(default_factory=list)
    tool_call_budget_first_denied_at_count: int | None = None
    # CR2 (agent runtime) fix: soft-warning idempotency. Without this
    # flag, the previous ``count == soft_threshold`` trigger could miss
    # a warning if any code path skipped an increment, AND could fire
    # repeatedly if a future change ever decremented the count. One-shot
    # flag means the warning fires exactly once per turn at the first
    # crossing.
    _tool_call_soft_warning_emitted: bool = False
    # chainlink #511: per-turn model-iteration ceiling — 3-tier, belt-and-
    # suspenders alongside the tool-call budget + homeostat.
    # ``IterationGateMiddleware`` counts model iterations (before_model) and
    # escalates: 75% gentle wrap-up nudge (no event), 90% urgent nudge (+event),
    # 100% hard stop (force-end the turn + event). Each one-shot flag fires its
    # tier exactly once. ``iteration_hard_stopped`` tells ``run_turn`` to send a
    # cap notice to the channel (the model never got to deliver). 0 = disabled.
    iteration_count: int = 0
    iteration_budget: int = 0
    _iteration_warn_75_emitted: bool = False
    _iteration_warn_90_emitted: bool = False
    _iteration_cap_emitted: bool = False
    iteration_hard_stopped: bool = False
    # Origin source of the inbound event (carried from AgentEvent.source so
    # outbound assistant replies on the same channel inherit it).
    channel_source: str | None = None
    # Runtime access-control context for tool middleware. Populated by
    # Agent.run_turn from the inbound AgentEvent and Config/IdentityResolver.
    author: str | None = None
    identity_resolver: Any | None = None
    access_control_enforced: bool = False
    # Frozen authorization context (chainlink #864). Created at ingress before
    # model execution and supplied as LangGraph runtime context for ordinary,
    # built-in, and wrapped MCP tools. Immutable - authority derives ONLY from
    # this carrier, NOT from model session_id, ContextVar fallback, or
    # single-active-turn heuristics.
    auth_context: AuthContext | None = None
    # Information flow control labels (chainlink #871). Immutable/monotonic
    # labels tracking data sensitivity from various sources. Initialized before
    # the first model call from inbound/folded messages, recent history,
    # automatic memory/session/skill/file injection, attachments, and
    # continuation context. Propagated to subagents, spawns, continuations,
    # and resumed turns. Blocked at incompatible sinks.
    ifc_labels: InformationFlowLabels | None = None
    # Number of successful send_message deliveries in this turn (incremented
    # only after the bridge confirms ``SendResult.sent``). The forgot-to-send
    # guard emits ``interactive_turn_no_send_message`` when an interactive turn
    # produced final text but this is still 0 — i.e. the reply never went out
    # (0.3.0: send_message is the sole delivery path).
    send_message_count: int = 0
    # Number of successful react tool calls this turn. A react is a valid
    # interactive response (an acknowledgment), so the forgot-to-send guard
    # treats react_count > 0 the same as a delivered send_message — otherwise
    # a react-only reply gets falsely flagged as "no reply" (0.3.2).
    react_count: int = 0
    # Channels that received a CONFIRMED delivery this turn (send_message
    # with SendResult.sent, or a confirmed react — tool or directive).
    # chainlink #423: the forgot-to-send guard is channel-scoped — an
    # interactive turn must deliver to the TRIGGERING channel; a
    # cross-channel send (e.g. an ops-channel alert) doesn't count as
    # replying to the user who asked. The plain counters above stay for
    # observability; this set is what the guard reads.
    delivered_channel_ids: set = field(default_factory=set)
    # Channel-layer state (Phase 6.3) — populated by the agent at run_turn start.
    loop_detector: object | None = None
    last_assistant_message_id: str | None = None
    # Synthesis-turn observability (CR#19). The synthesis prompt instructs
    # the agent to call ``saga_end_session`` (step 3); this flag flips True
    # in the tool handler on success. The agent's post-message hook checks
    # it at synthesis-turn end and emits ``saga_synthesis_skipped_boundary``
    # when False, so silent contract failures (agent didn't follow step 3)
    # become a visible algedonic signal instead of empty session-summary
    # blocks for the next session.
    saga_end_session_called: bool = False
    # Subagent task descriptions captured during the SDK message loop
    # (CR#15). ``TaskStartedMessage`` writes here; ``TaskNotificationMessage``
    # reads to populate the inbox push's ``description`` field. Lives on
    # the ctx (not on the SubagentLifecycleHook) so concurrent turns on
    # different channels don't share state.
    task_descriptions: dict[str, str] = field(default_factory=dict)
    # WikiBacklinksHook snapshot: ``{absolute_page_path: st_mtime}`` taken
    # at ``pre_query``, compared at ``finalize`` to detect which wiki
    # pages were modified during the turn. Same multi-channel-safety
    # rationale as task_descriptions. Empty dict when the hook didn't
    # populate it (e.g. tests that drive ``finalize`` directly).
    wiki_mtime_snapshot: dict[str, float] = field(default_factory=dict)
    # Per-turn saga call audit log. Populated by the
    # ``RecordingSagaClient`` wrapper around every saga method invocation
    # (query / store / feedback / mark_contributions / end_session /
    # contextual rewrite). Surfaces in turns.jsonl so the turn viewer
    # can show "what saga did this turn" without joining to events.jsonl.
    # Each entry: ``SagaCallRecord`` (call type, args summary, result
    # summary, latency_ms, error). Empty when no saga calls fired (e.g.
    # synthetic ticks with no inbound, scheduled callables).
    saga_calls: list[SagaCallRecord] = field(default_factory=list)
    # Durable server-owned ingress provenance copied from ``AgentEvent.extra``
    # when present (for example generic HTTP ``POST /event`` stamping). Tool
    # middleware reads this instead of trusting client-controlled trigger /
    # source / author fields for admin-sensitive decisions.
    event_ingress: str | None = None
    # Server-owned turn classification. Optional so older call sites and
    # fail-closed guards can distinguish "not classified yet" from an explicit
    # interactive/non-interactive decision.
    interactivity: TurnInteractivity | None = None


@dataclass
class SagaCallRecord:
    """One saga API call captured during a turn.

    Recorded by ``RecordingSagaClient`` (mimir/saga_client.py) which
    wraps the underlying ``SagaStore`` and appends
    to ``TurnContext.saga_calls`` on every method invocation. The
    rollup writes these into ``turns.jsonl`` so the turn viewer can
    display saga's per-turn behavior inline without joining to
    events.jsonl.

    Field rationale:
    - ``call_type`` — saga method name (``query`` / ``store`` /
      ``feedback`` / ``mark_contributions`` / ``end_session`` /
      ``rewrite``). ``rewrite`` is the contextual-rewrite path that
      fires inside ``query`` when a non-empty ``context`` is passed.
    - ``args`` — input summary as a JSON-able dict. Strings are
      truncated to 200 chars to bound row size. Full content lives
      in events.jsonl if needed.
    - ``result`` — output summary (atom IDs retrieved, atom ID stored,
      etc.). Bounded for the same reason.
    - ``latency_ms`` — wall-clock duration of the call.
    - ``t_ms`` — wall-clock offset from ``ctx.started_at`` to the
      moment the call STARTED (not finished). Lets the turn viewer
      interleave saga calls with SDK events on a single chronological
      timeline. ``None`` when the recorder couldn't resolve the active
      ctx (e.g. saga calls fired outside any turn — consolidation cron,
      decay sweeps).
    - ``error`` — exception message if the call raised, else ``None``.
      An errored call still produces a record so the turn viewer can
      surface failures.
    """

    call_type: str
    args: dict
    result: dict
    latency_ms: float
    error: str | None = None
    t_ms: float | None = None

    def to_dict(self) -> dict:
        out = {
            "call_type": self.call_type,
            "args": self.args,
            "result": self.result,
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.t_ms is not None:
            out["t_ms"] = round(self.t_ms, 2)
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class TurnRecord:
    """One JSONL record per agent turn (SPEC §10.2)."""

    ts: str
    turn_id: str
    session_id: str
    saga_session_id: str | None
    trigger: str
    channel_id: str | None
    input: str
    # Logical agent name — sourced from ``Config.agent_id``. Tagging
    # every turn record lets a cross-process operator running two
    # agents filter merged turns.jsonl output by agent without grepping
    # by MIMIR_HOME path. ``None`` on records written by code paths
    # predating this field — the turn viewer treats absent agent_id as
    # "unknown / single-agent legacy run".
    agent_id: str | None = None
    # Monotonically increasing turn sequence number, assigned by TurnLogger on
    # write. Survives retention trimming (the high-water mark is re-seeded from
    # the newest retained record), so the latest record's ``seq`` is the running
    # turn total surfaced in the web dossier. ``None`` on legacy records until
    # TurnLogger backfills them on startup.
    seq: int | None = None
    saga_atom_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    # chainlink #376: user messages that arrived mid-turn and were FOLDED into
    # this turn at a before_model boundary. Each entry is ``{"t_ms": float,
    # "text": str}`` (PR 4) — the rendered text the model saw plus a
    # start-relative fold offset (same axis as event/saga ``t_ms``) so the turn
    # viewer can place it on the timeline. One entry per fold, in fold order.
    # ``input`` stays the original turn prompt; these are the additional inputs
    # this single turn absorbed. Empty for the overwhelming majority of turns.
    # Threaded here so the durable surfaces — turn log, synthesis summary, turn
    # viewer — report what the turn consumed, not just the live message list.
    # (PR 3 shipped this as ``list[str]``; readers tolerate both.)
    injected_inputs: list[dict[str, Any]] = field(default_factory=list)
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
    # Discriminator for synthetic, non-conversational records (chainlink #60).
    # ``None`` for ordinary agent turns (the existing case). Set to
    # ``"claude_code_spawn"`` for records appended by ``spawn_claude_code``
    # on completion of a spawned ``claude -p`` subprocess — the spawn's
    # final ``total_cost_usd`` and ``modelUsage`` flow through here so
    # ``aggregate_usage`` sees plan-window spend natively.
    kind: str | None = None
    # Inline saga call audit. Each entry is a ``SagaCallRecord.to_dict()``
    # populated by ``RecordingSagaClient`` during the turn. Empty list
    # for turns that didn't touch saga (synthetic ticks, no-op heartbeats,
    # synthesis turns that didn't call back). Surfaces in the turn viewer
    # so "what saga did this turn" is visible inline without joining to
    # events.jsonl.
    saga_calls: list[dict[str, Any]] = field(default_factory=list)
    # Server-owned turn classification carried into the durable turn log.
    # Optional for backward compatibility and fail-closed downstream guards.
    interactivity: TurnInteractivity | None = None


def make_turn_id() -> str:
    # CR2 (agent runtime) fix: was ``hex[:12]`` = 48 bits. The
    # ``_active_turns`` registry (and the budget hook's
    # ``client_cell.turn_id`` foreign key) is keyed on this id;
    # birthday-bound 50% collision arrived at ~16M turns. With 64
    # bits, 50% collision is ~4B turns — well past the lifetime of
    # any single mimir process. The id is a key, not a display
    # string, so the brevity-vs-collision trade-off favors safety.
    return uuid.uuid4().hex[:16]


def make_process_session_id() -> str:
    """events.jsonl session_id — one per process lifetime (open-strix convention)."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
