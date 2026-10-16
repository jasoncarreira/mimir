"""Inert closure proposals; only authenticated operator ingress can apply them.

The model cannot read/write this server-owned store or invoke the decision path.
A batch binds to exactly the proposals in its delivered digest, never future ones.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .approval_requests import mint_id
from .commitments.store import commitment_fingerprint, run_store_io
from .memory_proposals import _safe_digest_field, ProposalRefusal
from .redaction import redact_text

_REPLY = re.compile(r"^(approve|decline)\s+((?:cp|cb)-[a-z2-7]{4}(?:\s+cp-[a-z2-7]{4})*)$", re.I)
_BACKENDS: dict[Path, tuple[str, Any]] = {}
TTL = 7 * 86400


def proposal_path(home: Path) -> Path:
    return home / ".mimir" / "commitment-proposals.sqlite3"


@contextmanager
def _db(home: Path):
    path = proposal_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ProposalRefusal("proposal store must not be a symlink")
    db = sqlite3.connect(path, timeout=1)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL, batch TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS reviews (turn_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, turn_id TEXT UNIQUE NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE proposals SET status='expired' WHERE status='pending' AND expires<=?", (time.time(),))
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def configure_approvals(home: Path, channel: str, store: Any) -> None:
    if channel and store is not None:
        _BACKENDS[home] = (channel, store)


def queue_closure(home: Path, *, record: Any, action: str, evidence: str,
                  proposed_by: str, turn_id: str) -> str:
    if action not in {"complete", "dismiss"}:
        raise ProposalRefusal("action must be complete or dismiss")
    if record.is_terminal() or record.sensitivity == "care":
        raise ProposalRefusal("terminal or CARE-tier commitments cannot be proposed; escalate CARE")
    if not evidence.strip() or not proposed_by or not turn_id:
        raise ProposalRefusal("evidence and server provenance are required")
    with _db(home) as db:
        if db.execute("SELECT 1 FROM reviews WHERE turn_id=?", (turn_id,)).fetchone():
            raise ProposalRefusal("review is already sealed")
        rows = db.execute("SELECT * FROM proposals").fetchall()
        pending = [json.loads(r["payload"]) for r in rows if r["status"] == "pending"]
        if any(r["commitment_id"] == record.id for r in pending):
            raise ProposalRefusal("commitment already has a pending closure")
        if sum(r["proposed_by"] == proposed_by for r in pending) >= 100:
            raise ProposalRefusal("pending closure limit reached")
        pid = mint_id("cp", excluded=frozenset(r["id"] for r in rows))
        payload = dict(commitment_id=record.id, action=action,
                       evidence=redact_text(evidence)[:1000], text=redact_text(record.text)[:200],
                       current_status=record.status, owner=record.owner_principal,
                       fingerprint=commitment_fingerprint(record), proposed_by=proposed_by,
                       turn_id=turn_id, created_at=time.time())
        db.execute("INSERT INTO proposals VALUES (?, ?, 'pending', ?, NULL)",
                   (pid, json.dumps(payload), time.time() + TTL))
    return pid


def is_closure_reply(event: Any) -> bool:
    return _REPLY.fullmatch((event.content or "").strip()) is not None


async def handle_reply(home: Path, event: Any, resolver: Any) -> str | None:
    """Server ingress only. Never installed as a tool or a CLI decision command."""
    match = _REPLY.fullmatch((event.content or "").strip())
    if match is None:
        return None
    from .operator_approval import _is_authenticated_operator
    backend = _BACKENDS.get(home)
    if (backend is None or event.channel_id != backend[0]
            or not _is_authenticated_operator(event, resolver)):
        return "Commitment closure approval refused: authenticated operator channel required."
    decision, names = match.group(1).lower(), match.group(2).lower().split()
    return await run_store_io(_decide, home, backend[1], names, decision,
                              resolver.resolve(event.author), event.source_id)


def _decide(home, store, names, decision, actor, message_id):
    notices = []
    with _db(home) as db:
        bulk = names[0].startswith("cb-")
        if bulk:
            if len(names) != 1:
                return "Skipped: batch approval cannot include individual IDs."
            rows = db.execute("SELECT * FROM proposals WHERE batch=?", (names[0],)).fetchall()
        else:
            rows = []
            for name in dict.fromkeys(names):
                row = db.execute("SELECT * FROM proposals WHERE id=?", (name,)).fetchone()
                if row is None:
                    notices.append(f"Skipped {name}: no pending proposal")
                else:
                    rows.append(row)
        for row in rows:
            pid = row["id"]
            data = json.loads(row["payload"])
            if row["status"] != "pending":
                notices.append(f"Skipped {pid}: {row['status']}")
                continue
            if bulk and data["action"] == "dismiss":
                notices.append(f"Skipped {pid}: dismissal needs explicit individual approval")
                continue
            if decision == "decline":
                db.execute("UPDATE proposals SET status='declined' WHERE id=?", (pid,))
                notices.append(f"Declined {pid}")
                continue
            applied = store.apply_proposed_closure_sync(
                data["commitment_id"], action=data["action"],
                expected_fingerprint=data["fingerprint"], expected_owner=data["owner"],
                actor_principal=actor, approval_message_id=message_id, proposal_id=pid,
            )
            db.execute("UPDATE proposals SET status=? WHERE id=?",
                       ("approved" if applied else "skipped", pid))
            notices.append(f"{'Applied' if applied else 'Skipped'} {pid}: {data['commitment_id']} "
                           + (data["action"] if applied else "already terminal, changed, CARE, or unauthorized"))
    return "\n".join(notices) or "Skipped: no pending commitment closure proposals."


def seal_review(home: Path, turn_id: str) -> None:
    """Server lifecycle hook: freeze one completed review, never model-callable."""
    with _db(home) as db:
        db.execute("INSERT OR IGNORE INTO reviews VALUES (?)", (turn_id,))
        if db.execute("SELECT 1 FROM batches WHERE turn_id=?", (turn_id,)).fetchone():
            return
        rows = db.execute("SELECT id, payload FROM proposals WHERE status='pending' AND batch IS NULL").fetchall()
        members = [r["id"] for r in rows if json.loads(r["payload"])["turn_id"] == turn_id]
        if members:
            batch = mint_id("cb", excluded=frozenset(r[0] for r in db.execute("SELECT id FROM batches")))
            db.execute("INSERT INTO batches (id, turn_id) VALUES (?, ?)", (batch, turn_id))
            db.executemany("UPDATE proposals SET batch=? WHERE id=?", [(batch, pid) for pid in members])


def _prepare_digest(home: Path):
    with _db(home) as db:
        pending = db.execute("SELECT id FROM batches WHERE delivered=0 AND retry_at<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        if pending is None:
            return None
        batch = pending["id"]
        rows = db.execute("SELECT * FROM proposals WHERE status='pending' AND batch=?", (batch,)).fetchall()
        if not rows:
            db.execute("UPDATE batches SET delivered=1 WHERE id=?", (batch,))
            return None
        # Stable batch membership survives cancellation/process death. A bounded
        # delivery lease prevents concurrent senders; uncertain sends retry the
        # same ID, not a newly minted approval scope.
        db.execute("UPDATE batches SET retry_at=? WHERE id=?", (time.time() + 300, batch))
        lines = [f"Commitment closure proposals — untrusted evidence, nothing closed. Batch {batch} (expires within 7 days)."]
        for row in rows:
            data = json.loads(row["payload"])
            lines.append(f"{row['id']} {data['action']} {data['commitment_id']}: "
                         f"{_safe_digest_field(data['text'])}\nEvidence: {_safe_digest_field(data['evidence'])}")
        lines.append(f"Reply `approve {batch}` for completions only. Dismissals need their specific cp-ID. "
                     "Or reply `approve cp-xxxx cp-yyyy` / `decline cp-xxxx` for individual decisions.")
    return batch, "\n".join(lines)


async def post_review_digest(home: Path, channel: str, send: Any) -> bool:
    if home not in _BACKENDS or _BACKENDS[home][0] != channel:
        return False
    prepared = await run_store_io(_prepare_digest, home)
    if prepared is None:
        return False
    batch, text = prepared
    try:
        result = await send(channel, text, final=True)
        if not result.sent:
            raise ProposalRefusal("digest delivery refused")
    except BaseException:
        # Cancellation/process death leaves a leased batch recoverable after
        # retry_at; ordinary delivery refusals can retry immediately.
        await run_store_io(_finish_delivery, home, batch, False)
        raise
    await run_store_io(_finish_delivery, home, batch, True)
    return True


def _finish_delivery(home, batch, delivered):
    with _db(home) as db:
        db.execute("UPDATE batches SET delivered=?, retry_at=0 WHERE id=?", (int(delivered), batch))
