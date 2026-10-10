"""Test-only canary for model-visible output and persisted turn/event records."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

OUTSIDER_MARKER = f"WITHHELD-MARKER-{uuid4()}"


def assert_marker_absent(*blobs: object) -> None:
    """Reject the canary anywhere in strings, structured records or JSONL files."""
    for blob in blobs:
        if isinstance(blob, Path):
            text = blob.read_text(encoding="utf-8")
        elif isinstance(blob, str):
            text = blob
        else:
            text = json.dumps(blob, default=str, ensure_ascii=False)
        assert OUTSIDER_MARKER not in text, "outsider content reached a model-visible record"
