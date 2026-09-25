"""Recovery and delivery-receipt hooks for the Worklink ready queue."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Collection

from ..models import AgentEvent
from .dispatch_failures import (
    STATE_FILE,
    _validate_merge_reconciliations,
    dispatch_failure_state_dir,
)

log = logging.getLogger(__name__)


def report_live_delivery_receipts(
    persist_dir: Path,
    home: Path | None,
    prune_stale: Callable[[Collection[str]], None],
) -> None:
    """Report receipts protected by Worklink state under the ledger lock."""
    if home is None:
        return
    try:
        if persist_dir.resolve() != dispatch_failure_state_dir(home).resolve():
            return
        with ExitStack() as stack:
            home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, home_fd)
            parent_fd = home_fd
            for component in ("state", "pollers", "worklink-ready-queue"):
                root_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                stack.callback(os.close, root_fd)
                if component == "state":
                    state_root_fd = root_fd
                parent_fd = root_fd
            lock_fd = os.open(
                f"{STATE_FILE}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600, dir_fd=root_fd,
            )
            stack.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            state_fd = os.open(
                STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd,
            )
            with os.fdopen(state_fd, encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return
                state = json.load(handle)
                if (not isinstance(state, dict) or state.get("version") != 1
                        or not isinstance(state.get("issues"), dict)):
                    return
                live = set()
                for entry in state["issues"].values():
                    if not isinstance(entry, dict):
                        return
                    issue = entry.get("issue_id")
                    signature = entry.get("signature")
                    notified = entry.get("notified_signatures")
                    occurrence = entry.get("occurrence_id")
                    if (type(issue) is not int or not isinstance(signature, str)
                            or not signature or type(entry.get("active")) is not bool
                            or not isinstance(notified, list)
                            or not all(isinstance(value, str) for value in notified)
                            or not (occurrence is None or isinstance(occurrence, str))):
                        return
                    if entry["active"] and signature not in notified:
                        key = f"worklink-run-failure:{issue}:{signature}:{occurrence}"
                        live.add(hashlib.sha256(key.encode()).hexdigest())
                transitions = state.get("factory_transitions", {})
                if not isinstance(transitions, dict):
                    return
                for key, entry in transitions.items():
                    if not isinstance(entry, dict):
                        return
                    kind = entry.get("kind")
                    issue = entry.get("issue_id")
                    run = entry.get("run_id")
                    attempt = entry.get("attempt")
                    if (kind not in ("factory_start", "factory_success")
                            or type(issue) is not int
                            or not isinstance(run, str) or not run
                            or type(attempt) is not int
                            or type(entry.get("notified")) is not bool
                            or not (entry.get("pr_url") is None or isinstance(entry["pr_url"], str))
                            or key != f"worklink-{kind}:{issue}:{run}:{attempt}"
                            or entry.get("delivery_key") != key):
                        return
                    if not entry["notified"]:
                        live.add(hashlib.sha256(key.encode()).hexdigest())
                reconciliations = _validate_merge_reconciliations(
                    state.get("merge_reconciliations")
                )
                for key, entry in reconciliations["notices"].items():
                    if not entry["notified"]:
                        live.add(hashlib.sha256(key.encode()).hexdigest())
                os.fsync(handle.fileno())
            os.fsync(root_fd)
            try:
                worklink_fd = os.open(
                    "worklink", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=state_root_fd,
                )
                stack.callback(os.close, worklink_fd)
                continuation_fd = os.open(
                    "continuations", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=worklink_fd,
                )
            except FileNotFoundError:
                continuation_fd = None
            if continuation_fd is not None:
                stack.callback(os.close, continuation_fd)
                with os.scandir(continuation_fd) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        fd = os.open(
                            entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=continuation_fd,
                        )
                        with os.fdopen(fd, encoding="utf-8") as handle:
                            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                                return
                            sidecar = json.load(handle)
                        if (not isinstance(sidecar, dict)
                                or sidecar.get("kind") != "worklink_tool_budget_continuation"):
                            return
                        key = sidecar.get("idempotency_key") or Path(entry.name).stem
                        if not isinstance(key, str):
                            return
                        live.add(hashlib.sha256(f"worklink-continuation:{key}".encode()).hexdigest())
            prune_stale(live)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("Worklink receipt pruning skipped or incomplete: %s", exc)


def recovery_relevance_check(persist_dir: Path):
    """Validate the single incident identity immediately before execution."""
    def current(event: AgentEvent) -> bool | None:
        items = event.extra.get("items") if isinstance(event.extra, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return None
        item = items[0]
        issue_id = item.get("issue_id")
        signature = item.get("error_signature")
        occurrence = item.get("failure_occurrence_id")
        if (
            not isinstance(issue_id, int)
            or isinstance(issue_id, bool)
            or not isinstance(signature, str)
            or not signature
            or not isinstance(occurrence, str)
            or not occurrence
        ):
            return None
        try:
            payload = json.loads((persist_dir / STATE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("issues"), dict):
            return None
        entry = payload["issues"].get(str(issue_id))
        if entry is None:
            return False
        if not isinstance(entry, dict) or not isinstance(entry.get("active"), bool):
            return None
        return bool(
            entry["active"]
            and entry.get("signature") == signature
            and entry.get("occurrence_id") == occurrence
        )

    async def check(event: AgentEvent) -> bool | None:
        return await asyncio.to_thread(current, event)

    return check
