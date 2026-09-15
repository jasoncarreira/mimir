"""One live domain grammar, with legacy decoding only at the record boundary."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from mimir.access_control import (
    _same_channel_authority,
    _source_is_triggering_channel_compatible,
    create_auth_context,
)
from mimir.agent import _initialize_ifc_labels
from mimir.models import AgentEvent, InformationFlowLabels, SourceKind, SourceLabel
from mimir.poller_recovery import _event_from_stash, _event_to_stash


def _source(**changes):
    return SourceLabel(**{
        "principal": "user", "domain": "channel", "resource_id": "slack-C1",
        "bridge_instance": "slack", "sensitivity": "private",
        "authorized_principals": frozenset({"user"}), **changes,
    })


@pytest.mark.parametrize("visibility", [None, "", 123, "private", "public", "unknown", "custom:detail", " "])
def test_ingress_producers_use_flat_domains(visibility):
    event = AgentEvent(
        trigger="channel_message", source="slack", channel_id="slack-C1",
        author="user", extra={"channel_visibility": visibility},
    )
    expected = visibility if isinstance(visibility, str) and visibility else None
    labels = _initialize_ifc_labels(event)
    auth = create_auth_context(event, ifc_labels=labels)
    assert (auth.domain, auth.domain_qualifier) == ("channel", expected)
    assert {(s.domain, s.domain_qualifier) for s in labels.sources} == {("channel", expected)}


@pytest.mark.parametrize("domain", ["channel:private", "channel:public", "channel:unknown", "repository:custom"])
def test_live_models_reject_second_grammar(domain):
    with pytest.raises(ValueError, match="domain must not contain"):
        _source(domain=domain)
    auth = create_auth_context(AgentEvent(
        trigger="channel_message", source="slack", channel_id="slack-C1",
    ))
    with pytest.raises(ValueError, match="domain must not contain"):
        replace(auth, domain=domain)


@pytest.mark.parametrize("qualifier", [None, "", "private", "public", "unknown", "custom:detail"])
def test_legacy_recovery_and_new_serialization(qualifier):
    source = _source(domain_qualifier=qualifier)
    event = AgentEvent(
        trigger="channel_message", source="slack", channel_id="slack-C1",
        author="user", ifc_labels=InformationFlowLabels().with_source(source),
    )
    stash = json.loads(json.dumps(_event_to_stash(event)))
    record = stash["ifc_labels"]["sources"][0]
    assert record["domain"] == "channel"
    assert record["domain_qualifier"] == qualifier
    assert _event_from_stash(stash).ifc_labels.sources == (source,)
    assert TypeAdapter(SourceLabel).dump_python(source)["domain_qualifier"] == qualifier
    record.pop("domain_qualifier")
    record["domain"] = "channel" if qualifier is None else f"channel:{qualifier}"
    restored = _event_from_stash(stash).ifc_labels.sources[0]
    assert restored == source
    assert _same_channel_authority(restored, "slack") is True
    assert _same_channel_authority(restored, "other") is False
    assert _same_channel_authority(restored, None) is False


def test_qualifier_is_part_of_identity_and_derivation():
    sources = tuple(_source(domain_qualifier=q) for q in (None, "", "private", "public"))
    assert len(set(sources)) == 4
    assert InformationFlowLabels(sources=sources + sources).sources == sources
    derived = SourceLabel.derived(
        sources, principal="service", domain="channel", domain_qualifier="private",
        resource_id="slack-C1", bridge_instance="slack", sensitivity="private",
    )
    assert (derived.domain, derived.domain_qualifier) == ("channel", "private")
    with pytest.raises(ValueError, match="conflicting persisted"):
        SourceLabel.from_record({
            "domain": "channel:private", "domain_qualifier": "public",
        })


def _domain_flow_decisions(classifier=_source_is_triggering_channel_compatible, *, legacy=False):
    decisions = []
    for domain, kind, resource, bridge, acl, audience, admin in itertools.product(
        ("channel", "channel:private", "channel:public", "channel:unknown",
         "channel:custom:detail", "channel:", "channel_metadata", "channelXYZ",
         "service", "repository", "repository:custom", "filesystem", "saga"),
        (*SourceKind, "unknown_test_source_kind"), ("slack-C1", "slack-C2", "repo:x"),
        ("slack", "other", None), (frozenset(), frozenset({"user"})),
        (None, frozenset(), frozenset({"user"}), frozenset({"user", "other"})),
        (False, True),
    ):
        base, separator, qualifier = domain.partition(":")
        source = _source(
            domain=base, domain_qualifier=qualifier if separator else None,
            resource_id=resource, bridge_instance=bridge,
            authorized_principals=acl,
        )
        source = SourceLabel.from_record({**vars(source), "source_kind": kind})
        if legacy:
            source = SimpleNamespace(**{
                **vars(source), "domain": domain, "is_complete": source.is_complete,
                "owner_attestation": None,
            })
        provider = None if audience is None else SimpleNamespace(
            audience_for=lambda *a, **kw: audience,
        )
        decisions.append(classifier(
            source, effective_principal="user", triggering_principal="user",
            resolved_triggering="slack-C1", audience_provider=provider,
            cross_platform_pull=True, admin_operator_cross_channel=admin,
            triggering_bridge_instance="slack",
        ))
    return decisions


def test_domain_flow_decisions_match_legacy_baseline():
    # Digest captured against the unmodified pre-split policy, including dynamic
    # qualifiers and the broader flat-name service prefix rule. Order is fixed.
    decisions = _domain_flow_decisions()
    assert len(decisions) == 28080
    assert sum(decisions) == 6652
    assert hashlib.sha256(json.dumps(decisions).encode()).hexdigest() == (
        "e94bea38e9976a0babe429696516c794fae0db72ba80cc366ec0cd62f76393f4"
    )
