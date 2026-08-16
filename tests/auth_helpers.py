"""Authorization carriers shared by middleware test harnesses."""

from __future__ import annotations

from mimir.models import AuthContext, InformationFlowLabels, TurnContext


def middleware_auth_context(channel_id: str = "test-channel") -> AuthContext:
    """Return a legitimate operator context for middleware integration tests."""
    return AuthContext(
        principal="test-admin",
        canonical_principal="test-admin",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id=channel_id,
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
        domain="channel",
        resource_id=channel_id,
        bridge_instance="test",
    )


def attach_middleware_auth_context(turn: TurnContext) -> TurnContext:
    """Attach a test authorization carrier belonging to the turn's channel."""
    auth_context = middleware_auth_context(turn.channel_id or "test-channel")
    turn.auth_context = auth_context
    turn.ifc_labels = auth_context.ifc_labels
    return turn
