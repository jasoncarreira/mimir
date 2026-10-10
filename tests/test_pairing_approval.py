"""Operator pairing decisions are authenticated, channel-bound and never tools."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir import approval_requests
from mimir.admin_users import build_users_payload
from mimir.identities import IdentityResolver
from mimir.identities_populator import approve_pairing, reject_pairing, request_pairing_with_code
from mimir.models import AgentEvent
from mimir.pairing_approval import complete_reply, sync_pending


def _setup(home: Path):
    request_pairing_with_code(home, "discord-123", "discord", channel_id="dm-discord-123", is_dm=True)
    request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    path = home / "state" / "identities.yaml"
    import yaml
    doc = yaml.safe_load(path.read_text())
    doc["people"].append({"canonical": "operator", "aliases": ["discord-99"],
                          "access": {"roles": ["admin"]}})
    path.write_text(yaml.safe_dump(doc))
    resolver = IdentityResolver(home)
    resolver.reload()
    return resolver, {p["canonical"]: p["pairing"]["request_id"] for p in doc["people"] if "pairing" in p}


@pytest.mark.parametrize("decision,author,source,channel,approved", [
    ("approve", "discord-99", "discord", "ops", True),
    ("decline", "discord-99", "discord", "ops", False),
    ("approve", "discord-123", "discord", "ops", None),
    ("approve", "discord-99", "web", "ops", None),
    ("approve", "discord-99", "api", "ops", None),
    ("approve", "discord-99", "stdin", "ops", None),
    ("approve", "discord-99", "discord", "other", None),
])
async def test_reply_requires_operator_and_channel(tmp_path, decision, author, source, channel, approved):
    resolver, ids = _setup(tmp_path)
    request_id = ids["slack-U1"]
    sync_pending(tmp_path, "ops", resolver)
    event = AgentEvent(trigger="user_message", author=author, source=source,
                       channel_id=channel, content=f"{decision} {request_id}")
    resolution = approval_requests.resolve(event, resolver)
    if approved is None:
        assert resolution.entry is None
        assert not resolver.is_authorized("slack-U1")
        return
    assert resolution.entry.approval_id == request_id
    notice = await complete_reply(tmp_path, "ops", event, resolution, resolver)
    assert request_id in notice
    assert resolver.is_authorized("slack-U1") is approved
    assert resolver.identity("slack-U1").pairing.status == ("approved" if approved else "rejected")
    assert resolver.access_metadata("slack-U1").roles == (("user",) if approved else ())
    assert resolver.identity("slack-U1").pairing.request_id is None
    assert resolver.identity("discord-123").pairing.status == "pending"


async def test_restart_expiry_and_stale_id(tmp_path):
    resolver, ids = _setup(tmp_path)
    sync_pending(tmp_path, "ops", resolver)
    for request_id in ids.values():
        approval_requests.cancel(request_id)
    # Recreate a registry from persisted state (in a new interpreter it is empty).
    # Cancel reserves IDs for replay protection, so remove only our owned IDs.
    for request_id in ids.values():
        approval_requests._RECENT.pop(request_id, None)
    sync_pending(tmp_path, "ops", resolver)
    assert {entry.approval_id for entry in approval_requests.pending("ops") if entry.kind == "pair"} >= set(ids.values())
    import yaml
    path = tmp_path / "state" / "identities.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["people"][0]["pairing"]["requested_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    path.write_text(yaml.safe_dump(doc))
    sync_pending(tmp_path, "ops", resolver)
    assert ids["discord-123"] not in {e.approval_id for e in approval_requests.pending("ops")}
    assert build_users_payload(resolver)["users"][0]["pairing"]["request_id"] == ids["discord-123"]


def test_no_pairing_mutations_are_model_tools():
    tools_dir = Path(__file__).resolve().parents[1] / "mimir" / "tools"
    forbidden = {"approve_pairing", "approve_pairing_code", "reject_pairing",
                 "admin_users_pairing_approve_v1", "admin_users_pairing_reject_v1"}
    for module in tools_dir.glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
                assert "identities_populator" not in (node.module or "")
                assert "pairing_approval" not in (node.module or "")
            if isinstance(node, ast.Import):
                assert not any("identities_populator" in alias.name or "pairing_approval" in alias.name
                               for alias in node.names)
        assert not forbidden & names, f"pairing approval reachable from {module.name}"


def test_pairing_mutations_require_matching_request_id(tmp_path):
    resolver, ids = _setup(tmp_path)
    before = (tmp_path / "state" / "identities.yaml").read_bytes()
    assert not approve_pairing(tmp_path, "discord-123", pending_only=True,
                               request_id=ids["slack-U1"])
    assert not reject_pairing(tmp_path, "discord-123", request_id=ids["slack-U1"])
    assert (tmp_path / "state" / "identities.yaml").read_bytes() == before
    assert resolver.identity("discord-123").pairing.status == "pending"


@pytest.mark.parametrize("kind", ["mp", "op"])
@pytest.mark.parametrize("reference_pair", [False, True])
def test_bare_reply_ignores_pairings_before_candidate_selection(tmp_path, kind, reference_pair):
    import time

    resolver, ids = _setup(tmp_path)
    channel = f"ops-{tmp_path.name}"
    sync_pending(tmp_path, channel, resolver)
    pair_id = ids["slack-U1"]
    approval_requests.set_prompt_message_id(pair_id, "pair-prompt")
    calls = []
    entry = approval_requests.register(
        kind=kind, channel_id=channel, description="ordinary approval",
        expires_at=time.monotonic() + 3600,
        resolver=lambda *args: calls.append(args[0]) or "granted",
    )
    try:
        event = AgentEvent(
            trigger="user_message", author="discord-99", source="discord",
            channel_id=channel, content="approve",
            extra={"reply_to_message_id": "pair-prompt"} if reference_pair else {},
        )
        result = approval_requests.resolve(event, resolver)
        assert result.entry == entry and result.status == "granted"
        assert calls == ["approve"]
        assert pair_id in {e.approval_id for e in approval_requests.pending(channel)}
        assert resolver.identity("slack-U1").pairing.status == "pending"
    finally:
        approval_requests.cancel(entry.approval_id)
        for request_id in ids.values():
            approval_requests.cancel(request_id)


@pytest.mark.parametrize("author,source,channel,bare", [
    ("discord-123", "discord", "ops", False),
    ("discord-99", "web", "ops", False),
    ("discord-99", "api", "ops", False),
    ("discord-99", "stdin", "ops", False),
    ("discord-99", "discord", "other", False),
    ("discord-99", "discord", "ops", True),
])
async def test_complete_reply_directly_rejects_unauthorized_or_unnamed_events(
    tmp_path, author, source, channel, bare,
):
    resolver, ids = _setup(tmp_path)
    request_id = ids["slack-U1"]
    sync_pending(tmp_path, "ops", resolver)
    valid = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id="ops", content=f"approve {request_id}")
    resolution = approval_requests.resolve(valid, resolver)
    assert resolution.entry.approval_id == request_id
    before = (tmp_path / "state" / "identities.yaml").read_bytes()
    event = AgentEvent(
        trigger="user_message", author=author, source=source, channel_id=channel,
        content="approve" if bare else valid.content,
        extra={"_pairing_action": valid.extra["_pairing_action"]},
    )
    try:
        assert await complete_reply(tmp_path, "ops", event, resolution, resolver) == "no pending request"
        assert (tmp_path / "state" / "identities.yaml").read_bytes() == before
        assert not resolver.is_authorized("slack-U1")
        assert request_id in {e.approval_id for e in approval_requests.pending("ops")}
    finally:
        for owned_id in ids.values():
            approval_requests.cancel(owned_id)


def test_no_operator_channel_skips_chat_registration(tmp_path, monkeypatch):
    resolver, ids = _setup(tmp_path)
    def unexpected_register(**kwargs):
        pytest.fail("attempted to register a pairing without an alert channel")
    monkeypatch.setattr(approval_requests, "register", unexpected_register)
    sync_pending(tmp_path, "", resolver)
    assert not any(entry.approval_id in ids.values()
                   for entry in approval_requests.pending("ops"))
