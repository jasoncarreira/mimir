#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _ensure_mimir_import_path() -> None:
    script = Path(__file__).resolve()
    candidates = [script.parents[4]]
    if value := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(value))
    executable = Path(sys.executable)
    if executable.parent.parent.name in {".venv", "venv"}:
        candidates.append(executable.parent.parent.parent)
    for candidate in candidates:
        if (candidate / "mimir" / "__init__.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_mimir_import_path()

from mimir.worklink.attention import AttentionRecord, render_attention_prompt
from mimir.worklink.dispatch_failures import (
    dispatch_failure_state_dir,
    pending_attention_records,
)


def main() -> int:
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if not home_value:
        return 0
    state_dir = dispatch_failure_state_dir(Path(home_value))
    try:
        pending = pending_attention_records(state_dir, limit=32)
    except Exception:
        return 1
    for payload in pending:
        try:
            record = AttentionRecord.from_json(
                payload, legacy=payload.get("source") == "legacy_v1"
            )
        except (KeyError, TypeError, ValueError):
            continue
        output = {
            "prompt": render_attention_prompt(record),
            "record_kind": record.kind.value,
            "issue_id": record.issue_id,
            "signature": record.error_signature,
            "occurrence_id": record.occurrence_id,
            "delivery_key": record.delivery_key,
        }
        sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
