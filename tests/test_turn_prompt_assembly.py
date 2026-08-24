"""Regression test for 181-H: per-turn prompt block assembly.

Pre-181-H the deepagents-backed Agent shoved only ``event.content``
(optionally prefixed with the SAGA recall block) into a
``HumanMessage`` — completely bypassing the rich per-turn user-side
prompt that the SDK path assembled: Recent activity, Recent feedback,
Session summaries, Resource usage, Upcoming, Upcoming commitments,
Self-state, etc.

181-H ports ``_build_turn_prompt`` + its eight ``_assemble_*``
helpers back from main. This test exercises them directly so a
regression that drops any of the labeled section headers — or
breaks the synthesis-turn branch — fails the suite instead of
silently shipping an empty-block prompt to the model.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mimir.access_control import SinkGate, build_scheduled_tick_service_principal
from mimir.agent import (
    Agent,
    _REFLECTION_CHANNEL_ID,
    _filter_session_turns,
    _prompt_source_labels,
)
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import (
    AgentEvent,
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    Integrity,
    PromptBlock,
    SourceLabel,
    TurnContext,
)
from mimir.saga.client import SagaStore
from mimir.turn_logger import TurnLogger


def _make_agent(tmp_path: Path) -> Agent:
    """Construct an Agent rooted at ``tmp_path``. Skips
    ``_build_agent_if_needed`` — these tests drive
    ``_build_turn_prompt`` directly without invoking the model.
    """
    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    return Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=cfg.home / "messages.jsonl"),
        index_generator=IndexGenerator(cfg.home),
    )


def _make_ctx(event: AgentEvent, saga_session_id: str | None = None) -> TurnContext:
    auth_context = AuthContext(
        principal=event.author,
        canonical_principal=event.author,
        roles=(),
        event_ingress=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
    )
    return TurnContext(
        turn_id="turn-test",
        session_id=event.channel_id or "default",
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=time.monotonic(),
        saga_session_id=saga_session_id,
        auth_context=auth_context,
        ifc_labels=InformationFlowLabels().with_source(SourceLabel(
            principal=event.author,
            domain="channel",
            resource_id=event.channel_id,
            bridge_instance=event.source or "test",
            sensitivity="private",
            authorized_principals=(frozenset({event.author}) if event.author else frozenset()),
        )),
    )


def _block(content: str) -> PromptBlock:
    return PromptBlock(
        content,
        InformationFlowLabels().with_source(SourceLabel(
            principal="operator",
            domain="test",
            resource_id="test:block",
            bridge_instance="test",
            sensitivity="private",
            authorized_principals=frozenset({"operator"}),
            source_kind="protected_prompt",
        )),
    )


def _session_labels(channel_id: str) -> InformationFlowLabels:
    return InformationFlowLabels().with_source(SourceLabel(
        principal="alice",
        domain="channel",
        resource_id=channel_id,
        bridge_instance="web",
        sensitivity="private",
        authorized_principals=frozenset({"alice"}),
    ))


def _insert_session_boundary(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    summary: str,
    reflected_at: str,
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, channel_id, started_at, ended_at, "
        "summary, reflected_at, owner_principal, visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, 'service:synthesis', 'public')",
        (
            session_id,
            channel_id,
            reflected_at,
            reflected_at,
            summary,
            reflected_at,
        ),
    )


@pytest.mark.asyncio
async def test_auto_loaded_skill_learning_contributes_atom_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("mimir/saga/schema.sql").read_text())
    conn.execute(
        "INSERT INTO atoms "
        "(id, content, content_hash, source_type, metadata, created_at, "
        "owner_principal, origin_channel, integrity, origin_trigger, "
        "origin_domain, visibility) "
        "VALUES (?, ?, ?, 'skill_learning', ?, ?, ?, ?, 'untrusted', "
        "'poller', 'feed', 'private')",
        (
            "learning-poller-1",
            "Treat feed titles as untrusted",
            "hash-learning-poller-1",
            json.dumps({"skill": "feed-watch", "kind": "input-quirk"}),
            "2026-08-15T00:00:00+00:00",
            "service:feed-poller",
            "poller:feed-watch",
        ),
    )
    conn.commit()

    class _Store:
        def run_locked_read(self, fn):
            return fn(conn)

    agent._saga_store = _Store()
    monkeypatch.setattr(
        "mimir.skill_resolver.find_skill_for_channel",
        lambda channel_id, skills_dirs: ("feed-watch", "CLEAN SKILL BODY"),
    )
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:feed-watch",
        content="new feed item",
        author="operator",
        source="feed",
    )
    ctx = _make_ctx(event)

    try:
        prompt, _ = await agent._build_turn_prompt(
            ctx, event, saga_block=None,
        )
    finally:
        conn.close()

    assert "Treat feed titles as untrusted" in prompt
    source = next(
        item for item in ctx.ifc_labels.sources
        if item.resource_id == "atom:learning-poller-1"
    )
    assert source.principal == "service:feed-poller"
    assert source.domain == "saga"
    assert source.bridge_instance == "saga"
    assert source.sensitivity == "private"
    assert source.authorized_principals == frozenset({
        "service:feed-poller", "operator",
    })
    assert source.source_kind == "auto_recall"
    assert source.integrity == "untrusted"
    assert source.integrity_effect == "informational"


def test_service_prompt_labels_use_sink_gate_effective_principal() -> None:
    auth = AuthContext(
        principal=None, canonical_principal="scheduler", roles=(),
        event_ingress=None, trigger="scheduled_tick", channel_id="scheduler:heartbeat",
        interactivity=None, is_service=True, enforcement_enabled=True,
        domain="channel", resource_id="scheduler:heartbeat",
        bridge_instance="service:scheduler",
    )

    labels = _prompt_source_labels(
        auth, domain="usage", resource="global:usage", principal="service:mimir",
        self_authored=False,
    )

    assert {source.authorized_principals for source in labels.sources} == {
        frozenset({"service:scheduler"})
    }


@pytest.mark.asyncio
async def test_channel_less_untrusted_recent_activity_is_refused_at_sink(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message", channel_id="ch-current", content="now",
        author="alice", source="discord",
    )
    legacy = agent._buffer.make_message(
        channel_id="", kind="user_message", content="legacy unbound message",
        author="alice", msg_id="legacy-message", source="discord",
        integrity=Integrity.UNTRUSTED,
    )
    await agent._buffer.append(legacy)
    auth = AuthContext(
        principal="alice", canonical_principal="alice", roles=(),
        event_ingress=None, trigger="user_message", channel_id="ch-current",
        interactivity=None, enforcement_enabled=True, domain="channel",
        resource_id="ch-current", bridge_instance="discord",
    )
    ctx = _make_ctx(event)
    ctx.auth_context = auth

    prompt, recent = await agent._build_turn_prompt(
        ctx, event, saga_block=None, initial_auth_context=auth,
    )

    assert "legacy unbound message" in prompt
    assert legacy in recent
    source = next(
        item for item in ctx.ifc_labels.sources
        if item.resource_id == "message:legacy-message"
    )
    assert source.integrity == Integrity.UNTRUSTED
    sink_auth = replace(auth, ifc_state=InformationFlowState(labels=ctx.ifc_labels))
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", event.channel_id, ctx.ifc_labels, sink_auth,
        enforce=True,
    )
    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:same_channel"


@pytest.mark.asyncio
async def test_self_authored_framework_prompt_sources_remain_sink_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.identities import Identity

    agent = _make_agent(tmp_path)
    core_dir = tmp_path / "memory" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "00-identity.md").write_text("CORE IDENTITY SENTINEL")
    channel_dir = tmp_path / "memory" / "channels" / "ch-current"
    channel_dir.mkdir(parents=True)
    (channel_dir / "notes.md").write_text("CHANNEL MEMORY SENTINEL")

    class _Resolver:
        def resolve(self, author):
            return "alice" if author == "alice-alias" else author

        def all_identities(self):
            return [Identity(
                canonical="alice", display_name="Alice",
                aliases=["alice-alias"],
            )]

        def display_name(self, author):
            return "Alice" if self.resolve(author) == "alice" else None

        def resolve_channel(self, channel_id):
            return None

    agent._buffer.resolver = _Resolver()
    recent_message = agent._buffer.make_message(
        channel_id="ch-current", kind="user_message",
        content="trusted recent reply", author="alice-alias",
        msg_id="trusted-message", source="discord", integrity=Integrity.TRUSTED,
    )
    await agent._buffer.append(recent_message)
    monkeypatch.setattr(
        "mimir.skill_resolver.find_skill_for_channel",
        lambda channel_id, skills_dirs: ("framework-skill", "SKILL SENTINEL"),
    )
    event = AgentEvent(
        trigger="user_message", channel_id="ch-current", content="now",
        author="alice", source="discord",
    )
    auth = AuthContext(
        principal="alice", canonical_principal="alice", roles=(),
        event_ingress=None, trigger="user_message", channel_id="ch-current",
        interactivity=None, enforcement_enabled=True, domain="channel",
        resource_id="ch-current", bridge_instance="discord",
    )
    ctx = _make_ctx(event)
    ctx.auth_context = auth

    system_prompt = agent._build_system_prompt(emit_health_events=False)
    turn_prompt, _ = await agent._build_turn_prompt(
        ctx, event, saga_block="SAGA SENTINEL", initial_auth_context=auth,
    )

    assert "CORE IDENTITY SENTINEL" in system_prompt
    for sentinel in (
        "CHANNEL MEMORY SENTINEL", "SKILL SENTINEL", "SAGA SENTINEL",
    ):
        assert sentinel in turn_prompt
    framework_domains = {"channel_memory", "identities", "saga", "skills"}
    framework_sources = [
        source for source in ctx.ifc_labels.sources
        if source.domain in framework_domains
    ]
    assert {source.domain for source in framework_sources} == framework_domains
    assert all(source.integrity == Integrity.TRUSTED for source in framework_sources)
    sink_auth = replace(auth, ifc_state=InformationFlowState(labels=ctx.ifc_labels))
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", event.channel_id, ctx.ifc_labels, sink_auth,
        enforce=True,
    )
    assert decision.allowed is True
    assert decision.reason == "ifc_allowed"


def test_agent_system_prompt_guidance_tracks_enforcement_flag(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)

    agent._config.access_control_enforced = False
    assert "## Access-control enforcement" not in agent._build_system_prompt()

    agent._config.access_control_enforced = True
    prompt = agent._build_system_prompt()
    assert "## Access-control enforcement" in prompt
    assert "the gate enforces these rules regardless of model compliance" in prompt


@pytest.mark.asyncio
async def test_heartbeat_authority_guidance_renders_only_under_enforcement(
    tmp_path: Path,
) -> None:
    from mimir.access_control import builtin_trigger_service_principal

    agent = _make_agent(tmp_path)
    authority = builtin_trigger_service_principal("heartbeat", tmp_path)
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="tick",
        service_principal=authority.canonical,
        service_authority=authority,
    )
    auth = AuthContext(
        principal=authority.canonical,
        canonical_principal=authority.canonical,
        roles=(),
        event_ingress=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
        is_service=True,
        service_authority=authority,
        enforcement_enabled=True,
    )

    agent._config.access_control_enforced = True
    prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, None, initial_auth_context=auth,
    )
    assert "## Autonomous trigger authority" in prompt
    assert "profile: ``heartbeat``" in prompt
    assert "``fetch_url`` may reach this profile's approved exact URLs and URL scopes" in prompt
    assert "scoped URLs require a clean turn" in prompt

    agent._config.access_control_enforced = False
    shadow_prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, None, initial_auth_context=auth,
    )
    assert "## Autonomous trigger authority" not in shadow_prompt


@pytest.mark.asyncio
async def test_delivery_uses_trusted_poller_configuration_in_shadow_mode(
    tmp_path: Path,
) -> None:
    from mimir.access_control import CapabilityTier, build_trigger_service_principal

    agent = _make_agent(tmp_path)
    agent._channels = type("Channels", (), {"find": lambda self, channel: object()})()
    authority = build_trigger_service_principal(
        canonical="configured-poller",
        trigger="poller",
        profile="custom",
        tier=CapabilityTier.SCOPE_CONTAINED,
        capabilities=(),
        creation_path="test",
    )
    authority = replace(
        authority, configured_delivery_channel="slack-ops",
    )
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:configured",
        content="new item",
        extra={"deliver": "slack-forged"},
        service_principal=authority.canonical,
        service_authority=authority,
    )
    auth = AuthContext(
        principal=authority.canonical,
        canonical_principal=authority.canonical,
        roles=(),
        event_ingress=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
        is_service=True,
        service_authority=authority,
        enforcement_enabled=False,
    )

    prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, None, initial_auth_context=auth,
    )

    assert "## Delivery" in prompt
    assert "slack-ops" in prompt
    assert "slack-forged" not in prompt


@pytest.mark.asyncio
async def test_http_delivery_extra_is_not_rendered(tmp_path: Path) -> None:
    from mimir.worklink.continuation import (
        HTTP_EVENT_INGRESS_EXTRA_KEY,
        HTTP_EVENT_INGRESS_EXTRA_VALUE,
    )

    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message",
        channel_id="web-alice",
        content="summarize my recent messages",
        author="alice",
        extra={
            "deliver": "slack-C0PRIVATE",
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
        },
    )
    auth = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=HTTP_EVENT_INGRESS_EXTRA_VALUE,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
        enforcement_enabled=False,
    )

    prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, None, initial_auth_context=auth,
    )

    assert "## Delivery" not in prompt
    assert "slack-C0PRIVATE" not in prompt


@pytest.mark.asyncio
async def test_poller_authority_guidance_matches_granted_capabilities(
    tmp_path: Path,
) -> None:
    from mimir.access_control import CapabilityTier, build_trigger_service_principal

    agent = _make_agent(tmp_path)
    agent._config.access_control_enforced = True
    authority = build_trigger_service_principal(
        canonical="github-poller",
        trigger="poller",
        profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=("read_file", "worklink_run"),
        creation_path="test",
    )
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github",
        content="new issue",
        service_principal=authority.canonical,
        service_authority=authority,
    )
    auth = AuthContext(
        principal=authority.canonical,
        canonical_principal=authority.canonical,
        roles=(),
        event_ingress=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
        is_service=True,
        service_authority=authority,
        enforcement_enabled=True,
    )

    prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, None, initial_auth_context=auth,
    )

    assert "profile: ``github``; tier ``code-execution``" in prompt
    assert "Available capabilities: ``read_file``, ``worklink_run``" in prompt
    assert "``fetch_url`` is not available to this trigger" in prompt
    assert "``web_search`` is not available to this trigger" in prompt


# ─── User-message branch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_turn_prompt_emits_labeled_sections(tmp_path: Path) -> None:
    """The standard turn-prompt path renders the labeled section
    headers that ``build_turn_prompt`` produces for the inputs it's
    given. ``## Today's date`` is always present (per build_turn_prompt
    contract). The current-event header — ``## Current event``
    surrogate for the user-message branch — surfaces the inbound
    body. The synthesis branch must NOT fire here.
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-1",
        content="hello mimir",
        author="user-1",
    )
    ctx = _make_ctx(event)
    turn_prompt, recent = await agent._build_turn_prompt(
        ctx, event, saga_block=None,
    )
    # The user-side body is in the prompt (proof we wired through
    # build_turn_prompt rather than echoing event.content alone).
    assert "hello mimir" in turn_prompt
    # build_turn_prompt always emits ``## Today's date`` — it's the
    # one header that's not conditional on optional block content.
    assert "## Today's date" in turn_prompt
    # Synthesis branch did NOT fire — the synthesis template is
    # markedly different (its body starts with the saga_session
    # summary scaffold, no event header).
    assert "Mark each atom" not in turn_prompt  # synthesis-template phrase
    # No recent messages in the freshly-instantiated buffer.
    assert recent == []


@pytest.mark.asyncio
async def test_build_turn_prompt_surfaces_saga_block(
    tmp_path: Path,
) -> None:
    """When the pre-message SAGA hook supplies a block, it appears in
    the prompt under its canonical label.
    Verifies the wiring from ``_run_turn_body`` → ``_build_turn_prompt``
    actually threads those args through (181-H regression: pre-fix
    they were discarded and only ``event.content`` made it through).
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-2",
        content="what's the topic?",
    )
    ctx = _make_ctx(event)
    turn_prompt, _ = await agent._build_turn_prompt(
        ctx, event,
        saga_block="- atom-foo: prior fact",
    )
    assert "## Possibly relevant memories (from SAGA)" in turn_prompt
    assert "atom-foo: prior fact" in turn_prompt
    assert "## Subagent updates" not in turn_prompt


# ─── Synthesis branch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_turn_prompt_routes_synthesis_to_dedicated_template(
    tmp_path: Path,
) -> None:
    """``trigger='saga_session_end'`` must route through
    ``_build_synthesis_prompt`` (which loads the saga_session_end
    template), NOT the standard build_turn_prompt path."""
    agent = _make_agent(tmp_path)
    agent._config.turns_log.write_text(json.dumps({
        "turn_id": "turn-session",
        "ts": "2026-06-28T05:30:00+00:00",
        "saga_session_id": "sess-xyz",
        "channel_id": "ch-3",
        "output": "session content",
    }))
    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="ch-3",
        content="(unused)",
        extra={"saga_session_id": "sess-xyz"},
        ifc_labels=_session_labels("ch-3"),
    )
    ctx = _make_ctx(event, saga_session_id="sess-xyz")
    turn_prompt, recent = await agent._build_turn_prompt(
        ctx, event, saga_block=None,
    )
    # The synthesis template carries the session id verbatim
    # (placeholder ``{saga_session_id}`` is filled by render).
    assert "sess-xyz" in turn_prompt
    assert f"`{tmp_path}/scratch/turns/turn-test/`" in turn_prompt
    # The standard-turn ``## Today's date`` header should NOT be
    # present — synthesis uses its own scaffold.
    assert "## Today's date" not in turn_prompt
    # No recent list when synthesis branch fires.
    assert recent == []
    assert event.ifc_labels is not None
    assert all(source in ctx.ifc_labels.sources for source in event.ifc_labels.sources)


# ─── Direct synthesis prompt builder ────────────────────────────────


def test_filter_session_turns_uses_turn_record_ts_for_window_break(
    tmp_path: Path,
) -> None:
    """Session-window filtering must use turns.jsonl's canonical ``ts``
    field. A regression that looks only for ``timestamp`` still returns the
    matching records, but never hits the time-based break and scans the full
    file tail on every synthesis turn.
    """
    turns_path = tmp_path / "turns.jsonl"
    records = [
        {
            "turn_id": "too-old-match",
            "ts": "2026-06-28T05:00:00+00:00",
            "saga_session_id": "sess-1",
            "channel_id": "ch-1",
        },
        {
            "turn_id": "old-nonmatch",
            "ts": "2026-06-28T05:05:00+00:00",
            "saga_session_id": "other",
        },
        {
            "turn_id": "recent-match-1",
            "ts": "2026-06-28T05:30:00+00:00",
            "saga_session_id": "sess-1",
            "channel_id": "ch-1",
        },
        {
            "turn_id": "recent-match-2",
            "ts": "2026-06-28T05:31:00+00:00",
            "saga_session_id": "sess-1",
            "channel_id": "ch-1",
        },
        {
            "turn_id": "newest-nonmatch",
            "ts": "2026-06-28T05:32:00+00:00",
            "saga_session_id": "other",
        },
    ]
    turns_path.write_text("\n".join(json.dumps(r) for r in records))

    filtered = _filter_session_turns(
        turns_path, "sess-1", channel_id="ch-1", idle_minutes=10,
    )

    assert [rec["turn_id"] for rec in filtered] == [
        "recent-match-1",
        "recent-match-2",
    ]


def test_filter_session_turns_rejects_foreign_channel_with_same_session_id(
    tmp_path: Path,
) -> None:
    turns_path = tmp_path / "turns.jsonl"
    records = [
        {
            "turn_id": "foreign",
            "ts": "2026-06-28T05:30:00+00:00",
            "saga_session_id": "sess-known",
            "channel_id": "ch-foreign",
            "output": "foreign secret",
        },
        {
            "turn_id": "own",
            "ts": "2026-06-28T05:31:00+00:00",
            "saga_session_id": "sess-known",
            "channel_id": "ch-own",
            "output": "own content",
        },
    ]
    turns_path.write_text("\n".join(json.dumps(record) for record in records))

    filtered = _filter_session_turns(
        turns_path, "sess-known", channel_id="ch-own",
    )

    assert [record["turn_id"] for record in filtered] == ["own"]


@pytest.mark.asyncio
async def test_build_synthesis_prompt_handles_empty_window(
    tmp_path: Path,
) -> None:
    """When the session has no recorded turns, ``_build_synthesis_prompt``
    must still emit a valid synthesis prompt (the lean variant) rather
    than crashing. Pre-181-H regression — _filter_session_turns
    returning [] should produce a template render, not a KeyError.
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="ch-empty",
        extra={"saga_session_id": "sess-empty"},
        ifc_labels=_session_labels("ch-empty"),
    )
    ctx = _make_ctx(event, saga_session_id="sess-empty")
    rendered = await agent._build_synthesis_prompt(ctx, event)
    assert isinstance(rendered, PromptBlock)
    assert "sess-empty" in rendered.content
    assert rendered.content  # non-empty
    assert rendered.labels == InformationFlowLabels()


# ─── Full-block-stack regression coverage ───────────────────────────
#
# Every conditional block in ``build_turn_prompt`` has its own
# labeled section header. If a future change drops one of the
# kwargs from agent's ``_build_turn_prompt`` (or somebody removes a
# ``_add_labeled`` line in prompts.py), we want a test that fails
# specifically — naming the missing block — instead of a silent
# behavior loss. These tests cover both sides:
#
#   * ``test_build_turn_prompt_function_emits_all_blocks_when_supplied``
#     drives ``mimir.prompts.build_turn_prompt`` directly with every
#     block populated. Catches dropped ``_add_labeled`` calls in
#     prompts.py.
#
#   * ``test_agent_build_turn_prompt_threads_all_helper_outputs``
#     monkey-patches every ``Agent._assemble_*`` helper to return a
#     known sentinel, runs ``Agent._build_turn_prompt``, asserts
#     each sentinel + its header surface. Catches the case where
#     a helper is added but its return value never makes it into
#     the build_turn_prompt() call — the exact regression Mimir's
#     second review flagged.


# Canonical labels emitted by ``build_turn_prompt`` for every
# optional block. Order matches the implementation in prompts.py.
# If one of these is removed (legitimate API change), update the
# test in lockstep with the prompt's `_add_labeled` call site.
_OPTIONAL_BLOCK_LABELS = (
    "Known identities",
    "Recent feedback signals",
    "Recent session summaries",
    "Resource usage",
    "Upcoming",
    "Upcoming commitments",
    "Self-state",
    "Recent activity",
    "Possibly relevant memories (from SAGA)",
)


def test_build_turn_prompt_function_emits_all_blocks_when_supplied() -> None:
    """Drive ``build_turn_prompt`` directly with every optional block
    populated (and a resolver + recent_messages so the identity +
    activity branches fire). Every canonical header must appear in
    the rendered output.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from mimir.history import Message
    from mimir.prompts import build_turn_prompt

    from mimir.identities import Identity

    class _StubResolver:
        """Drop-in for IdentityResolver covering the four methods that
        build_turn_prompt / MessageBuffer.assemble_recent_activity call:
        ``resolve``, ``display_name``, ``all_identities``,
        ``resolve_channel``. Concrete enough to drive both code paths
        without filesystem setup."""

        def __init__(self, identities: list[Identity]) -> None:
            self._identities = {i.canonical: i for i in identities}
            self._alias_to_canonical = {
                alias: i.canonical
                for i in identities for alias in (i.aliases or [])
            }

        def resolve(self, author: str | None) -> str | None:
            if author is None:
                return None
            return self._alias_to_canonical.get(author, author)

        def display_name(self, author: str | None) -> str | None:
            if author is None:
                return None
            canonical = self._alias_to_canonical.get(author, author)
            ident = self._identities.get(canonical)
            return ident.display_name if ident else None

        def all_identities(self) -> list[Identity]:
            return list(self._identities.values())

        def resolve_channel(self, channel_id):
            return None

    resolver_obj = _StubResolver([
        Identity(
            canonical="canon-1",
            display_name="User One",
            aliases=["user-id-1"],
        ),
    ])

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-coverage",
        content="please summarize",
        author="user-id-1",
        author_id="user-id-1",
        source_id="msg-1",
    )
    recent = [
        Message(
            ts=datetime.now(tz=timezone.utc).isoformat(),
            msg_id="msg-0",
            channel_id="ch-coverage",
            author="user-id-1",
            author_display="User One",
            kind="user",
            content="prior message",
            source="discord",
        ),
    ]
    rendered = build_turn_prompt(
        event,
        recent_messages=recent,
        saga_block="- atom-x: saga-fact",
        recent_message_chars=200,
        resolver=resolver_obj,
        feedback_block="recent feedback ledger",
        session_summaries_block="recent boundaries summary",
        usage_block="cost: $0.00 / 1h",
        upcoming_block="next scheduled tick at +5m",
        commitments_block="C-1 due Friday",
        self_state_block="homeostat: green",
        saga_session_id="sess-x",
    )

    # The always-on header is present.
    assert "## Today's date" in rendered, "always-on date header missing"

    # Every optional block fired its labeled section. Loop is the
    # regression guard: a future change that drops any single
    # `_add_labeled(...)` call in prompts.py will fail here with
    # the specific label name in the assertion message.
    for label in _OPTIONAL_BLOCK_LABELS:
        assert f"## {label}" in rendered, (
            f"build_turn_prompt dropped the {label!r} section even "
            f"though its block argument was supplied. Check "
            f"mimir/prompts.py:build_turn_prompt for a missing "
            f"_add_labeled(\"{label}\", ...) call."
        )

    import inspect

    assert "subagent_block" not in inspect.signature(build_turn_prompt).parameters


def test_build_turn_prompt_directs_scratch_to_server_supplied_turn_path() -> None:
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(trigger="user_message", channel_id="ch", content="work")
    rendered = build_turn_prompt(
        event, turn_scratch_path="/mimir-home/scratch/turns/server-turn-id",
    )

    assert "## Current turn scratch" in rendered
    assert "`/mimir-home/scratch/turns/server-turn-id/`" in rendered
    assert "shared `scratch/` root" in rendered


@pytest.mark.asyncio
async def test_agent_build_turn_prompt_threads_all_helper_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive ``Agent._build_turn_prompt`` with every ``_assemble_*``
    helper monkey-patched to return a known sentinel. Each sentinel
    must surface under its corresponding header in the assembled
    turn prompt.

    The pre-181-H regression was that the agent assembled a
    HumanMessage from just ``event.content`` — the helpers ran but
    their outputs were discarded. This test catches the same failure
    mode at a finer granularity: if anyone drops a single kwarg from
    the ``build_turn_prompt(...)`` call in ``_build_turn_prompt``,
    we lose its sentinel and the assertion names which block
    regressed.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from mimir.history import Message

    agent = _make_agent(tmp_path)

    # Seed a recent message so the buffer's `assemble_recent_activity`
    # has something to return. The exact content doesn't matter — we
    # check the labeled header, not the body — but we do need the
    # ``Recent activity`` branch to fire.
    seeded = Message(
        ts=datetime.now(tz=timezone.utc).isoformat(),
        msg_id="prior-1",
        channel_id="ch-cover",
        author="user-a",
        author_display="User A",
        kind="user",
        content="seeded recent",
        source="discord",
    )
    await agent._buffer.append(seeded)

    # Patch every helper to return a recognizable sentinel.
    # ``_assemble_usage_block`` returns a 2-tuple (block, deferred);
    # the rest are scalar str | None.
    monkeypatch.setattr(
        agent, "_assemble_usage_block",
        lambda auth_context: (_block("USAGE_SENTINEL"), []),
    )
    monkeypatch.setattr(
        agent, "_assemble_upcoming_block",
        lambda auth_context: _block("UPCOMING_SENTINEL"),
    )
    monkeypatch.setattr(
        agent, "_assemble_commitments_block",
        lambda channel_id, auth_context: _block("COMMITMENTS_SENTINEL"),
    )
    monkeypatch.setattr(
        agent, "_assemble_self_state_block",
        lambda auth_context, **_kw: _block("SELFSTATE_SENTINEL"),
    )

    async def _fake_session_summaries(*, channel_id, auth_context):
        assert auth_context is admin_auth
        return _block("SESSIONS_SENTINEL")

    monkeypatch.setattr(
        agent, "_assemble_session_summaries", _fake_session_summaries,
    )
    monkeypatch.setattr(
        agent._feedback, "recent_prompt_block",
        lambda auth_context: _block("FEEDBACK_SENTINEL"),
    )

    # Stub a resolver on the buffer so the identity branch fires.
    from mimir.identities import Identity

    class _StubResolver:
        def __init__(self) -> None:
            self._identities = {
                "canon-x": Identity(
                    canonical="canon-x",
                    display_name="Cover User",
                    aliases=["user-a"],
                )
            }
            self._alias = {"user-a": "canon-x"}

        def resolve(self, a):
            return self._alias.get(a, a) if a is not None else None

        def display_name(self, a):
            c = self._alias.get(a, a) if a else None
            return self._identities[c].display_name if c in self._identities else None

        def all_identities(self):
            return list(self._identities.values())

        def resolve_channel(self, cid):
            return None

    monkeypatch.setattr(agent._buffer, "resolver", _StubResolver())

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-cover",
        content="full-coverage input",
        author="user-a",
        author_id="user-a",
        source_id="msg-now",
    )
    ctx = _make_ctx(event)
    admin_auth = AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-cover",
        interactivity=None,
    )
    turn_prompt, _ = await agent._build_turn_prompt(
        ctx, event,
        saga_block="SAGA_SENTINEL",
        initial_auth_context=admin_auth,
    )

    # Each labeled section must be threaded with the helper's output.
    # The list mirrors prompts.py's `_add_labeled` calls — keep in
    # lockstep with _OPTIONAL_BLOCK_LABELS above.
    expected_pairs = (
        ("Known identities", "Cover User"),
        ("Recent feedback signals", "FEEDBACK_SENTINEL"),
        ("Recent session summaries", "SESSIONS_SENTINEL"),
        ("Resource usage", "USAGE_SENTINEL"),
        ("Upcoming", "UPCOMING_SENTINEL"),
        ("Upcoming commitments", "COMMITMENTS_SENTINEL"),
        ("Self-state", "SELFSTATE_SENTINEL"),
        ("Recent activity", "seeded recent"),
        ("Possibly relevant memories (from SAGA)", "SAGA_SENTINEL"),
    )
    missing: list[str] = []
    for label, sentinel in expected_pairs:
        header_present = f"## {label}" in turn_prompt
        body_present = sentinel in turn_prompt
        if not (header_present and body_present):
            missing.append(
                f"{label!r}: header={header_present}, body={body_present}"
            )
    assert not missing, (
        "Agent._build_turn_prompt is failing to thread block content "
        "into the prompt. Specific failures:\n  " + "\n  ".join(missing)
        + "\nCheck the build_turn_prompt(...) keyword arguments "
        "inside mimir/agent.py:_build_turn_prompt."
    )

    # The always-on header is always present — sanity check.
    assert "## Today's date" in turn_prompt


@pytest.mark.asyncio
async def test_channel_memory_omits_for_principal_less_auth_context(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    channel_dir = tmp_path / "memory" / "channels" / "ch-ownerless"
    channel_dir.mkdir(parents=True)
    (channel_dir / "notes.md").write_text("PRIVATE CHANNEL MEMORY")
    event = AgentEvent(
        trigger="user_message", channel_id="ch-ownerless", content="hello",
        author=None, source="discord",
    )
    auth = AuthContext(
        principal=None, canonical_principal=None, roles=(), event_ingress=None,
        trigger="user_message", channel_id="ch-ownerless", interactivity=None,
        enforcement_enabled=True,
    )

    prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, saga_block=None,
        initial_auth_context=auth,
    )

    assert "PRIVATE CHANNEL MEMORY" not in prompt
    assert "## Today's date" in prompt


@pytest.mark.asyncio
async def test_feedback_block_renders_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for chainlink #681.

    ``FeedbackLog.recent_block`` can refresh and parse events/turns JSONL
    snapshots. Prompt assembly must not do that synchronous file/CPU work
    on the dispatcher event loop.
    """
    agent = _make_agent(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(agent, "_assemble_usage_block", lambda auth_context: (None, []))
    monkeypatch.setattr(agent, "_assemble_upcoming_block", lambda auth_context: None)
    monkeypatch.setattr(agent, "_assemble_commitments_block", lambda channel_id, auth_context: None)
    monkeypatch.setattr(agent, "_assemble_self_state_block", lambda auth_context, **_kw: None)

    async def _none_summaries(*, channel_id, auth_context):
        assert isinstance(auth_context, AuthContext)
        return None

    monkeypatch.setattr(agent, "_assemble_session_summaries", _none_summaries)

    def recent_block_stub(auth_context):
        return _block("FEEDBACK_SENTINEL")

    monkeypatch.setattr(agent._feedback, "recent_prompt_block", recent_block_stub)

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)

    monkeypatch.setattr("mimir.agent.asyncio.to_thread", fake_to_thread)

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-feedback",
        content="hello",
        author="user-a",
    )
    turn_prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, saga_block=None,
    )

    assert "FEEDBACK_SENTINEL" in turn_prompt
    assert "recent_block_stub" in calls


@pytest.mark.asyncio
async def test_ownerless_non_self_feedback_does_not_abort_prompt_assembly(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    agent._config.turns_log.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "turn_id": "scheduled-turn",
        "channel_id": "scheduler:heartbeat",
        "result_is_error": True,
    }) + "\n")
    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-feedback",
        content="hello",
        author="operator",
        source="test",
    )
    auth = AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("admin",),
        event_ingress=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        interactivity=None,
        enforcement_enabled=True,
        bridge_instance="test",
    )

    turn_prompt, _ = await agent._build_turn_prompt(
        _make_ctx(event), event, saga_block=None, initial_auth_context=auth,
    )

    assert "turn error: (no detail)" in turn_prompt


@pytest.mark.asyncio
async def test_session_summaries_counts_turns_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for chainlink #597.

    ``JsonlSnapshot.records`` can tail and parse ``turns.jsonl`` when its
    cache is stale. Prompt assembly must invoke the turn-count helper via
    ``asyncio.to_thread`` so scheduled turns do not do that file/CPU work
    on the event loop.
    """
    agent = _make_agent(tmp_path)

    class _StubSaga:
        async def recent_session_boundaries(
            self, *, channel_id, count, auth_context,
        ):
            assert channel_id == "ch-1"
            assert count == agent._config.recent_boundaries
            assert auth_context is admin_auth
            return [
                {
                    "ts": "2026-06-21T09:00:00+00:00",
                    "channel_id": "ch-1",
                    "summary": "older",
                },
                {
                    "ts": "2026-06-21T10:00:00+00:00",
                    "channel_id": "ch-1",
                    "summary": "newer",
                },
            ]

    agent._saga = _StubSaga()
    admin_auth = AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
    )
    to_thread_calls: list[tuple[object, tuple, dict]] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr("mimir.agent.asyncio.to_thread", fake_to_thread)

    block = await agent._assemble_session_summaries(
        channel_id="ch-1", auth_context=admin_auth,
    )

    assert block is not None
    block = block.content
    assert len(to_thread_calls) == 1
    func, args, kwargs = to_thread_calls[0]
    assert func.__name__ == "count_turns_since_many"
    assert args == (agent._config.turns_log,)
    assert kwargs["channel_id"] == "ch-1"
    assert kwargs["since_timestamps"] == [
        "2026-06-21T09:00:00+00:00",
        "2026-06-21T10:00:00+00:00",
    ]
    snapshot_records = kwargs["snapshot_records"]
    assert snapshot_records.__self__ is agent._turns_snapshot
    assert snapshot_records.__func__ is agent._turns_snapshot.records.__func__


def test_reflection_channel_id_matches_the_scheduler_channel(tmp_path: Path) -> None:
    # The constant must equal the channel the scheduler synthesizes for the
    # `reflect` job; if these drift, reflection silently reverts to
    # channel-scoped summaries with no error.
    auth = build_scheduled_tick_service_principal("reflect", tmp_path)

    assert auth is not None
    assert auth.channel_memory_directory == _REFLECTION_CHANNEL_ID


@pytest.mark.asyncio
async def test_reflection_turn_assembles_recent_cross_channel_sessions(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("mimir/saga/schema.sql").read_text())
    _insert_session_boundary(
        conn,
        session_id="oldest",
        channel_id="channel-old",
        summary="oldest boundary excluded by configured count",
        reflected_at="2026-07-01T08:00:00+00:00",
    )
    _insert_session_boundary(
        conn,
        session_id="middle",
        channel_id="channel-alpha",
        summary="recent alpha boundary",
        reflected_at="2026-07-01T09:00:00+00:00",
    )
    _insert_session_boundary(
        conn,
        session_id="newest",
        channel_id="channel-beta",
        summary="recent beta boundary",
        reflected_at="2026-07-01T10:00:00+00:00",
    )
    conn.commit()
    agent._saga = SagaStore(conn=conn, embedding_dim=4)
    agent._config.recent_boundaries = 2
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id=_REFLECTION_CHANNEL_ID,
        content="reflect",
        author="service:scheduler",
    )

    try:
        prompt, _ = await agent._build_turn_prompt(
            _make_ctx(event), event, saga_block=None,
        )
    finally:
        conn.close()

    assert "recent alpha boundary" in prompt
    assert "recent beta boundary" in prompt
    assert "oldest boundary excluded by configured count" not in prompt


@pytest.mark.asyncio
async def test_ordinary_turn_session_summaries_remain_channel_scoped(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("mimir/saga/schema.sql").read_text())
    _insert_session_boundary(
        conn,
        session_id="own",
        channel_id="channel-alpha",
        summary="own channel boundary",
        reflected_at="2026-07-01T09:00:00+00:00",
    )
    _insert_session_boundary(
        conn,
        session_id="other",
        channel_id="channel-beta",
        summary="other channel boundary",
        reflected_at="2026-07-01T10:00:00+00:00",
    )
    conn.commit()
    agent._saga = SagaStore(conn=conn, embedding_dim=4)
    agent._config.recent_boundaries = 5
    event = AgentEvent(
        trigger="user_message",
        channel_id="channel-alpha",
        content="hello",
        author="user-a",
    )

    try:
        prompt, _ = await agent._build_turn_prompt(
            _make_ctx(event), event, saga_block=None,
        )
    finally:
        conn.close()

    assert "own channel boundary" in prompt
    assert "other channel boundary" not in prompt


@pytest.mark.asyncio
async def test_session_summary_assembly_is_authorization_scoped(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    agent._config.access_control_enforced = True
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("mimir/saga/schema.sql").read_text())
    rows = [
        ("public", "public summary", "user:bob", "public"),
        ("alice", "alice summary", "user:alice", "private"),
        ("bob", "bob summary", "user:bob", "private"),
        ("service", "service summary", "service:synthesis", "service"),
    ]
    for index, (session_id, summary, owner, visibility) in enumerate(rows):
        conn.execute(
            "INSERT INTO sessions (id, channel_id, started_at, ended_at, "
            "summary, reflected_at, owner_principal, visibility) "
            "VALUES (?, 'ch-auth', '2026-07-01T00:00:00+00:00', ?, ?, ?, ?, ?)",
            (
                session_id,
                f"2026-07-01T00:0{index}:00+00:00",
                summary,
                f"2026-07-01T00:0{index}:00+00:00",
                owner,
                visibility,
            ),
        )
    conn.commit()
    agent._saga = SagaStore(conn=conn, embedding_dim=4)
    agent._config.recent_boundaries = len(rows)

    admin_auth = AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-auth",
        interactivity=None,
        enforcement_enabled=True,
    )
    alice_auth = AuthContext(
        principal="user:alice",
        canonical_principal="user:alice",
        roles=(),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-auth",
        interactivity=None,
        enforcement_enabled=True,
    )

    try:
        admin_source = await agent._assemble_session_summaries(
            channel_id="ch-auth", auth_context=admin_auth,
        )
        alice_source = await agent._assemble_session_summaries(
            channel_id="ch-auth", auth_context=alice_auth,
        )
    finally:
        conn.close()

    assert admin_source is not None
    admin_block = admin_source.content
    assert all(summary in admin_block for _, summary, _, _ in rows)
    assert alice_source is not None
    alice_block = alice_source.content
    assert "public summary" in alice_block
    assert "alice summary" in alice_block
    assert "bob summary" not in alice_block
    assert "service summary" not in alice_block
    assert all(
        "user:alice" in source.authorized_principals
        for source in alice_source.labels.sources
    )
    assert all(
        source.principal in source.authorized_principals
        for source in alice_source.labels.sources
    )


@pytest.mark.asyncio
async def test_commitment_prompt_labels_are_owned_by_record_principal(
    tmp_path: Path,
) -> None:
    from mimir.commitments import CommitmentRecord, CommitmentsStore, make_commitment_id

    agent = _make_agent(tmp_path)
    agent._commitments = CommitmentsStore(path=tmp_path / "commitments.jsonl")
    await agent._commitments.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="ch-auth", text="alice reminder",
        owner_principal="user:alice",
    ))
    auth = AuthContext(
        principal="user:alice", canonical_principal="user:alice", roles=(),
        event_ingress=None, trigger="user_message", channel_id="ch-auth",
        interactivity=None, enforcement_enabled=True,
    )

    block = agent._assemble_commitments_block("ch-auth", auth)

    assert block is not None
    assert {source.principal for source in block.labels.sources} == {"user:alice"}
    assert {source.authorized_principals for source in block.labels.sources} == {
        frozenset({"user:alice"})
    }


@pytest.mark.asyncio
async def test_agent_build_turn_prompt_omits_blocks_for_none_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse coverage: when every ``_assemble_*`` returns ``None``
    (or empty), the corresponding labeled sections must NOT appear.
    Prevents accidental rendering of empty-body blocks (which would
    eat prompt cache and confuse the model).
    """
    agent = _make_agent(tmp_path)
    monkeypatch.setattr(agent, "_assemble_usage_block", lambda auth_context: (None, []))
    monkeypatch.setattr(agent, "_assemble_upcoming_block", lambda auth_context: None)
    monkeypatch.setattr(agent, "_assemble_commitments_block", lambda channel_id, auth_context: None)
    monkeypatch.setattr(agent, "_assemble_self_state_block", lambda auth_context, **_kw: None)

    async def _none_summaries(*, channel_id, auth_context):
        assert isinstance(auth_context, AuthContext)
        return None

    monkeypatch.setattr(agent, "_assemble_session_summaries", _none_summaries)
    monkeypatch.setattr(agent._feedback, "recent_prompt_block", lambda auth_context: None)

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-empty",
        content="hello",
        author="user-a",
    )
    ctx = _make_ctx(event)
    turn_prompt, _ = await agent._build_turn_prompt(
        ctx, event, saga_block=None,
    )

    # None of the optional headers should appear — they're conditional
    # on truthy block content.
    leaked: list[str] = []
    for label in _OPTIONAL_BLOCK_LABELS:
        if f"## {label}" in turn_prompt:
            leaked.append(label)
    assert not leaked, (
        f"Empty/None helpers should suppress their labeled sections, "
        f"but these still appeared in the prompt: {leaked}"
    )

    # The always-on header survives.
    assert "## Today's date" in turn_prompt
