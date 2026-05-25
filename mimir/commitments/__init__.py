"""Commitments — durable records of future obligations the agent has
accepted (agent promises, user requests it agreed to follow up on,
open loops to revisit).

Phase 1 (this module today): data model + JSONL store + CLI commands.
Operator-managed; no LLM extraction or prompt surfacing yet.

Phase 2 (planned): session-boundary extraction hook + due-check poller
+ algedonic ``commitment_due`` / ``commitment_expired`` events.

Phase 3 (planned): prompt-builder block (Upcoming commitments).
"""

from .models import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    CommitmentStatus,
    make_commitment_id,
    make_dedupe_key,
)
from .store import COMMITMENTS_JSONL_SCHEMA_VERSION, CommitmentsStore

__all__ = [
    "COMMITMENTS_JSONL_SCHEMA_VERSION",
    "CommitmentKind",
    "CommitmentRecord",
    "CommitmentSensitivity",
    "CommitmentStatus",
    "CommitmentsStore",
    "make_commitment_id",
    "make_dedupe_key",
]
