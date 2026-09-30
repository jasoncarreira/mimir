"""Server-owned, untrusted memory proposals awaiting human attestation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ProposalRefusal(ValueError):
    """A proposal cannot be queued without changing the store."""


def proposal_path(home: Path) -> Path:
    return home / ".mimir" / "memory-proposals.jsonl"


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
        for line in file:
            record = json.loads(line)
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
