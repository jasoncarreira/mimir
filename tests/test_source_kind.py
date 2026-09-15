"""Known-kind classification and compatibility with open runtime strings."""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from mimir.access_control import _source_is_triggering_channel_compatible
from mimir.models import SourceKind, SourceLabel, _mint_owner_attestation
from mimir.prompt_sources import prompt_source_label


KINDS = (
    "acp_hands_result agent_self auto_recall channel "
    "channel_bound_unowned_feedback channel_scoped_feedback feedback_chain "
    "mcp owner_attested_feedback protected_prompt protected_tool "
    "recent_activity_assistant recent_activity_user service"
).split()
UNKNOWN_KINDS = [
    "web_ui", "operator_command", "file", "unknown", "",
    "protected_tool\x1b", "secret contents",
]


def test_source_kind_vocabulary_and_annotation():
    assert set(SourceKind) == set(KINDS)
    assert get_type_hints(SourceLabel)["source_kind"] is SourceKind


@pytest.mark.parametrize("kind", KINDS + UNKNOWN_KINDS)
def test_source_kind_string_producer_compatibility(kind):
    source = prompt_source_label(
        SimpleNamespace(resource_id="slack-C1", channel_id="slack-C1"),
        principal="user", domain="channel", resource="slack-C1",
        bridge_instance="slack", self_authored=False,
        authorized_principals=frozenset({"user"}), source_kind=kind,
    )
    assert source.source_kind == kind
    assert replace(source).source_kind == kind
    assert json.loads(json.dumps({"source_kind": source.source_kind})) == {
        "source_kind": kind,
    }


def test_unclassified_source_kind_refuses_import():
    # A fresh process owns the enum and import state, independent of collection.
    result = subprocess.run(
        [sys.executable, "-c", """
from enum import StrEnum
from mimir import models
models.SourceKind = StrEnum('SourceKind', {
    **{member.name: member.value for member in models.SourceKind},
    'UNCLASSIFIED': 'unclassified',
})
import mimir.access_control
"""], capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "AssertionError: unclassified SourceKind" in result.stderr


def test_source_kind_flow_decisions_match_pre_enum_baseline():
    # Captured by executing this matrix against the unmodified string policy.
    # Covers every known kind plus open strings, with same/different/non-channel
    # resources, ACLs, bridge bindings, integrity, audiences, attestations, admin
    # bypass and cross-platform pull. Keep the ordering stable for the digest.
    decisions = []
    for (kind, resource, domain, bridge, acl, integrity, effect, admin,
         audience, attested, cross) in itertools.product(
        KINDS + UNKNOWN_KINDS,
        ("slack-C1", "slack-C2", "repo:x"), ("channel", "service"),
        ("slack", "other"), (frozenset(), frozenset({"user"})),
        ("trusted", "untrusted"), ("informational", "active_ingest"),
        (False, True),
        (None, frozenset(), frozenset({"user"}), frozenset({"user", "other"})),
        (False, True), (False, True),
    ):
        source = SourceLabel(
            principal="user", domain=domain, resource_id=resource,
            bridge_instance=bridge, sensitivity="private",
            authorized_principals=acl, source_kind=kind,
            integrity=integrity, integrity_effect=effect,
            owner_attestation=(
                _mint_owner_attestation("user", "raw", resource) if attested else None
            ),
        )
        provider = None if audience is None else SimpleNamespace(
            audience_for=lambda *a, **kw: audience,
            identity_resolver=SimpleNamespace(identity=lambda value: SimpleNamespace(
                canonical=value, access=SimpleNamespace(is_service=False),
            )),
        )
        kwargs = dict(
            effective_principal="user", triggering_principal="raw",
            resolved_triggering="slack-C1", audience_provider=provider,
            cross_platform_pull=cross, admin_operator_cross_channel=admin,
            triggering_bridge_instance="slack",
        )
        decision = _source_is_triggering_channel_compatible(source, **kwargs)
        if kind in KINDS:
            assert _source_is_triggering_channel_compatible(
                replace(source, source_kind=SourceKind(kind)), **kwargs,
            ) == decision
        decisions.append(decision)
    assert len(decisions) == 64512
    assert sum(decisions) == 22432
    assert hashlib.sha256(json.dumps(decisions).encode()).hexdigest() == (
        "7e573d3435b0e009bc7329e4267738d240e4f42b76ac10241d5b4b495c40ad5e"
    )
