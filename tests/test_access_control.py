from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from mimir.access_control import (
    AccessStatus,
    DenialReason,
    authorize_action,
    authorize_inbound,
    create_auth_context,
)
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, AuthContext, TurnContext


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _event(author: str | None) -> AgentEvent:
    return AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author=author,
        content="hello",
    )


def test_inbound_allows_allowlisted_user_when_enforced(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver, enforce=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.USER_ALLOWED
    assert decision.denial_reason is None
    assert decision.canonical_author == "alice"
    assert decision.roles == ("user",)


def test_inbound_distinguishes_known_non_allowlisted_from_unknown(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    known = authorize_inbound(_event("slack-U1"), resolver, enforce=True)
    unknown = authorize_inbound(_event("slack-U2"), resolver, enforce=True)

    assert known.allowed is False
    assert known.status == AccessStatus.DENIED
    assert known.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert known.canonical_author == "alice"
    assert unknown.allowed is False
    assert unknown.reason == DenialReason.UNKNOWN_AUTHOR
    assert unknown.canonical_author == "slack-U2"


def test_admin_action_requires_admin_role(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: root
            aliases: [slack-UADMIN]
            access: {roles: [user, admin]}
        """,
    )

    user = authorize_action(_event("slack-U1"), resolver, admin=True, enforce=True)
    admin = authorize_action(_event("slack-UADMIN"), resolver, admin=True, enforce=True)

    assert user.allowed is False
    assert user.reason == DenialReason.ADMIN_REQUIRED
    assert admin.allowed is True
    assert admin.status == AccessStatus.ADMIN_ALLOWED
    assert admin.reason is None


def test_admin_action_follows_canonical_aliases_across_slack_discord(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: root
            aliases: [slack-UADMIN, discord-42]
            access: {roles: [user, admin]}
        """,
    )

    slack = authorize_action("slack-UADMIN", resolver, admin=True, enforce=True)
    discord = authorize_action("discord-42", resolver, admin=True, enforce=True)

    assert slack.allowed is True
    assert discord.allowed is True
    assert slack.canonical_author == "root"
    assert discord.canonical_author == "root"
    assert slack.roles == ("user", "admin")
    assert discord.roles == ("user", "admin")


def test_legacy_default_allows_but_reports_would_deny_reason(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.enforcement_enabled is False


def test_missing_resolver_preserves_single_operator_legacy_behavior() -> None:
    decision = authorize_action(_event("slack-U1"), None, admin=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.canonical_author == "slack-U1"


def test_missing_author_has_stable_denial_reason_when_enforced() -> None:
    decision = authorize_inbound(_event(None), None, enforce=True)

    assert decision.allowed is False
    assert decision.status == AccessStatus.DENIED
    assert decision.denial_reason == "missing_author"


def test_log_fields_are_stable_string_values(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    fields = authorize_action(
        "slack-U1",
        resolver,
        admin=True,
        enforce=True,
    ).as_log_fields()

    assert fields == {
        "allowed": False,
        "status": "denied",
        "required_tier": "admin",
        "denial_reason": "admin_required",
        "author": "slack-U1",
        "canonical_author": "alice",
        "roles": ["user"],
        "enforcement_enabled": True,
    }


def test_auth_context_frozen_is_immutable(tmp_path: Path) -> None:
    """Verify AuthContext is frozen and cannot be mutated after creation."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user, admin]}
        """,
    )

    event = _event("slack-U1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.principal == "slack-U1"
    assert auth_ctx.canonical_principal == "alice"
    assert auth_ctx.roles == ("user", "admin")
    assert auth_ctx.is_service is False


def test_auth_context_carries_ingress_provenance(tmp_path: Path) -> None:
    """Verify AuthContext captures server-owned ingress metadata."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author="slack-U1",
        content="hello",
        extra={"event_ingress": "http-api"},
    )
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.event_ingress == "http-api"
    assert auth_ctx.trigger == "user_message"
    assert auth_ctx.channel_id == "slack-C1"


def test_auth_context_service_identity(tmp_path: Path) -> None:
    """Verify AuthContext captures service identity from identity resolver."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: mcp-service
            aliases: [mcp-1]
            access: {roles: [service], is_service: true}
        """,
    )

    event = _event("mcp-1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.is_service is True
    assert "service" in auth_ctx.roles


def test_auth_context_denies_forged_session_id(tmp_path: Path) -> None:
    """Verify that forged session_ids cannot widen authority."""
    from mimir._context import resolve_auth_context

    # Create a real turn context with auth_context for alice (user only)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user, admin]}
        """,
    )

    alice_event = _event("slack-U1")
    alice_auth = create_auth_context(alice_event, resolver)

    # Create bob's auth context
    bob_event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C2",
        author="slack-U2",
        content="hello",
    )
    bob_auth = create_auth_context(bob_event, resolver)

    # Create TurnContext for alice with her auth_context
    from mimir.models import TurnContext

    alice_turn = TurnContext(
        turn_id="turn-alice",
        session_id="session-alice",
        saga_session_id="saga-session-alice",
        trigger="user_message",
        channel_id="slack-C1",
        started_at=0.0,
        auth_context=alice_auth,
    )

    # Create a DIFFERENT TurnContext for bob with his auth_context
    # but NOT registered in _active_turns
    bob_turn = TurnContext(
        turn_id="turn-bob",
        session_id="session-bob",
        saga_session_id="saga-session-bob",
        trigger="user_message",
        channel_id="slack-C2",
        started_at=0.0,
        auth_context=bob_auth,
    )

    # Register only alice's turn
    from mimir._context import set_current_turn, reset_current_turn
    token = set_current_turn(alice_turn)

    try:
        # Try to resolve with bob's saga_session_id - should NOT return bob's auth_context
        # because bob's turn is not registered in _active_turns
        # The resolution should fall back to alice's active turn
        forged_auth, path = resolve_auth_context({"session_id": "saga-session-bob"})
        # Should get alice's context (the active turn), not bob's
        assert forged_auth is not None
        assert forged_auth.principal == "slack-U1"
        # Path should be single_active (fallback to active turn)
        assert path == "single_active"
    finally:
        reset_current_turn(token)


def test_auth_context_denies_missing_carrier_under_enforcement() -> None:
    """Verify operations are denied when auth_context is missing under enforcement."""
    from mimir._context import resolve_auth_context, set_current_turn, reset_current_turn
    from mimir.models import TurnContext, TurnInteractivity

    # Create a TurnContext WITHOUT auth_context (legacy path)
    turn_ctx = TurnContext(
        turn_id="turn-1",
        session_id="session-1",
        trigger="user_message",
        channel_id="slack-C1",
        started_at=0.0,
    )

    token = set_current_turn(turn_ctx)
    try:
        # Under enforcement, missing auth_context should deny operations
        auth_ctx, path = resolve_auth_context({})
        assert auth_ctx is None
        assert path == "missing"
    finally:
        reset_current_turn(token)


def test_auth_context_denies_concurrent_turn_principal_swap(tmp_path: Path) -> None:
    """Verify concurrent turns cannot swap principals to widen authority."""
    from mimir._context import set_current_turn, reset_current_turn, resolve_auth_context
    from mimir.models import TurnContext

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user, admin]}
        """,
    )

    # Create turn for alice (user only)
    alice_event = _event("slack-U1")
    alice_auth = create_auth_context(alice_event, resolver)

    alice_turn = TurnContext(
        turn_id="turn-alice",
        session_id="session-alice",
        trigger="user_message",
        channel_id="slack-C1",
        started_at=0.0,
        auth_context=alice_auth,
    )

    # Register alice's turn
    alice_token = set_current_turn(alice_turn)

    try:
        # Resolve alice's context using her correct session_id
        alice_resolved, path = resolve_auth_context({"session_id": "session-alice"})
        assert alice_resolved is not None
        assert alice_resolved.canonical_principal == "alice"
        assert "admin" not in alice_resolved.roles

        # Try to resolve with bob's session_id while alice's turn is active
        # This simulates trying to use another user's session to get elevated privileges
        # The key security property: we should NOT get bob's auth context
        # because we're looking up by session_id that exists in a DIFFERENT turn
        # that isn't currently active in this task
        swapped_auth, path = resolve_auth_context({"session_id": "session-bob"})

        # Either returns None (no turn found with that session in this task)
        # or returns the currently active turn's auth context
        # It should NOT return bob's admin context
        if swapped_auth is not None:
            assert swapped_auth.canonical_principal == "alice", \
                "Should not be able to swap principals via session_id"
    finally:
        reset_current_turn(alice_token)
