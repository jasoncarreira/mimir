"""Bounded scope audit records for the stdlib-only local ACP proxy.

These records go to local stderr, not the ACP stdout stream or daemon journal.
Only a fixed event and allowlisted status fields are accepted; execution inputs
and file contents must never be serialized here.
"""
from __future__ import annotations

import json
import sys
from typing import Any

MAX_AUDIT_PATH_BYTES = 4096
_OUTCOMES = frozenset({"approved", "already_approved", "denied", "cancelled"})


async def safe_log_event(event_type: str, **payload: Any) -> None:
    """Write one fixed-shape JSON event; a broken diagnostic sink is nonfatal."""
    if event_type != "acp_permission_outcome":
        return
    path = payload.get("path")
    if not isinstance(path, str):
        path = "<invalid>"
    path = path.encode("utf-8", errors="replace")[:MAX_AUDIT_PATH_BYTES].decode(
        "utf-8", errors="ignore"
    )
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        outcome = "denied"
    record = {
        "type": "acp_permission_outcome",
        "wrapper_name": "hands_request_scope",
        "path": path,
        "outcome": outcome,
        "resource_resolvable": payload.get("resource_resolvable") is True,
    }
    try:
        # JSON escaping keeps path newlines/control characters inside one record.
        # Never print to stdout: it carries ACP JSON-RPC frames.
        sys.stderr.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        sys.stderr.flush()
    except Exception:
        # Diagnostics must not turn a refusal into a failed tool operation.
        pass
