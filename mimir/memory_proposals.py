"""Server-owned, untrusted memory proposals awaiting human attestation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from contextlib import contextmanager
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import approval_requests
from .redaction import redact_text


# Configured only by the server's Agent; a tool cannot install an approval.
_APPROVAL_BACKENDS: dict[Path, tuple[str, Any]] = {}
_NAMED_MP = re.compile(r"^(?:approve|decline)\s+(mp-[a-z2-7]{4})(?:\s*:\s*.*)?$", re.I | re.S)


class ProposalRefusal(ValueError):
    """A proposal cannot be queued without changing the store."""


class ProposalStoreCorrupt(ProposalRefusal):
    """A malformed record, with a safe location for operator telemetry."""

    def __init__(self, line_number: int):
        self.line_number = line_number
        super().__init__(f"malformed proposal store record at line {line_number}")


def proposal_path(home: Path) -> Path:
    return home / ".mimir" / "memory-proposals.jsonl"


def same_model_target(target: Path, protected: Path) -> bool:
    """Identity comparison, including case-equivalent names before creation."""
    try:
        return target.samefile(protected)
    except FileNotFoundError:
        if target.parent == target or protected.parent == protected:
            return target == protected
        if not same_model_target(target.parent, protected.parent):
            return False
        if target.name == protected.name:
            return True
        if target.exists() or protected.exists():
            return False
        parent = protected.parent
        while not parent.exists():
            parent = parent.parent
        with tempfile.TemporaryDirectory(prefix=".mimir-path-probe-", dir=parent) as probe:
            reference = Path(probe) / protected.name
            reference.touch()
            try:
                return (Path(probe) / target.name).samefile(reference)
            except FileNotFoundError:
                return False


def protected_model_targets(home: Path) -> tuple[tuple[Path, bool], ...]:
    """Server-owned live targets; bool means the whole directory is protected.

    Enumerate live outbox files as well as its root so hard links outside that
    directory retain protection. Never cache: operator merges replace inodes.
    Proposal worktrees are not live targets.
    """
    store = proposal_path(home)
    outbox = home / "state/social-outbox"
    targets = [(store, False), (store.with_suffix(".lock"), False),
               (store.parent, False), (outbox, True)]
    if store.parent.exists():
        targets.extend((path, False) for path in store.parent.glob(".memory-proposals-*"))
    if outbox.exists():
        targets.extend((path, False) for path in outbox.rglob("*") if path.is_file())
    return tuple(targets)


def is_protected_model_path(candidate: Path) -> bool:
    """One unconditional file boundary for all principals and file backends."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return False
    try:
        # Python 3.13 leaves symlink loops unresolved with strict=False.
        # Detect those errors before identity checks so unrelated bad paths
        # retain their backend/service denial rather than a store denial.
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            # Future creates still need lexical and case-identity protection.
            resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        # Keep unresolved protected spellings closed; unrelated resolution
        # failures retain the existing backend/service denial and its reason.
        root = Path(home)
        store = proposal_path(root)
        outbox = root / "state/social-outbox"
        return (candidate in {store, store.parent}
                or candidate == outbox or candidate.is_relative_to(outbox))
    try:
        for target, recursive in protected_model_targets(Path(home).resolve()):
            protected = target.resolve()
            if resolved == protected or (recursive and resolved.is_relative_to(protected)):
                return True
            candidates = (resolved, *resolved.parents) if recursive else (resolved,)
            if any(same_model_target(path, protected) for path in candidates):
                return True
        return False
    except (OSError, RuntimeError, ValueError):
        # A failed protected-target identity lookup must not grant access.
        return True


def is_protected_proposal_path(candidate: Path) -> bool:
    """Compatibility name for the shared protected-model boundary."""
    return is_protected_model_path(candidate)


@contextmanager
def _store_lock(home: Path, *, exclusive: bool):
    """Lock a stable sidecar inode, including across atomic store replacement."""
    path = proposal_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ProposalRefusal("proposal directory must not be a symlink")
    lock_path = path.with_suffix(".lock")
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def _load_records(home: Path) -> list[dict[str, Any]]:
    """Read under the sidecar lock; refuse the whole store on corruption."""
    path = proposal_path(home)
    if not path.exists():
        return []
    records = []
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            try:
                record = json.loads(line)
                required = ("status", "content_sha256", "proposed_by", "id", "content",
                            "stream", "turn_id", "expires_at")
                if not isinstance(record, dict) or any(
                    not isinstance(record.get(key), str) or not record[key].strip()
                    for key in required
                ):
                    raise ValueError("invalid proposal fields")
                deadline = datetime.fromisoformat(record["expires_at"])
                if deadline.tzinfo is None:
                    raise ValueError("expiry must have a timezone")
            except (ValueError, TypeError):
                raise ProposalStoreCorrupt(line_number) from None
            records.append(record)
    return records


def _replace_records(home: Path, records: list[dict[str, Any]]) -> None:
    """Publish complete, fsynced bytes while holding the sidecar lock."""
    path = proposal_path(home)
    fd, temporary = tempfile.mkstemp(prefix=".memory-proposals-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.writelines(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def queue_proposal(
    home: Path, *, content: str, stream: str, rationale: str,
    proposed_by: str, turn_id: str, origin_trigger: str, origin_ref: str | None,
    sources: tuple[Any, ...],
) -> str:
    """Check limits and append one JSONL record atomically across processes."""
    if not isinstance(content, str) or not content.strip():
        raise ProposalRefusal("content is required")
    content = content.strip()
    if len(content) > 500:
        raise ProposalRefusal("content exceeds 500 characters")
    if stream not in {"semantic", "episodic", "procedural"}:
        raise ProposalRefusal("stream must be semantic, episodic, or procedural")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ProposalRefusal("rationale is required")
    if not proposed_by or not turn_id or not origin_trigger or not sources:
        raise ProposalRefusal("missing server-owned proposal provenance")

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _store_lock(home, exclusive=True):
        records = _load_records(home)
        pending = [record for record in records if record["status"] == "pending"]
        if any(record["content_sha256"] == digest for record in pending):
            raise ProposalRefusal("duplicate pending proposal")
        if sum(record["proposed_by"] == proposed_by for record in pending) >= 20:
            raise ProposalRefusal("principal has 20 pending proposals (limit reached)")
        existing_ids = {record["id"] for record in records}
        proposal_id = approval_requests.mint_id("mp", excluded=frozenset(existing_ids))
        now = datetime.now(timezone.utc)
        record = {
            "id": proposal_id,
            "content": content,
            "stream": stream,
            "rationale": rationale,
            "content_sha256": digest,
            "proposed_by": proposed_by,
            "turn_id": turn_id,
            "origin_trigger": origin_trigger,
            "origin_ref": origin_ref,
            "ifc_sources": [
                {
                    "source_kind": source.source_kind,
                    "integrity": source.integrity,
                    "integrity_effect": source.integrity_effect,
                    "resource_id": source.resource_id,
                }
                for source in sources
            ],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=7)).isoformat(),
            "status": "pending",
        }
        records.append(record)
        _replace_records(home, records)
    sync_pending(home)
    return proposal_id


def configure_approvals(home: Path, channel_id: str, saga_store: Any) -> None:
    """Re-register surviving requests after restart, against the operator channel."""
    if channel_id and saga_store is not None:
        _APPROVAL_BACKENDS[home] = (channel_id, saga_store)
        sync_pending(home)


def _update(home: Path, proposal_id: str, status: str, **fields: Any) -> None:
    with _store_lock(home, exclusive=True):
        records = _load_records(home)
        for record in records:
            if record["id"] == proposal_id:
                record.update(status=status, **fields)
        _replace_records(home, records)


def _records(home: Path) -> list[dict[str, Any]]:
    with _store_lock(home, exclusive=False):
        return _load_records(home)


def list_proposals(home: Path, *, status: str = "pending") -> list[dict[str, Any]]:
    """Operator view of the durable queue (never exposed as a model tool)."""
    if status not in {"pending", "all"}:
        raise ValueError("status must be pending or all")
    return [r for r in _records(home) if status == "all" or r["status"] == status]


def _digest_path(home: Path) -> Path:
    return proposal_path(home).with_name("memory-proposal-digest.json")


def _digest_state(home: Path) -> dict[str, Any]:
    path = _digest_path(home)
    if not path.exists():
        return {"last_sent": None, "sent": {}}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as file:
            state = json.load(file)
        if (not isinstance(state, dict) or not isinstance(state.get("sent"), dict)
                or (state.get("last_sent") is not None and
                    not isinstance(state.get("last_sent"), str))):
            raise ValueError("invalid digest state")
        for entry in state["sent"].values():
            if (not isinstance(entry, dict) or not isinstance(entry.get("first"), str)
                    or not isinstance(entry.get("reminded"), bool)):
                raise ValueError("invalid digest entry")
            datetime.fromisoformat(entry["first"])
        if state["last_sent"] is not None:
            datetime.fromisoformat(state["last_sent"])
        return state
    except (OSError, ValueError, TypeError) as exc:
        raise ProposalRefusal("invalid memory proposal digest state") from exc


def _save_digest_state(home: Path, state: dict[str, Any]) -> None:
    path = _digest_path(home)
    fd, temporary = tempfile.mkstemp(prefix=".memory-proposal-digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_digest_field(value: Any) -> str:
    """Redact and render untrusted bytes inert in Markdown and chat bridges."""
    text = redact_text(str(value) if value is not None else "(none)")
    text = " ".join(text.splitlines())
    for char in "\\`*_~|[]()<>":
        text = text.replace(char, "\\" + char)
    return text.replace("@", "@\u200b").replace(":", ":\u200b").replace(".", ".\u200b")


async def post_review_digest(
    home: Path, channel: str, send: Any, *, now: datetime | None = None,
) -> bool:
    """Server-only scheduled digest. Record a delivery only after the bridge accepts it."""
    if not channel.strip():
        return False
    now = now or datetime.now(timezone.utc)
    with _store_lock(home, exclusive=False):
        records = _load_records(home)
        state = _digest_state(home)
    last = state["last_sent"]
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=30):
        return False
    eligible = []
    for record in records:
        if record["status"] != "pending" or datetime.fromisoformat(record["expires_at"]) <= now:
            continue
        prior = state["sent"].get(record["id"])
        if prior is None or (not prior["reminded"] and
                            now - datetime.fromisoformat(prior["first"]) >= timedelta(hours=24)):
            eligible.append(record)
    if not eligible:
        return False
    remaining = len(eligible) - 10
    eligible = eligible[:10]
    lines = ["Pending memory proposals — derived from untrusted external content — approve only if true and worth keeping"]
    for record in eligible:
        proposal_id = record["id"]
        lines.extend((
            f"\n{_safe_digest_field(proposal_id)} ({_safe_digest_field(record['stream'])})",
            f"Content: {_safe_digest_field(record['content'])}",
            f"Origin: {_safe_digest_field(record['proposed_by'])} / {_safe_digest_field(record.get('origin_ref'))}",
            f"Rationale: {_safe_digest_field(record['rationale'])}",
            f"approve {proposal_id} / decline {proposal_id} / approve {proposal_id}: <edited text>",
        ))
    if remaining > 0:
        lines.append(f"\n{remaining} more pending (`mimir memory proposals list`)")
    result = await send(channel, "\n".join(lines), final=True)
    if not getattr(result, "sent", False):
        return False
    if len(eligible) == 1:
        approval_requests.set_prompt_message_id(
            eligible[0]["id"],
            getattr(result, "first_message_id", None) or getattr(result, "message_id", None),
        )
    with _store_lock(home, exclusive=True):
        state = _digest_state(home)
        state["last_sent"] = now.isoformat()
        for record in eligible:
            entry = state["sent"].get(record["id"])
            if entry is None:
                state["sent"][record["id"]] = {"first": now.isoformat(), "reminded": False}
            else:
                entry["reminded"] = True
        _save_digest_state(home, state)
    return True


def sync_pending(home: Path) -> None:
    """Reconcile the durable queue with the registry; also sweeps expired records."""
    backend = _APPROVAL_BACKENDS.get(home)
    if backend is None:
        return
    channel, _saga = backend
    now = datetime.now(timezone.utc)
    registered = {entry.approval_id for entry in approval_requests.pending(channel)}
    try:
        records = _records(home)
    except ProposalRefusal as exc:
        logger = logging.getLogger(__name__)
        logger.warning("Memory proposal registration skipped: %s", exc)
        if isinstance(exc, ProposalStoreCorrupt):
            try:
                from .event_logger import log_event_sync

                log_event_sync(
                    "memory_proposal_store_corrupt", line_number=exc.line_number,
                    channel_id=channel,
                )
            except Exception:  # Telemetry failure must not turn corruption into a boot crash.
                logger.debug("Memory proposal corruption event emit failed", exc_info=True)
        return
    for record in records:
        if record.get("status") != "pending":
            continue
        deadline = datetime.fromisoformat(record["expires_at"])
        remaining = (deadline - now).total_seconds()
        if remaining <= 0:
            _update(home, record["id"], "expired")
            approval_requests.cancel(record["id"], expired=True)
            continue
        if record["id"] in registered:
            continue

        def resolve(decision, edit, event, identity, resolved_at, approval_event, reply_source,
                    *, proposal_id=record["id"]):
            # The registry alone chooses the request. The async SAGA write is
            # completed by the ingress caller, never by a tool or model turn.
            event.extra["_memory_proposal_action"] = (home, proposal_id, decision, edit)
            return "granted" if decision == "approve" else "declined"

        try:
            approval_requests.register(
                kind="mp", approval_id=record["id"], channel_id=channel,
                description=_safe_digest_field(record.get("content", "memory proposal"))[:160],
                expires_at=time.monotonic() + remaining, resolver=resolve,
                supports_edits=True, inject_into_turn=False,
            )
        except ValueError:
            # A recently resolved ID is still reserved even if its durable
            # record was not approved (e.g. an integrity refusal).
            continue


def is_mp_reply(event: Any) -> bool:
    return _NAMED_MP.fullmatch((event.content or "").strip()) is not None


async def complete_reply(home: Path, event: Any, resolution: Any, resolver: Any) -> str | None:
    """Finish a reply, restoring retryability unless the durable decision completed."""
    try:
        return await _complete_reply(home, event, resolution, resolver)
    except ProposalRefusal as exc:
        return f"Could not complete memory proposal: {exc}"
    finally:
        entry = resolution.entry
        if entry is not None and entry.kind == "mp":
            try:
                record = next((r for r in _records(home) if r["id"] == entry.approval_id), None)
            except ProposalRefusal:
                record = None
            if record is None or record["status"] == "pending":
                approval_requests.restore_uncompleted(entry)


async def _complete_reply(home: Path, event: Any, resolution: Any, resolver: Any) -> str | None:
    """Finish a registry-selected reply and return a server-owned notice."""
    action = event.extra.pop("_memory_proposal_action", None)
    from .operator_approval import _is_authenticated_operator
    if not _is_authenticated_operator(event, resolver):
        return None
    if resolution.entry is None or resolution.entry.kind != "mp":
        if not is_mp_reply(event):
            return resolution.message
        match = _NAMED_MP.fullmatch((event.content or "").strip())
        proposal_id = match.group(1).lower()
        record = next((r for r in _records(home) if r.get("id") == proposal_id), None)
        channel = _APPROVAL_BACKENDS.get(home, (None, None))[0]
        if record and channel == event.channel_id:
            if (record.get("status") == "pending" and
                    datetime.fromisoformat(record["expires_at"]) <= datetime.now(timezone.utc)):
                _update(home, proposal_id, "expired")
                approval_requests.cancel(proposal_id, expired=True)
                return f"expired proposal {proposal_id}"
            if record.get("status") == "expired":
                return f"expired proposal {proposal_id}"
            if record.get("status") in {"approved", "declined"}:
                return f"already decided {proposal_id}"
        return resolution.message or f"no pending request {proposal_id}"
    if action is None:
        return resolution.message
    action_home, proposal_id, decision, edit = action
    if action_home != home or action_home not in _APPROVAL_BACKENDS:
        return f"no pending request {proposal_id}"
    return await decide_proposal(
        home, proposal_id, decision, edit=edit,
        approved_by=resolver.resolve(event.author), approval_event_id=event.source_id,
        saga_store=_APPROVAL_BACKENDS[home][1],
    )


async def decide_proposal(
    home: Path, proposal_id: str, decision: str, *, edit: str | None = None,
    approved_by: str, approval_event_id: str | None, saga_store: Any,
) -> str:
    """Server-side decision path after authenticated bridge reply resolution.

    Never expose this as a CLI decision or model tool: neither attests an operator.
    """
    if decision not in {"approve", "decline"} or not _NAMED_MP.fullmatch(f"{decision} {proposal_id}"):
        return f"no pending request {proposal_id}"
    record = next((r for r in _records(home) if r.get("id") == proposal_id), None)
    if record is None or record.get("status") != "pending":
        return f"already decided {proposal_id}"
    if datetime.fromisoformat(record["expires_at"]) <= datetime.now(timezone.utc):
        _update(home, proposal_id, "expired")
        return f"expired proposal {proposal_id}"
    if hashlib.sha256(record["content"].encode("utf-8")).hexdigest() != record["content_sha256"]:
        return f"hash mismatch for {proposal_id}"
    if decision == "decline":
        _update(home, proposal_id, "declined")
        return f"Declined {proposal_id}"
    content = record["content"] if edit is None else edit.strip()
    if not content:
        return f"empty edit for {proposal_id}"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        result = await saga_store.store(
            content, stream=record["stream"], source_type="operator_approved_proposal",
            provenance={
                "approved_by": approved_by, "approval_event_id": approval_event_id,
                "proposal_id": proposal_id, "proposed_by": record["proposed_by"],
                "proposal_turn_id": record["turn_id"], "origin_ref": record["origin_ref"],
                "content_sha256": digest, "edited": edit is not None,
            },
        )
    except Exception as exc:
        return f"Could not store {proposal_id}: {exc}"
    if not result.get("stored"):
        atom_id = result.get("atom_id")
        if result.get("reason") == "duplicate" and isinstance(atom_id, str) and atom_id.strip():
            _update(home, proposal_id, "approved", atom_id=atom_id, deduplicated=True)
            return f"Already in memory as {atom_id}"
        return f"Could not store {proposal_id}: {result.get('reason', 'duplicate')}"
    _update(home, proposal_id, "approved", atom_id=result["atom_id"])
    return f"Stored {proposal_id} as {result['atom_id']}"
