from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.channel_audience as channel_audience
import mimir.identities as identities_module
from mimir.access_control import _source_is_triggering_channel_compatible
from mimir.agent import Agent
from mimir.acp.session_store import SessionStore
from mimir.channel_audience import ServerChannelAudienceProvider, attest_owner
from mimir.feedback import FeedbackLog
from mimir.history import MessageBuffer
from mimir.identities import AccessMetadata, Identity, IdentityResolver
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


def test_provider_self_invalidates_injected_resolver_after_out_of_band_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities_path = tmp_path / "state" / "identities.yaml"
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {discord: dm-old}\n",
    )
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    provider = ServerChannelAudienceProvider(tmp_path, identity_resolver=resolver)
    assert provider.audience_for("dm-old", principal="alice") == frozenset({"alice"})

    reads = 0
    parses = 0
    original_read_text = Path.read_text
    original_safe_load = identities_module.yaml.safe_load

    def counted_read_text(path: Path, *args, **kwargs):
        nonlocal reads
        if path == identities_path:
            reads += 1
        return original_read_text(path, *args, **kwargs)

    def counted_safe_load(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_safe_load(*args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    monkeypatch.setattr(identities_module.yaml, "safe_load", counted_safe_load)
    prior = identities_path.stat()
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {discord: dm-new-longer}\n",
    )
    assert identities_path.stat().st_size != prior.st_size

    assert provider.audience_for("dm-old", principal="alice") is None
    for _ in range(20):
        assert provider.audience_for(
            "dm-new-longer", principal="alice"
        ) == frozenset({"alice"})
    assert provider.identity_resolver is resolver
    assert (reads, parses) == (1, 1)


def test_group_slack_and_guild_channels_are_unknown_without_visibility_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {slack: slack-D1}\n",
    )
    provider = ServerChannelAudienceProvider(tmp_path)
    for channel in ("slack-C1", "discord-guild-1", "public", "group-private"):
        assert provider.audience_for(channel, principal="alice") is None

    class VisibilityTrapIdentity:
        canonical = "alice"
        dm_channels = {"discord": "discord-D-alice"}

        @property
        def channel_visibility(self):
            raise AssertionError("audience derivation must not inspect visibility")

    class Resolver:
        def __init__(self, home):
            self.home = home

        def reload(self):
            return None

        def reload_if_changed(self):
            return True

        def identity(self, principal):
            return VisibilityTrapIdentity() if principal == "alice" else None

    monkeypatch.setattr(channel_audience, "IdentityResolver", Resolver)
    trapped_provider = ServerChannelAudienceProvider(tmp_path)
    assert trapped_provider.audience_for("discord-guild-1", principal="alice") is None

    module_tree = ast.parse(inspect.getsource(channel_audience))
    provider = next(
        node for node in module_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ServerChannelAudienceProvider"
    )
    audience_for = next(
        node for node in provider.body
        if isinstance(node, ast.FunctionDef) and node.name == "audience_for"
    )
    module_functions = {
        node.name: node for node in module_tree.body if isinstance(node, ast.FunctionDef)
    }
    local_calls = {
        node.func.id
        for node in ast.walk(audience_for)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in module_functions
    }
    # Audience derivation may not delegate into a module-local helper whose
    # dependency surface is outside this proof.
    assert local_calls == set()
    for node in ast.walk(module_tree):
        values = (
            node.id if isinstance(node, ast.Name) else None,
            node.attr if isinstance(node, ast.Attribute) else None,
            node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None,
            node.name if isinstance(node, ast.alias) else None,
            node.asname if isinstance(node, ast.alias) else None,
        )
        assert all(
            value is None or "visibility" not in value.casefold()
            for value in values
        )


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

        def reload_if_changed(self):
            return True

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


def test_recent_user_requires_bound_minted_attestation_and_singleton_destination() -> None:
    class Resolver:
        def __init__(self, canonical: str) -> None:
            self.canonical = canonical

        def identity(self, author: str | None) -> Identity | None:
            if author == "raw-alice":
                return Identity(canonical=self.canonical)
            return None

    class Provider:
        def __init__(self, audience: frozenset[str]) -> None:
            self.audience = audience

        def audience_for(self, channel_id, *, principal):
            assert channel_id == "destination"
            assert principal == "alice"
            return self.audience

    attestation = attest_owner(Resolver("alice"), "raw-alice", "source")
    assert attestation is not None
    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "alice",
        "resolved_triggering": "destination",
        "audience_provider": Provider(frozenset({"alice"})),
        "cross_platform_pull": True,
    }
    unattested = _source(
        principal="alice",
        resource_id="source",
        source_kind="recent_activity_user",
    )
    hand_built = _source(
        principal="alice",
        resource_id="source",
        source_kind="recent_activity_user",
        owner_attestation=SimpleNamespace(
            canonical_principal="alice",
            raw_author="raw-alice",
            source_channel="source",
        ),
    )
    forged_attestation = attest_owner(
        Resolver("mallory"), "raw-alice", "source",
    )
    forged = _source(
        principal="alice",
        resource_id="source",
        source_kind="recent_activity_user",
        owner_attestation=forged_attestation,
    )
    bound = _source(
        principal="alice",
        resource_id="source",
        source_kind="recent_activity_user",
        owner_attestation=attestation,
    )

    assert _source_is_triggering_channel_compatible(unattested, **kwargs) is False
    assert _source_is_triggering_channel_compatible(hand_built, **kwargs) is False
    assert _source_is_triggering_channel_compatible(forged, **kwargs) is False
    assert _source_is_triggering_channel_compatible(bound, **kwargs) is True
    assert _source_is_triggering_channel_compatible(
        bound,
        **{
            **kwargs,
            "audience_provider": Provider(frozenset({"alice", "bob"})),
        },
    ) is False


def test_provider_reuses_injected_resolver_and_reads_each_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_identities(
        tmp_path,
        "people:\n  - canonical: alice\n    dm_channels: {discord: dm-alice}\n",
    )
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    session = SessionStore(tmp_path).create_owned_session("alice")
    reads: dict[str, int] = {"identities": 0, "session": 0}
    parses = 0
    original_read_text = Path.read_text
    original_safe_load = identities_module.yaml.safe_load

    def counted_read_text(path: Path, *args, **kwargs):
        if path.name == "identities.yaml":
            reads["identities"] += 1
        elif path == session.metadata_path:
            reads["session"] += 1
        return original_read_text(path, *args, **kwargs)

    def counted_safe_load(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_safe_load(*args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    monkeypatch.setattr(identities_module.yaml, "safe_load", counted_safe_load)
    provider = ServerChannelAudienceProvider(
        tmp_path,
        identity_resolver=resolver,
    )

    for _ in range(20):
        assert provider.audience_for("dm-alice", principal="alice") == frozenset({"alice"})
        assert provider.audience_for(session.thread_id, principal="alice") == frozenset({"alice"})

    assert provider.identity_resolver is resolver
    assert reads == {"identities": 0, "session": 1}
    assert parses == 0


def test_recent_activity_and_owner_attested_feedback_use_distinct_audience_policies() -> None:
    class Resolver:
        def identity(self, value):
            if value == "alice":
                return Identity(canonical="alice")
            return None

    class ProtocolProvider:
        def audience_for(self, channel_id, *, principal):
            return frozenset({"alice"})

    class ResolverProvider:
        identity_resolver = Resolver()

        def audience_for(self, channel_id, *, principal):
            return frozenset({"alice", "service:mimir"})

    marker = attest_owner(Resolver(), "alice", "source")
    assert marker is not None
    recent = _source(
        principal="alice",
        resource_id="source",
        source_kind="recent_activity_user",
        owner_attestation=marker,
    )
    feedback = _source(
        principal="alice",
        resource_id="source",
        source_kind="owner_attested_feedback",
        owner_attestation=marker,
    )
    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "alice",
        "resolved_triggering": "destination",
        "cross_platform_pull": True,
    }

    assert _source_is_triggering_channel_compatible(
        recent, audience_provider=ProtocolProvider(), **kwargs,
    ) is True
    assert _source_is_triggering_channel_compatible(
        feedback, audience_provider=ProtocolProvider(), **kwargs,
    ) is False
    assert _source_is_triggering_channel_compatible(
        recent, audience_provider=ResolverProvider(), **kwargs,
    ) is False
    assert _source_is_triggering_channel_compatible(
        feedback, audience_provider=ResolverProvider(), **kwargs,
    ) is True


def test_owner_attested_feedback_requires_bound_marker() -> None:
    class Resolver:
        def identity(self, value):
            return (
                Identity(canonical="alice")
                if value in {"raw-alice", "alice"}
                else None
            )

    class Provider:
        identity_resolver = Resolver()

        def audience_for(self, channel_id, *, principal):
            return frozenset({"alice"})

    marker = attest_owner(Resolver(), "raw-alice", "source")
    assert marker is not None
    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "raw-alice",
        "resolved_triggering": "destination",
        "audience_provider": Provider(),
        "cross_platform_pull": False,
    }
    sources = (
        _source(
            source_kind="owner_attested_feedback",
            resource_id="source",
            owner_attestation=None,
        ),
        _source(
            source_kind="owner_attested_feedback",
            resource_id="source",
            owner_attestation=SimpleNamespace(
                canonical_principal="alice",
                raw_author="raw-alice",
                source_channel="source",
            ),
        ),
        _source(
            source_kind="owner_attested_feedback",
            resource_id="other",
            owner_attestation=marker,
        ),
    )

    assert all(
        not _source_is_triggering_channel_compatible(source, **kwargs)
        for source in sources
    )
    assert _source_is_triggering_channel_compatible(
        _source(
            source_kind="owner_attested_feedback",
            resource_id="source",
            owner_attestation=marker,
        ),
        **kwargs,
    ) is True


def test_owner_audience_normalization_skips_service_and_service_mimir() -> None:
    class Resolver:
        def identity(self, value):
            values = {
                "alice-alias": Identity(canonical="alice"),
                "bob-alias": Identity(canonical="bob"),
                "worker-alias": Identity(
                    canonical="worker",
                    access=AccessMetadata(is_service=True),
                ),
                "canonical-service": Identity(canonical="service:worker"),
            }
            return values.get(value)

        def resolve(self, value):
            raise AssertionError("resolve must not be called")

    class Provider:
        identity_resolver = Resolver()

        def __init__(self, audience):
            self.audience = audience

        def audience_for(self, channel_id, *, principal):
            return self.audience

    marker = attest_owner(Resolver(), "alice-alias", "source")
    assert marker is not None
    source = _source(
        principal="alice",
        resource_id="source",
        source_kind="owner_attested_feedback",
        owner_attestation=marker,
    )
    kwargs = {
        "effective_principal": "alice",
        "triggering_principal": "alice-alias",
        "resolved_triggering": "destination",
        "cross_platform_pull": True,
    }
    positive = (
        frozenset({"alice-alias"}),
        frozenset({"alice-alias", "worker-alias"}),
        frozenset({"alice-alias", "canonical-service"}),
        frozenset({"alice-alias", "service:mimir"}),
    )
    negative = (
        frozenset({"alice-alias", "bob-alias"}),
        frozenset({"alice-alias", "unknown"}),
        frozenset({"unknown"}),
        frozenset({"worker-alias"}),
        frozenset({"service:mimir"}),
        frozenset(),
    )

    for audience in positive:
        snapshot = frozenset(audience)
        assert _source_is_triggering_channel_compatible(
            source, audience_provider=Provider(audience), **kwargs,
        ) is True
        assert audience == snapshot
    for audience in negative:
        snapshot = frozenset(audience)
        assert _source_is_triggering_channel_compatible(
            source, audience_provider=Provider(audience), **kwargs,
        ) is False
        assert audience == snapshot
    assert source.authorized_principals == frozenset({"alice"})


def test_trusted_agent_arm_does_not_call_resolver_or_provider() -> None:
    class Provider:
        @property
        def identity_resolver(self):
            raise AssertionError("resolver metadata was not expected")

        def audience_for(self, channel_id, *, principal):
            raise AssertionError("provider lookup was not expected")

    assert _source_is_triggering_channel_compatible(
        _source(
            source_kind="agent_self",
            integrity="trusted",
            integrity_effect="informational",
        ),
        effective_principal="alice",
        triggering_principal="alice",
        resolved_triggering="destination",
        audience_provider=Provider(),
        cross_platform_pull=True,
    ) is True


def test_same_channel_arm_does_not_call_resolver_or_provider() -> None:
    class Provider:
        @property
        def identity_resolver(self):
            raise AssertionError("resolver metadata was not expected")

        def audience_for(self, channel_id, *, principal):
            raise AssertionError("provider lookup was not expected")

    assert _source_is_triggering_channel_compatible(
        _source(resource_id="destination"),
        effective_principal="alice",
        triggering_principal="alice",
        resolved_triggering="destination",
        audience_provider=Provider(),
        cross_platform_pull=True,
    ) is True
