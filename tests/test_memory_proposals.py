from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from mimir import _context
from mimir.access_control import (
    CapabilityTier, SAGA_TAINT_REFUSAL, build_trigger_service_principal,
    create_auth_context, get_tool_registry, saga_mutation_taint_refusal,
)
from mimir.memory_proposals import proposal_path
from mimir.models import AgentEvent, InformationFlowLabels, SourceLabel, TurnContext
from mimir.tools.memory_propose import memory_propose


@pytest.fixture
def proposal_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    service = build_trigger_service_principal(
        canonical="poller:papers", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("memory_propose", "read_file", "write_file", "edit_file", "ls"),
        roots=(tmp_path / "state" / "pollers" / "papers",), creation_path="test",
    )
    source = SourceLabel.from_record(dict(
        principal=None, domain="public", resource_id="feed:item:42",
        bridge_instance=None, sensitivity="public", source_kind="channel",
        integrity="untrusted", integrity_effect="active_ingest",
    ))
    labels = InformationFlowLabels(sources=(source,))
    auth = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="feed:item:42", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=labels)
    turn = TurnContext(turn_id="papers-turn-42", session_id=service.canonical,
                       trigger="poller", channel_id=service.canonical,
                       started_at=0, auth_context=auth)
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    runtime = ToolRuntime(state={}, context=auth, config={}, stream_writer=lambda _: None,
                          tool_call_id="proposal-test", store=None)

    def call(content="A useful fact", stream="semantic", rationale="It matters"):
        return asyncio.run(memory_propose.coroutine(
            content=content, stream=stream, rationale=rationale, runtime=runtime,
        ))

    return SimpleNamespace(home=tmp_path, auth=auth, turn=turn, call=call, labels=labels)


def _records(home):
    path = proposal_path(home)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_server_digest_batches_escapes_redacts_and_reminds_once(proposal_turn):
    from mimir.memory_proposals import post_review_digest

    env = proposal_turn
    first = await asyncio.to_thread(env.call, content="@everyone https://example.org ghp_" + "a" * 36)
    # The proposal tool refuses credentials; queue a normal adversarial string
    # and inject a token-shaped value in rationale to verify outbound redaction.
    assert "credential" in first
    first = await asyncio.to_thread(env.call, content="@everyone https://example.org", rationale="ghp_" + "a" * 36)
    second = await asyncio.to_thread(env.call, content="another fact")
    ids = [r["id"] for r in _records(env.home)]
    assert all(proposal_id in first + second for proposal_id in ids)
    messages = []

    async def send(channel, text, *, final):
        messages.append((channel, text))
        return SimpleNamespace(sent=True)

    now = datetime.now(timezone.utc)
    assert await post_review_digest(env.home, "operator", send, now=now)
    assert len(messages) == 1
    text = messages[0][1]
    assert all(proposal_id in text for proposal_id in ids)
    assert "derived from untrusted external content — approve only if true and worth keeping" in text
    assert "@everyone" not in text and "https://example.org" not in text
    assert "@\u200beveryone" in text and "https:\u200b//" in text
    assert "ghp_" + "a" * 36 not in text
    assert "\\[REDACTED\\]" in text
    assert "approve " + ids[0] in text and "decline " + ids[0] in text
    await asyncio.to_thread(env.call, content="third fact")
    third = _records(env.home)[-1]["id"]
    assert not await post_review_digest(env.home, "operator", send, now=now + timedelta(minutes=29))
    assert await post_review_digest(env.home, "operator", send, now=now + timedelta(minutes=31))
    assert third in messages[1][1] and all(i not in messages[1][1] for i in ids)
    assert await post_review_digest(env.home, "operator", send, now=now + timedelta(hours=24, minutes=32))
    assert len(messages) == 3 and all(proposal_id in messages[2][1] for proposal_id in (*ids, third))
    assert not await post_review_digest(env.home, "operator", send, now=now + timedelta(hours=26))
    assert len(messages) == 3


@pytest.mark.asyncio
async def test_digest_does_not_mark_failed_delivery(proposal_turn):
    from mimir.memory_proposals import post_review_digest

    env = proposal_turn
    await asyncio.to_thread(env.call)
    sent = []

    async def send(channel, text, *, final):
        sent.append(text)
        return SimpleNamespace(sent=len(sent) > 1)

    assert not await post_review_digest(env.home, "operator", send)
    assert await post_review_digest(env.home, "operator", send)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_corrupt_digest_state_refuses_delivery(proposal_turn):
    from mimir.memory_proposals import ProposalRefusal, _digest_path, post_review_digest

    await asyncio.to_thread(proposal_turn.call)
    _digest_path(proposal_turn.home).write_text('{"sent": "not a ledger", "last_sent": null}')

    async def send(*args, **kwargs):
        pytest.fail("corrupt delivery ledger must not result in a duplicate post")

    with pytest.raises(ProposalRefusal, match="digest state"):
        await post_review_digest(proposal_turn.home, "operator", send)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["declined", "approved", "expired"])
async def test_digest_never_reminds_a_decided_proposal(proposal_turn, status):
    from mimir.memory_proposals import _update, post_review_digest

    env = proposal_turn
    await asyncio.to_thread(env.call)
    proposal_id = _records(env.home)[0]["id"]
    messages = []

    async def send(channel, text, *, final):
        messages.append(text)
        return SimpleNamespace(sent=True)

    now = datetime.now(timezone.utc)
    assert await post_review_digest(env.home, "operator", send, now=now)
    _update(env.home, proposal_id, status)
    assert not await post_review_digest(env.home, "operator", send,
                                        now=now + timedelta(hours=25))
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_digest_excludes_expired_pending_proposals(proposal_turn):
    from mimir.memory_proposals import post_review_digest

    env = proposal_turn
    await asyncio.to_thread(env.call)
    record, = _records(env.home)
    record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    proposal_path(env.home).write_text(json.dumps(record) + "\n")

    async def send(*args, **kwargs):
        pytest.fail("expired pending proposal must not be delivered")

    assert not await post_review_digest(env.home, "operator", send)


def test_operator_cli_list_approve_edit_decline_uses_checked_store(proposal_turn, monkeypatch, capsys):
    import struct
    from mimir.cli import main
    from mimir.runtime import resolve_saga_db_path
    from mimir.saga.client import SagaStore

    env = proposal_turn
    monkeypatch.setattr("mimir.saga.client._embed_text_sync",
                        lambda text: (struct.pack("4f", 1, 0, 0, 0), "stub", "stub", 4))
    saga = SagaStore(db_path=resolve_saga_db_path(env.home), embedding_dim=4)
    saga._ensure_conn()
    first = env.call(content="fact one")
    second = env.call(content="fact two")
    ids = [r["id"] for r in _records(env.home)]
    assert all(i in first + second for i in ids)
    monkeypatch.setattr("mimir.commands.memory.getpass.getuser", lambda: "operator")

    def cli(*args):
        main(["memory", "proposals", *args])
        return 0

    assert cli("list") == 0
    output = capsys.readouterr().out
    assert all(i in output for i in ids)
    assert cli("approve", ids[0], "--text", "edited fact") == 0
    assert cli("decline", ids[1]) == 0
    assert cli("list") == 0
    assert "(no proposals)" in capsys.readouterr().out
    assert cli("list", "--status", "all") == 0
    output = capsys.readouterr().out
    assert all(i in output for i in ids)
    atom = saga._ensure_conn().execute("SELECT content, source_type, provenance FROM atoms").fetchone()
    assert atom[0] == "edited fact" and atom[1] == "operator_approved_proposal"
    assert json.loads(atom[2])["approved_by"] == "cli:operator"
    assert [r["status"] for r in _records(env.home)] == ["approved", "declined"]


@pytest.mark.parametrize("case", ["tampered", "expired", "decided", "blank_edit"])
def test_cli_approval_cannot_bypass_b3b_checks(proposal_turn, monkeypatch, capsys, case):
    from mimir.cli import main
    from mimir.memory_proposals import _update
    from mimir.saga.client import SagaStore
    from mimir.runtime import resolve_saga_db_path

    home = proposal_turn.home
    saga = SagaStore(db_path=resolve_saga_db_path(home), embedding_dim=4)
    saga._ensure_conn()
    proposal_turn.call()
    record, = _records(home)
    if case == "tampered":
        record["content"] = "altered after queue"
    elif case == "expired":
        record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    elif case == "decided":
        _update(home, record["id"], "declined")
    if case in {"tampered", "expired"}:
        proposal_path(home).write_text(json.dumps(record) + "\n")
    with pytest.raises(SystemExit) as exit_info:
        main(["memory", "proposals", "approve", record["id"],
              *(["--text", "  "] if case == "blank_edit" else [])])
    assert exit_info.value.code == 1
    assert {"tampered": "hash mismatch", "expired": "expired", "decided": "already decided",
            "blank_edit": "empty edit"}[case] in capsys.readouterr().out
    assert saga._ensure_conn().execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 0


def test_tainted_proposal_records_every_field_without_mutating_saga(proposal_turn, monkeypatch):
    env = proposal_turn
    from mimir.saga.client import SagaStore
    from mimir.tools.memory import _MEMORY_STATE

    saga_db = env.home / "saga.db"
    saga = SagaStore(db_path=saga_db, embedding_dim=4)
    # A misplaced SAGA write must reach the unchanged-store assertion, not
    # fail incidentally on ambient provider availability or vector dimensions.
    import struct

    monkeypatch.setattr(
        "mimir.saga.client._embed_text_sync",
        lambda text: (struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "stub", "stub", 4),
    )
    monkeypatch.setitem(_MEMORY_STATE, "client", saga)
    # Exercise a real write before taking the baseline: this fake embedder
    # supports the write path a faulty memory_propose implementation might use.
    asyncio.run(saga.store("Existing fixture fact", stream="semantic"))
    conn = saga._ensure_conn()
    before_atoms = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert before_atoms == 1
    before_tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    before = saga_db.read_bytes()
    content = "Fact with newline\nkept verbatim"
    result = env.call(content=content)
    record, = _records(env.home)
    assert re.search(r"Proposed memory mp-[a-z2-7]{4} queued", result)
    assert "nothing is stored yet" in result and "do not notify the operator" in result
    assert record == {
        "id": record["id"], "content": content, "stream": "semantic",
        "rationale": "It matters", "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "proposed_by": env.auth.canonical_principal or env.auth.principal,
        "turn_id": "papers-turn-42", "origin_trigger": "poller",
        "origin_ref": env.auth.origin_ref,
        "ifc_sources": [{
            "source_kind": env.labels.sources[0].source_kind,
            "integrity": "untrusted", "integrity_effect": "active_ingest",
            "resource_id": "feed:item:42",
        }],
        "created_at": record["created_at"], "expires_at": record["expires_at"],
        "status": "pending",
    }
    assert record["id"] in result
    assert datetime.fromisoformat(record["expires_at"]) - datetime.fromisoformat(record["created_at"]) == timedelta(days=7)
    assert saga_db.read_bytes() == before
    assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == before_atoms
    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == before_tables


def test_clean_turn_refuses_directs_to_memory_store(proposal_turn):
    env = proposal_turn
    clean_labels = InformationFlowLabels(sources=(
        SourceLabel.from_record(dict(
            principal=None, domain="public", resource_id="trusted:item",
            bridge_instance=None, sensitivity="public", source_kind="channel",
            integrity="trusted", integrity_effect="active_ingest",
        )),
    ))
    clean = create_auth_context(AgentEvent(
        trigger="poller", channel_id=env.auth.channel_id, source="poller",
        source_id="trusted:item", service_principal=env.auth.channel_id,
        service_authority=env.auth.service_authority,
    ), enforce=True, ifc_labels=clean_labels)
    env.turn.auth_context = clean
    runtime = ToolRuntime(state={}, context=clean, config={}, stream_writer=lambda _: None,
                          tool_call_id="clean-test", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "this turn can store directly; use memory_store" in result
    assert _records(env.home) == []


@pytest.mark.parametrize("content,reason", [
    ("ghp_" + "a" * 36, "credential"),
    ("x" * 501, "500"),
])
def test_invalid_content_writes_nothing(proposal_turn, content, reason):
    assert reason in proposal_turn.call(content=content)
    assert _records(proposal_turn.home) == []


def test_queue_strips_content_before_hashing_and_rejects_blank(proposal_turn):
    assert "queued" in proposal_turn.call(content="  some fact\n")
    record, = _records(proposal_turn.home)
    assert record["content"] == "some fact"
    assert record["content_sha256"] == hashlib.sha256(b"some fact").hexdigest()
    assert "content is required" in proposal_turn.call(content=" \n\t ")
    assert len(_records(proposal_turn.home)) == 1


def test_store_guard_survives_writable_roots_including_mimir(proposal_turn):
    from mimir.readonly_backend import WriteGuardBackend

    proposal_turn.call()
    backend = WriteGuardBackend(root_dir=proposal_turn.home, writable_dirs=[".mimir", "."])
    path = str(proposal_path(proposal_turn.home))
    assert backend.write(path, "replacement").error
    assert backend.edit(path, "pending", "approved").error
    wide = build_trigger_service_principal(
        canonical="poller:wide", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("write_file", "edit_file"), roots=(proposal_turn.home / ".mimir",),
        creation_path="test",
    )
    auth = create_auth_context(AgentEvent(
        trigger="poller", channel_id=wide.canonical, source="poller",
        source_id="wide", service_principal=wide.canonical, service_authority=wide,
    ), enforce=True, ifc_labels=proposal_turn.labels)
    for tool in ("write_file", "edit_file"):
        decision = get_tool_registry().authorize_tool(
            tool, auth, enforce=True, target_channel=path,
            arguments={"file_path": path}, ifc_labels=proposal_turn.labels,
        )
        assert not decision.allowed and decision.reason == "protected_memory_proposal_path"
    assert _records(proposal_turn.home)[0]["status"] == "pending"


def test_duplicate_pending_proposal_is_refused(proposal_turn):
    env = proposal_turn
    assert "queued" in env.call()
    assert "duplicate" in env.call(rationale="different why")
    assert len(_records(env.home)) == 1


def test_21st_pending_per_principal_is_refused(proposal_turn):
    env = proposal_turn
    for index in range(20):
        assert "queued" in env.call(content=f"Fact {index}")
    assert "20 pending proposals" in env.call(content="Fact 20")
    assert len(_records(env.home)) == 20


def test_file_tools_cannot_access_proposal_path(proposal_turn):
    env = proposal_turn
    env.call()
    paths = (
        str(proposal_path(env.home)), "/.mimir/memory-proposals.jsonl",
        ".mimir/memory-proposals.jsonl", str(proposal_path(env.home).parent),
        "/.mimir", ".mimir",
    )
    registry = get_tool_registry()
    human = create_auth_context(AgentEvent(
        trigger="user_message", channel_id="web-operator", source="web",
        source_id="message-1", author="viewer",
    ), enforce=True, ifc_labels=env.labels)
    for auth in (env.auth, replace(env.auth, roles=("admin",)), human):
        for path in paths:
            for tool in (
                "read_file", "aread", "write_file", "edit_file", "ls", "als",
                "glob", "aglob", "grep", "agrep",
            ):
                key = "file_path" if tool in {"read_file", "aread", "write_file", "edit_file"} else "path"
                decision = registry.authorize_tool(
                    tool, auth, enforce=True, target_channel=path,
                    arguments={key: path}, ifc_labels=env.labels,
                )
                assert not decision.allowed, (tool, path, decision)
                assert decision.reason == "protected_memory_proposal_path"


def test_backend_keeps_store_out_of_admin_reads_and_broad_searches(proposal_turn):
    from mimir.readonly_backend import WriteGuardBackend

    env = proposal_turn
    env.call()
    env.turn.auth_context = replace(env.auth, roles=("admin",), service_authority=None)
    backend = WriteGuardBackend(root_dir=env.home, writable_dirs=[".mimir", "state"])
    for path in (str(proposal_path(env.home)), "/.mimir/memory-proposals.jsonl", ".mimir/memory-proposals.jsonl"):
        assert backend.read(path).error
        assert asyncio.run(backend.aread(path)).error
        assert backend.write(path, "replacement").error
        assert backend.edit(path, "pending", "approved").error
    for path in (str(proposal_path(env.home).parent), "/.mimir", ".mimir"):
        assert backend.ls(path).error
        assert asyncio.run(backend.als(path)).error
    (env.home / "public.jsonl").write_text("A useful fact\n")
    for result in (backend.glob("*.jsonl", "/"), asyncio.run(backend.aglob("*.jsonl", "/"))):
        assert [match["path"] for match in result.matches] == ["/public.jsonl"]
    for result in (backend.grep("A useful fact", "/"), asyncio.run(backend.agrep("A useful fact", "/"))):
        assert [match["path"] for match in result.matches] == ["/public.jsonl"]
    listing = backend.ls("/")
    assert any(entry["path"] == "/public.jsonl" for entry in listing.entries)
    assert not any(".mimir" in entry["path"] for entry in listing.entries)


@pytest.mark.parametrize("alias_kind", ["hard_link", "directory_case", "filename_case"])
def test_store_identity_aliases_are_protected(proposal_turn, alias_kind):
    from mimir.memory_proposals import is_protected_proposal_path
    from mimir.readonly_backend import WriteGuardBackend

    env = proposal_turn
    assert "queued" in env.call()
    store = proposal_path(env.home)
    if alias_kind == "hard_link":
        alias = env.home / "proposal-alias.jsonl"
        alias.hardlink_to(store)
    else:
        # Exercise native case equivalence only where the fixture filesystem
        # supports it; the hard-link case proves identity matching everywhere.
        probe = env.home / "case-probe"
        probe.write_text("fixture")
        if not (env.home / "CASE-PROBE").exists():
            pytest.skip("fixture filesystem is case-sensitive")
        alias = (env.home / ".MIMIR" / store.name if alias_kind == "directory_case"
                 else store.with_name("MEMORY-PROPOSALS.JSONL"))
    assert alias.samefile(store)
    assert is_protected_proposal_path(alias)
    if alias_kind == "directory_case":
        assert is_protected_proposal_path(alias.parent)

    admin = replace(env.auth, roles=("admin",), service_authority=None)
    env.turn.auth_context = admin
    backend = WriteGuardBackend(root_dir=env.home, writable_dirs=["."])
    virtual = "/" + alias.relative_to(env.home).as_posix()
    for path in (str(alias), virtual):
        assert backend.read(path).error
        assert asyncio.run(backend.aread(path)).error
        assert backend.write(path, "replacement").error
        assert backend.edit(path, "pending", "approved").error
        for auth in (env.auth, admin):
            for tool in ("read_file", "aread", "write_file", "edit_file", "glob", "grep"):
                key = "path" if tool in {"glob", "grep"} else "file_path"
                decision = get_tool_registry().authorize_tool(
                    tool, auth, enforce=True, target_channel=path,
                    arguments={key: path}, ifc_labels=env.labels,
                )
                assert not decision.allowed
                assert decision.reason == "protected_memory_proposal_path"
    public = env.home / "public.jsonl"
    public.write_text("A useful fact\n")
    assert [match["path"] for match in backend.glob("*.jsonl", "/").matches] == ["/public.jsonl"]
    assert [match["path"] for match in backend.grep("A useful fact", "/").matches] == ["/public.jsonl"]


@pytest.mark.parametrize("principal", ["poller", "human", "admin"])
@pytest.mark.parametrize("alias_kind", ["ordinary", "hard_link", "directory_case", "filename_case"])
def test_outbox_identity_and_backend_matrix(proposal_turn, principal, alias_kind):
    from mimir.memory_proposals import is_protected_model_path
    from mimir.readonly_backend import WriteGuardBackend
    from mimir.read_policy import is_protected_read_path

    env = proposal_turn
    live = env.home / "state/social-outbox/feed/x.yaml"
    live.parent.mkdir(parents=True)
    live.write_text("dispatch: public-outbox-marker\n")
    alias = live
    if alias_kind == "hard_link":
        alias = env.home / "outbox-alias.yaml"
        alias.hardlink_to(live)
    elif alias_kind != "ordinary":
        probe = env.home / "case-probe"
        probe.write_text("fixture")
        if not (env.home / "CASE-PROBE").exists():
            pytest.skip("fixture filesystem is case-sensitive")
        alias = (env.home / "State/Social-Outbox/feed/x.yaml"
                 if alias_kind == "directory_case" else live.with_name("X.YAML"))
    assert alias.samefile(live)
    assert is_protected_model_path(alias)
    assert is_protected_read_path(alias)
    auth = env.auth
    if principal != "poller":
        auth = create_auth_context(AgentEvent(
            trigger="user_message", channel_id="web-operator", source="web",
            source_id="message-1", author="viewer",
        ), enforce=True, ifc_labels=env.labels)
        if principal == "admin":
            auth = replace(auth, roles=("admin",))
    env.turn.auth_context = auth
    backend = WriteGuardBackend(root_dir=env.home, writable_dirs=["."])
    virtual = "/" + alias.relative_to(env.home).as_posix()
    paths = (str(alias), virtual, virtual.lstrip("/"),
             virtual.replace("/feed/", "/./feed/"), "/" + virtual)
    for path in paths:
        for tool in ("read_file", "aread", "ls", "als", "glob", "aglob",
                     "grep", "agrep", "write_file", "edit_file"):
            key = "file_path" if tool in {"read_file", "aread", "write_file", "edit_file"} else "path"
            decision = get_tool_registry().authorize_tool(
                tool, auth, enforce=False, target_channel=path, arguments={key: path},
            )
            assert not decision.allowed, (principal, tool, path)
        assert backend.read(path).error
        assert asyncio.run(backend.aread(path)).error
        assert backend.ls(path).error
        assert backend.write(path, "replacement").error
        assert backend.edit(path, "public", "replacement").error
    (env.home / "public.yaml").write_text("public-outbox-marker\n")
    for result in (backend.glob("*.yaml", "/"), asyncio.run(backend.aglob("*.yaml", "/")),
                   backend.grep("public-outbox-marker", "/"),
                   asyncio.run(backend.agrep("public-outbox-marker", "/"))):
        assert all(match["path"] == "/public.yaml" for match in result.matches)
        if principal == "admin":
            assert result.matches
    assert not any("social-outbox" in entry["path"] for entry in backend.ls("/state").entries)


def test_outbox_protection_before_creation_and_worktree_exclusion(proposal_turn):
    from mimir.memory_proposals import is_protected_model_path

    home = proposal_turn.home
    assert is_protected_model_path(home / "state/social-outbox/feed/new.yaml")
    assert not is_protected_model_path(home / "scratch/proposals/state/social-outbox/feed/new.yaml")


@pytest.mark.parametrize("relative,protected", [
    ("memory/channels/channel-b/loop/summary.md", False),
    (".mimir/memory-proposals.jsonl", True),
    ("state/social-outbox/feed/loop/post.yaml", True),
])
def test_unresolvable_paths_keep_protected_classification(
    proposal_turn, monkeypatch, relative, protected,
):
    import errno
    from mimir.memory_proposals import is_protected_model_path

    candidate = proposal_turn.home / relative
    original_resolve = Path.resolve

    def python313_resolve(path, strict=False):
        if path == candidate:
            if strict:
                raise OSError(errno.ELOOP, "Too many levels of symbolic links")
            # Python 3.13's non-strict resolution can retain a symlink loop.
            return path
        return original_resolve(path, strict=strict)

    def failed_identity(*args):
        raise OSError(errno.ELOOP, "Too many levels of symbolic links")

    monkeypatch.setattr(Path, "resolve", python313_resolve)
    monkeypatch.setattr("mimir.memory_proposals.same_model_target", failed_identity)
    assert is_protected_model_path(candidate) is protected


def test_protected_path_before_store_creation(proposal_turn):
    from mimir.memory_proposals import is_protected_proposal_path

    store = proposal_path(proposal_turn.home)
    assert not store.exists()
    assert is_protected_proposal_path(store)
    assert is_protected_proposal_path(store.parent)
    assert not is_protected_proposal_path(proposal_turn.home / "public.jsonl")


@pytest.mark.parametrize("bad_record", [
    {}, [], None, 42, "record", {"status": "pending"},
    {"status": "pending", "content_sha256": [], "proposed_by": "p", "id": "mp-1"},
    *({key: value for key, value in {"status": "pending", "content_sha256": "hash", "proposed_by": "p", "id": "mp-1"}.items() if key != omitted}
      for omitted in ("status", "content_sha256", "proposed_by", "id")),
])
def test_malformed_store_refuses_without_writing(proposal_turn, bad_record):
    env = proposal_turn
    assert "queued" in env.call()
    path = proposal_path(env.home)
    with path.open("a") as handle:
        handle.write(json.dumps(bad_record) + "\n")
    before = path.read_bytes()
    assert "malformed proposal store record at line 2" in env.call(content="Another fact")
    assert path.read_bytes() == before


def test_invalid_json_store_refuses_without_writing(proposal_turn):
    env = proposal_turn
    assert "queued" in env.call()
    path = proposal_path(env.home)
    with path.open("a") as handle:
        handle.write("not json\n")
    before = path.read_bytes()
    assert "malformed proposal store record at line 2" in env.call(content="Another fact")
    assert path.read_bytes() == before


@pytest.mark.parametrize("principal", ["admin", "granted_service", "ungranted_service"])
@pytest.mark.parametrize("override", [None, False, True])
def test_taint_refusal_hint_and_tool_share_capability_gate(proposal_turn, monkeypatch, principal, override):
    from mimir import access_control

    env = proposal_turn
    if principal == "admin":
        auth = replace(env.auth, roles=("admin",), service_authority=None)
    elif principal == "granted_service":
        auth = env.auth
    else:
        service = build_trigger_service_principal(
            canonical="poller:other", trigger="poller", profile="research",
            tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
            capabilities=("memory_store",), roots=(env.home / "state",), creation_path="test",
        )
        auth = create_auth_context(AgentEvent(
            trigger="poller", channel_id=service.canonical, source="poller",
            source_id="other", service_principal=service.canonical,
            service_authority=service,
        ), enforce=True, ifc_labels=env.labels)
    env.turn.auth_context = auth
    expected = principal != "ungranted_service" if override is None else override
    if override is not None:
        # Changing the one shared gate must affect both consumers, rather than
        # allowing copied permission checks to pass the agreement test.
        monkeypatch.setattr(access_control, "can_propose_memory", lambda context: override)
    assert access_control.can_propose_memory(auth) is expected
    refusal = saga_mutation_taint_refusal(auth)
    assert ("memory_propose" in refusal) is expected
    if not expected:
        assert refusal == SAGA_TAINT_REFUSAL
    runtime = ToolRuntime(state={}, context=auth, config={}, stream_writer=lambda _: None,
                          tool_call_id="gate-agreement", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert ("queued" in result) is expected
    assert len(_records(env.home)) == int(expected)
    if not expected:
        assert "write access denied" in result


def test_missing_ifc_sources_cannot_queue(proposal_turn):
    env = proposal_turn
    from mimir.models import InformationFlowState

    empty = replace(env.auth, ifc_labels=InformationFlowLabels(),
                    ifc_state=InformationFlowState(labels=InformationFlowLabels()))
    env.turn.auth_context = empty
    runtime = ToolRuntime(state={}, context=empty, config={}, stream_writer=lambda _: None,
                          tool_call_id="missing-ifc", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "missing IFC source provenance" in result
    assert _records(env.home) == []


def test_authority_and_turn_id_are_required(proposal_turn):
    env = proposal_turn

    service = build_trigger_service_principal(
        canonical="poller:other", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE, capabilities=("memory_store",),
        roots=(env.home / "state",), creation_path="test",
    )
    unauthorized = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="other", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=env.labels)
    env.turn.auth_context = unauthorized
    runtime = ToolRuntime(state={}, context=unauthorized, config={}, stream_writer=lambda _: None,
                          tool_call_id="unauthorized", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "write access denied" in result
    # The authorized context is not the context of this live turn.
    runtime = ToolRuntime(state={}, context=env.auth, config={}, stream_writer=lambda _: None,
                          tool_call_id="wrong-turn", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "missing authoritative turn id" in result
    assert _records(env.home) == []
