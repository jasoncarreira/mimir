from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.access_control import _source_is_triggering_channel_compatible
from mimir.agent import Agent
from mimir.acp.session_store import SessionStore
from mimir.channel_audience import ServerChannelAudienceProvider, attest_owner
from mimir.feedback import FeedbackLog
from mimir.history import MessageBuffer
from mimir.identities import Identity, IdentityResolver
from mimir.models import AuthContext, OwnerAttestation, SourceLabel


def _write_identities(home: Path, body: str) -> None:
    path = home / "state" / "identities.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _source(**changes: object) -> SourceLabel:
    values = {
        "principal": "alice",
        "domain": "recent_activity",
        "resource_id": "dm-alice",
        "bridge_instance": "discord",
        "sensitivity": "private",
        "authorized_principals": frozenset({"alice"}),
        "source_kind": "protected_prompt",
        "integrity": "untrusted",
        "integrity_effect": "informational",
    }
    values.update(changes)
    return SourceLabel(**values)


def test_acp_audience_comes_from_load_owned_metadata(tmp_path: Path) -> None:
    record = SessionStore(tmp_path).create_owned_session("alice")
    provider = ServerChannelAudienceProvider(tmp_path)

    assert provider.audience_for(record.thread_id, principal="alice") == frozenset({"alice"})
    assert provider.audience_for(record.thread_id, principal="bob") is None
    record.metadata_path.write_text("{}", encoding="utf-8")
    assert provider.audience_for(record.thread_id, principal="alice") is None


def test_dm_audience_uses_strict_identity_dm_channels(tmp_path: Path) -> None:
    _write_identities(
        tmp_path,
        """
people:
  - canonical: alice
    aliases: [discord-alice]
    dm_channels: {discord: discord-dm-alice, slack: slack-dm-alice}
""",
    )
    provider = ServerChannelAudienceProvider(tmp_path)

    assert provider.audience_for("discord-dm-alice", principal="discord-alice") == frozenset({"alice"})
    assert provider.audience_for("discord-dm-alice", principal="unknown") is None
    assert provider.audience_for("discord-guild", principal="discord-alice") is None


def test_empty_and_missing_audiences_are_unknown(tmp_path: Path) -> None:
    provider = ServerChannelAudienceProvider(tmp_path)
    assert provider.audience_for(None, principal="alice") is None
    assert provider.audience_for("", principal="alice") is None
    assert provider.audience_for("dm", principal=None) is None


def test_provider_absence_failure_and_live_reload_fail_closed_without_cache(
    tmp_path: Path,
) -> None:
    provider = ServerChannelAudienceProvider(tmp_path)
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {discord: dm-old}\n",
    )
    assert provider.audience_for("dm-old", principal="alice") == frozenset({"alice"})
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {discord: dm-new}\n",
    )
    assert provider.audience_for("dm-old", principal="alice") is None
    assert provider.audience_for("dm-new", principal="alice") == frozenset({"alice"})
    _write_identities(tmp_path, "people: [")
    assert provider.audience_for("dm-new", principal="alice") is None


def test_group_slack_and_guild_channels_are_unknown_without_visibility_reads(
    tmp_path: Path,
) -> None:
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {slack: slack-D1}\n",
    )
    provider = ServerChannelAudienceProvider(tmp_path)
    for channel in ("slack-C1", "discord-guild-1", "public", "group-private"):
        assert provider.audience_for(channel, principal="alice") is None


def test_hostile_event_model_and_request_audience_values_are_ignored(
    tmp_path: Path,
) -> None:
    provider = ServerChannelAudienceProvider(tmp_path)
    hostile = {
        "channel_id": "public",
        "principal": "alice",
        "audience": ["alice"],
        "channel_visibility": "private",
    }
    assert provider.audience_for(
        hostile["channel_id"], principal=hostile["principal"],
    ) is None


def test_owner_attestation_is_factory_minted_by_strict_identity() -> None:
    class Resolver:
        def identity(self, author: str | None) -> Identity | None:
            if author == "discord-alice":
                return Identity(canonical="alice")
            return None

        def resolve(self, author: str | None) -> str | None:
            raise AssertionError("resolve must not be called")

    resolver = Resolver()
    attestation = attest_owner(resolver, "discord-alice", "discord-D1")
    assert attestation is not None
    assert attestation.canonical_principal == "alice"
    assert attest_owner(resolver, "alice-looking", "discord-D1") is None
    with pytest.raises(TypeError):
        OwnerAttestation("alice", "discord-alice", "discord-D1")


def test_eligibility_paths_never_call_identity_resolver_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StrictResolver:
        identity_calls: list[str | None] = []

        def __init__(self, home=None):
            self.home = home

        def reload(self):
            return None

        def identity(self, author):
            self.identity_calls.append(author)
            if author in {"alice", "alice-alias"}:
                return Identity(
                    canonical="alice",
                    dm_channels={"discord": "dm-alice"},
                )
            return None

        def resolve(self, author):
            raise AssertionError("eligibility called permissive resolve")

    import mimir.channel_audience as audience_module

    monkeypatch.setattr(audience_module, "IdentityResolver", StrictResolver)
    provider = ServerChannelAudienceProvider(tmp_path)
    assert provider.audience_for("dm-alice", principal="alice-alias") == frozenset({"alice"})
    resolver = StrictResolver()
    assert attest_owner(resolver, "alice-alias", "dm-alice") is not None

    buffer = MessageBuffer(history_path=tmp_path / "history.jsonl", resolver=resolver)
    message = buffer.make_message(
        channel_id="dm-alice",
        kind="user_message",
        content="strict history",
        author="alice-alias",
    )
    buffer._append_in_memory(message)
    assert buffer.assemble_recent_activity_candidates(
        channel_id="destination",
        author="alice",
        recent_per_channel=5,
        recent_author_cross=5,
        cross_hours=24,
    ) == [message]

    class KnownProvider:
        def audience_for(self, channel_id, *, principal):
            return frozenset({"alice"})

    agent = object.__new__(Agent)
    agent._buffer = buffer
    agent._identity_resolver = resolver
    agent._config = SimpleNamespace(
        recent_per_channel=5,
        recent_author_cross=5,
        recent_cross_hours=24,
        recent_sources=None,
    )
    auth = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="destination",
        interactivity=None,
        audience_provider=KnownProvider(),
    )
    from mimir.models import AgentEvent

    selected, _ = agent._select_recent_activity(
        AgentEvent(
            trigger="user_message",
            channel_id="destination",
            author="alice",
        ),
        auth,
    )
    assert selected == [message]

    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"timestamp":"2999-01-01T00:00:00+00:00","type":"tool_call_denied",'
        '"tool":"strict","reason":"strict","channel_id":"dm-alice",'
        '"owner_principal":"alice-alias"}\n',
        encoding="utf-8",
    )
    feedback = FeedbackLog(
        events_path=events,
        turns_path=tmp_path / "turns.jsonl",
        identity_resolver=resolver,
    )
    block = feedback.recent_prompt_block(auth)
    assert block is not None
    assert "strict" in block.content

    protected_sources = (
        inspect.getsource(ServerChannelAudienceProvider.audience_for),
        inspect.getsource(attest_owner),
        inspect.getsource(MessageBuffer.assemble_recent_activity_candidates),
        inspect.getsource(FeedbackLog._select_prompt_recent),
        inspect.getsource(Agent._select_recent_activity),
        inspect.getsource(_source_is_triggering_channel_compatible),
    )
    assert all(".resolve(" not in source for source in protected_sources)
    assert "attest_owner(" in protected_sources[4]
    assert "_source_is_triggering_channel_compatible(" in protected_sources[4]


def test_destination_audience_subset_direction() -> None:
    class Provider:
        audiences = {
            "destination": frozenset({"alice", "bob"}),
            "source-wide": frozenset({"alice", "bob", "carol"}),
            "source-narrow": frozenset({"alice"}),
        }

        def audience_for(self, channel_id, *, principal):
            return self.audiences.get(channel_id)

    provider = Provider()
    wide = _source(resource_id="source-wide")
    narrow = _source(resource_id="source-narrow")
    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "alice",
        "resolved_triggering": "destination",
        "audience_provider": provider,
        "cross_platform_pull": True,
    }
    assert _source_is_triggering_channel_compatible(wide, **kwargs) is True
    assert _source_is_triggering_channel_compatible(narrow, **kwargs) is False


def test_compatibility_preconditions_and_early_arms_do_not_query_provider() -> None:
    class Provider:
        def audience_for(self, channel_id, *, principal):
            raise AssertionError("provider lookup was not expected")

    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "alice",
        "resolved_triggering": "destination",
        "audience_provider": Provider(),
        "cross_platform_pull": True,
    }
    assert _source_is_triggering_channel_compatible(
        _source(authorized_principals=frozenset()), **kwargs,
    ) is False
    assert _source_is_triggering_channel_compatible(
        _source(
            source_kind="agent_self",
            integrity="trusted",
            integrity_effect="informational",
        ),
        **kwargs,
    ) is True
    assert _source_is_triggering_channel_compatible(
        _source(resource_id="destination"), **kwargs,
    ) is True
