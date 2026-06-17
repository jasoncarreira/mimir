from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from mimir.access_control import (
    AccessStatus,
    DenialReason,
    authorize_action,
    authorize_inbound,
)
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent


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
