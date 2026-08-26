"""Best-effort local completion signals for cron-backed pollers."""

from __future__ import annotations

import json
import os
from pathlib import Path


TRIGGER_FIFO_NAME = "poller-triggers.fifo"


def trigger_fifo_path(home: Path) -> Path:
    return home / "state" / TRIGGER_FIFO_NAME


def notify_poller(home: Path, poller: str, *, reason: str) -> bool:
    """Notify the live scheduler without waiting; the unchanged cron is fallback."""
    payload = json.dumps(
        {"poller": poller, "reason": reason}, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    try:
        fd = os.open(trigger_fifo_path(home), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, payload)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True
