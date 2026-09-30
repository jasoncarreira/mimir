"""Server-owned, untrusted memory proposals awaiting human attestation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ProposalRefusal(ValueError):
    """A proposal cannot be queued without changing the store."""


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
    targets = [(store, False), (store.parent, False), (outbox, True)]
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


def queue_proposal(
    home: Path, *, content: str, stream: str, rationale: str,
    proposed_by: str, turn_id: str, origin_trigger: str, origin_ref: str | None,
    sources: tuple[Any, ...],
) -> str:
    """Check limits and append one JSONL record atomically across processes."""
    if not isinstance(content, str) or not content.strip():
        raise ProposalRefusal("content is required")
    if len(content) > 500:
        raise ProposalRefusal("content exceeds 500 characters")
    if stream not in {"semantic", "episodic", "procedural"}:
        raise ProposalRefusal("stream must be semantic, episodic, or procedural")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ProposalRefusal("rationale is required")
    if not proposed_by or not turn_id or not origin_trigger or not sources:
        raise ProposalRefusal("missing server-owned proposal provenance")

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = proposal_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ProposalRefusal("proposal directory must not be a symlink")
    with os.fdopen(
        os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600),
        "r+", encoding="utf-8",
    ) as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        pending = []
        for line_number, line in enumerate(file, 1):
            try:
                record = json.loads(line)
            except ValueError:
                raise ProposalRefusal(f"malformed proposal store record at line {line_number}") from None
            required = ("status", "content_sha256", "proposed_by", "id")
            if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key].strip()
                for key in required
            ):
                raise ProposalRefusal(f"malformed proposal store record at line {line_number}")
            if record["status"] == "pending":
                pending.append(record)
        if any(record["content_sha256"] == digest for record in pending):
            raise ProposalRefusal("duplicate pending proposal")
        if sum(record["proposed_by"] == proposed_by for record in pending) >= 20:
            raise ProposalRefusal("principal has 20 pending proposals (limit reached)")
        existing_ids = {record["id"] for record in pending}
        proposal_id = "mp-" + secrets.token_hex(4)
        while proposal_id in existing_ids:
            proposal_id = "mp-" + secrets.token_hex(4)
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
        file.seek(0, os.SEEK_END)
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())
        return proposal_id
