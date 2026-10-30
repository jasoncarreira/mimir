"""In-server trust attestation for tracker poller items.

The poller process is not trusted to say who wrote an issue. The host calls
``attest_item`` with the validated extras of one item; it re-reads the issue from
the tracker, re-applies the delivery rule and returns the prompt it would have
rendered. The host, not this module, compares that prompt with the emitted one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from mimir.poller_hooks import ItemAttestation, PollerHooks
from mimir.poller_tracker import (
    TRACKER_KINDS,
    safe_adapter,
    safe_event_id,
    safe_ref,
    safe_url,
)

from ..worklink.backends.registry import WorklinkConfig
from . import transport as tracker_transport
from .adapters import build_adapter
from .prompts import render_prompt
from .thread import Decision, TrackerIssue, delivery_decision
from .transport import TrackerUnavailable

REFUSED = ItemAttestation(False)
UNAVAILABLE = ItemAttestation(None)


def attest_item(extras: Mapping[str, Any], env: Mapping[str, str]) -> ItemAttestation:
    """Return the attestation for one tracker item described by ``extras``."""

    adapter_name = safe_adapter(extras.get("adapter"))
    ref = safe_ref(extras.get("ref"))
    url = extras.get("url")
    event_id = safe_event_id(extras.get("attested_event_id"))
    kind = extras.get("kind")
    if (
        adapter_name is None
        or ref is None
        or event_id is None
        or kind not in TRACKER_KINDS
        or safe_url(url, ref) is None
    ):
        return REFUSED
    home = env.get("MIMIR_HOME")
    if not home:
        return UNAVAILABLE
    try:
        tracker = WorklinkConfig.load(Path(home) / "worklink.yaml").tracker
    except (OSError, ValueError, yaml.YAMLError):
        return UNAVAILABLE
    adapter_config = tracker.adapters.get(adapter_name)
    if adapter_config is None:
        return REFUSED
    try:
        adapter = build_adapter(
            adapter_name, adapter_config, env, tracker_transport.default_transport()
        )
        issue = adapter.read(ref)
        decision = delivery_decision(issue, adapter.trust)
    except TrackerUnavailable:
        return UNAVAILABLE
    except ValueError:
        return REFUSED
    if issue.adapter != adapter_name or issue.ref != ref:
        return REFUSED
    return attestation_for(issue, decision, str(kind), event_id, url=str(url))


def attestation_for(
    issue: TrackerIssue, decision: Decision, kind: str, event_id: str, *, url: str | None = None
) -> ItemAttestation:
    """Turn a delivery ``decision`` into the verdict for the item naming ``event_id``.

    When ``url`` is given the issue's canonical url must equal it. Pure: no
    network access, so the contract harness can reuse it on a decision it already made.
    """

    if decision.kind == "unavailable":
        return UNAVAILABLE
    if (
        decision.kind != "deliver"
        or decision.cutoff is None
        or decision.cutoff.id != event_id
        or (url is not None and issue.url != url)
    ):
        return REFUSED
    return ItemAttestation(
        True, actor=decision.cutoff.actor_id, prompt=render_prompt(kind, issue)
    )


def tracker_poller_hooks() -> PollerHooks:
    return PollerHooks(trust_attestor=attest_item)
