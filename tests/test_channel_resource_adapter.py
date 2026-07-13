"""Channel resource adapter tests for chainlink #866.

Tests authorization for send_message/react/fetch_channel_history based on
server-resolved triggering channel and bridge resources.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from mimir.access_control import (
    ChannelResourceAdapter,
    OperationDecision,
    ToolAuthorization,
    get_operation_catalog,
)
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, AuthContext


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _auth_context(
    channel_id: str | None,
    roles: tuple[str, ...] = (),
    enforce: bool = False,
) -> AuthContext:
    return AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=roles,
        event_ingress=None,
        trigger="user_message",
        channel_id=channel_id,
        interactivity=None,
        enforcement_enabled=enforce,
    )


class TestChannelResourceAdapterDecision:
    """Test the ChannelResourceAdapter.get_decision method."""

    def test_returns_resource_scoped_for_send_message(self):
        decision = ChannelResourceAdapter.get_decision("send_message", None)
        assert decision == OperationDecision.RESOURCE_SCOPED

    def test_returns_resource_scoped_for_react(self):
        decision = ChannelResourceAdapter.get_decision("react", None)
        assert decision == OperationDecision.RESOURCE_SCOPED

    def test_returns_resource_scoped_for_fetch_channel_history(self):
        decision = ChannelResourceAdapter.get_decision("fetch_channel_history", None)
        assert decision == OperationDecision.RESOURCE_SCOPED

    def test_returns_none_for_non_channel_operation(self):
        decision = ChannelResourceAdapter.get_decision("shell_exec", None)
        assert decision is None


class TestChannelResourceAdapterAuthorization:
    """Test the ChannelResourceAdapter.authorize_channel_operation method."""

    def test_same_scope_allows_regular_user(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C1",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is True
        assert auth.reason == "same_scope_channel"

    def test_same_scope_with_different_prefix_allows(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C1",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is True

    def test_cross_channel_denies_regular_user(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C2",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "cross_channel_scope"
        assert auth.required_tier.value == "admin"

    def test_cross_channel_allows_admin_user(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C2",
            _auth_context("discord-C1", roles=("user", "admin")),
            enforce=True,
        )
        assert auth.allowed is True

    def test_missing_triggering_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C1",
            _auth_context(None),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "missing_triggering_channel"

    def test_missing_target_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            None,
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "missing_target_channel"

    def test_empty_target_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "missing_target_channel"

    def test_unknown_channel_denies_regular_user(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "unknown-channel",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "cross_channel_scope"

    def test_react_same_scope_allows(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "react",
            "discord-C1",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is True

    def test_react_cross_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "react",
            "discord-C2",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False

    def test_fetch_channel_history_same_scope_allows(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "fetch_channel_history",
            "discord-C1",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is True

    def test_fetch_channel_history_cross_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "fetch_channel_history",
            "discord-C2",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False


class TestChannelResourceAdapterAliasResolution:
    """Test server-side channel alias resolution."""

    def test_resolves_channel_alias(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-C1
                aliases: [alias-for-c1]
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "alias-for-c1",
                _auth_context("discord-C1"),
                enforce=True,
            )
            assert auth.allowed is True
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)

    def test_resolves_triggering_channel_alias(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-C1
                aliases: [alias-for-c1]
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "discord-C1",
                _auth_context("alias-for-c1"),
                enforce=True,
            )
            assert auth.allowed is True
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)

    def test_resolves_both_aliases_to_different_denies(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-C1
                aliases: [alias1]
              - canonical: discord-C2
                aliases: [alias2]
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "alias2",
                _auth_context("alias1"),
                enforce=True,
            )
            assert auth.allowed is False
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)

    def test_resolves_both_aliases_to_same_allows(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-C1
                aliases: [alias1, alias2]
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "alias2",
                _auth_context("alias1"),
                enforce=True,
            )
            assert auth.allowed is True
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)

    def test_cross_bridge_denies(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-C1
              - canonical: slack-C1
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "slack-C1",
                _auth_context("discord-C1"),
                enforce=True,
            )
            assert auth.allowed is False
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)


class TestOperationCatalogIntegration:
    """Test that OperationCatalog uses ChannelResourceAdapter."""

    def test_channel_operations_return_resource_scoped(self):
        catalog = get_operation_catalog()
        decision = catalog.get_decision("send_message", None)
        assert decision == OperationDecision.RESOURCE_SCOPED

    def test_non_channel_operations_return_open(self):
        catalog = get_operation_catalog()
        decision = catalog.get_decision("list_channels", None)
        assert decision == OperationDecision.OPEN


class TestShadowMode:
    """Test that non-enforced calls work in shadow mode."""

    def test_cross_channel_allowed_in_shadow_mode(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C2",
            _auth_context("discord-C1"),
            enforce=False,
        )
        assert auth.allowed is True
        assert auth.is_shadow_decision is True

    def test_same_scope_allowed_in_shadow_mode(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C1",
            _auth_context("discord-C1"),
            enforce=False,
        )
        assert auth.allowed is True


class TestDifferentBridgeInstances:
    """Test that channel equality alone is not authority across bridge instances."""

    def test_same_channel_different_bridge_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "slack-C1",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False
        assert auth.reason == "cross_channel_scope"

    def test_cross_platform_alias_different_bridges(self, tmp_path: Path):
        resolver = _resolver(
            tmp_path,
            """
            channels:
              - canonical: discord-general
              - canonical: slack-general
            """,
        )
        ChannelResourceAdapter.set_identity_resolver(resolver)

        try:
            auth = ChannelResourceAdapter.authorize_channel_operation(
                "send_message",
                "slack-general",
                _auth_context("discord-general"),
                enforce=True,
            )
            assert auth.allowed is False
        finally:
            ChannelResourceAdapter.set_identity_resolver(None)


class TestPublicUnknownChannels:
    """Test handling of public and unknown channels."""

    def test_public_channel_cross_channel_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "web-hook",
            _auth_context("discord-C1"),
            enforce=True,
        )
        assert auth.allowed is False

    def test_no_triggering_channel_public_target_denies(self):
        auth = ChannelResourceAdapter.authorize_channel_operation(
            "send_message",
            "discord-C1",
            _auth_context(None),
            enforce=True,
        )
        assert auth.allowed is False
