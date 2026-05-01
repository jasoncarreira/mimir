"""BenchBridge — minimum viable outbound for the benchmark adapter.

The benchmark adapter:
- Drives inbound by POSTing to ``/event`` with ``channel_id`` like
  ``bench-task-3``. No bridge-side inbound handler needed.
- Consumes outbound via mimir's stdout stream. BenchBridge prints a
  newline-delimited tagged line per send so ``run.py`` can parse it.

Reactions: written to ``<home>/logs/reactions.jsonl`` (the spec's bench
behavior — see §7.1). No native reaction concept exists for stdout.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .base import Bridge, SendResult


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class BenchBridge(Bridge):
    """Stdout-routed outbound bridge for benchmark runs.

    Args:
        home: Agent home — used to scope reactions.jsonl.
        stream: Where to write outbound lines. Defaults to ``sys.stdout``;
            tests pass a ``StringIO``.
    """

    home: Path
    stream: object | None = None

    # Match plain "bench" (current single-channel adapter) and "bench-*"
    # (legacy per-session channel naming). startswith semantics — "bench"
    # matches both since "bench-seed-0".startswith("bench") is True.
    prefixes = ("bench",)
    name = "bench"

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
    ) -> SendResult:
        message_id = uuid.uuid4().hex[:12]
        line = (
            f"[mimir:bench send_message channel={channel_id} "
            f"msg_id={message_id}] {text}"
        )
        out = self.stream if self.stream is not None else sys.stdout
        try:
            out.write(line + "\n")
            out.flush()
        except (OSError, AttributeError):
            return SendResult(sent=False, error="stdout write failed")

        if attachment_paths:
            names = ",".join(str(p) for p in attachment_paths)
            try:
                out.write(
                    f"[mimir:bench send_message_attachments channel={channel_id} "
                    f"msg_id={message_id}] {names}\n"
                )
                out.flush()
            except (OSError, AttributeError):
                pass
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        path = self.home / "logs" / "reactions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _utc_now_iso(),
            "channel_id": channel_id,
            "message_id": message_id,
            "emoji": emoji,
        }
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError:
            return False
        return True
