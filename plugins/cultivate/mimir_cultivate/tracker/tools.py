"""Host-side tracker tools, bound to the one issue the poller attested for this turn.

The model supplies only the issue reference and content. Credentials come from the
server process environment, and every result has a fixed shape: tracker prose,
response bodies and Chainlink ids never appear in a result.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from mimir.models import AuthContext
from mimir.poller_tracker import NOTICE_REASONS
from mimir.tools.refusals import ToolPolicyRefusal

from ..forge import _bind_injected_runtime
from ..worklink.planning import missing_epic_template_parts, missing_leaf_template_parts
from . import transport
from .adapters import build_adapter
from .adapters.base import ActionResult
from .config import MAX_STATE_LENGTH, TrackerConfig, configured_states
from .evidence import EvidenceError, evidence_transaction, tracker_dir
from .lifecycle import request_transition
from .record import ExternalRecord, render_external_record, strip_external_record
from .transport import TrackerUnavailable
from .work import ChainlinkWork, WorkUnavailable

log = logging.getLogger(__name__)

TRACKER_ENV_KEYS = (
    "GITHUB_TOKEN",
    "MIMIR_GITHUB_SELF_LOGIN",
    "MIMIR_GITHUB_TRUSTED_BOT_LOGINS",
    "LINEAR_API_KEY",
    "JIRA_SITE",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
)
_TITLE_MAX = 120
_DESCRIPTION_MAX = 20_000
_BODY_MAX = 8_000
_REASON = re.compile(r"[a-z0-9_]{1,40}")
_ISSUE_PATH_REFERENCE = re.compile(r"\bissue/\d+-a\d+\b")
_CHAINLINK_WORD = re.compile(r"(?i)\bchainlink\b")


def _result(status: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **extra}


def _file_result(
    status: str, kind: str, reason: str = "", missing: list[str] | None = None,
) -> dict[str, Any]:
    return _result(status, reason, kind=kind, missing=list(missing or ()))


def _fixed_reason(reason: object, default: str) -> str:
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else default


def _bound_record(runtime: ToolRuntime[AuthContext] | None, external_ref: object) -> tuple[Any, ExternalRecord]:
    context = getattr(runtime, "context", None)
    if isinstance(external_ref, str):
        for adapter, ref, url in getattr(context, "tracker_issue_targets", frozenset()):
            if external_ref == f"{adapter}:{ref}":
                return context, ExternalRecord(adapter, ref, url)
    raise ToolPolicyRefusal("Tracker action rejected: issue is outside this turn's poller scope")


def _turn_is_tainted(context: Any) -> bool:
    fallback = getattr(context, "ifc_labels", None)
    state = getattr(context, "ifc_state", None)
    labels = state.current(fallback=fallback) if state is not None else fallback
    return any(source.integrity != "trusted" for source in getattr(labels, "sources", ()))


def _home() -> Path | None:
    value = os.environ.get("MIMIR_HOME")
    return Path(value) if value else None


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in TRACKER_ENV_KEYS if os.environ.get(key)}


def _tracker_config(home: Path) -> TrackerConfig | None:
    from ..worklink.backends.registry import WorklinkConfig

    try:
        return WorklinkConfig.load(home / "worklink.yaml").tracker
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _adapter(config: TrackerConfig, record: ExternalRecord):
    adapter_config = config.adapters.get(record.adapter)
    if adapter_config is None:
        return None
    try:
        return build_adapter(record.adapter, adapter_config, _env(), transport.default_transport())
    except ValueError:
        return None


@contextmanager
def _filing_lock(home: Path, external_ref: str) -> Iterator[None]:
    directory = tracker_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(external_ref.encode("utf-8")).hexdigest()[:16]
    with (directory / f"filing-{digest}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_filing(kind: object, title: object, description: object) -> tuple[str, list[str]] | None:
    if kind not in ("leaf", "epic"):
        return "invalid_kind", []
    if not isinstance(title, str) or not 1 <= len(title) <= _TITLE_MAX or not title.isprintable():
        return "invalid_title", []
    if not isinstance(description, str) or not description.strip():
        return "invalid_description", []
    if len(description) > _DESCRIPTION_MAX:
        return "description_too_long", []
    if strip_external_record(description) != description:
        return "reserved_record_line", []
    missing = (
        missing_leaf_template_parts(description) if kind == "leaf"
        else missing_epic_template_parts(description)
    )
    if missing:
        return "template_incomplete", missing
    return None


@tool
def tracker_file_work(
    external_ref: str,
    kind: Literal["leaf", "epic"],
    title: str,
    description: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """File a Worklink leaf or epic for the tracker issue named by this turn."""
    context, record = _bound_record(runtime, external_ref)
    if _turn_is_tainted(context):
        return _file_result("refused", str(kind), "tainted_turn")
    invalid = _validate_filing(kind, title, description)
    if invalid is not None:
        return _file_result("refused", str(kind), *invalid)
    home = _home()
    if home is None:
        return _file_result("error", kind, "home_unavailable")
    work = ChainlinkWork(cwd=home)
    try:
        with _filing_lock(home, record.external_ref()):
            try:
                if work.find(record.external_ref()):
                    return _file_result("exists", kind)
            except WorkUnavailable:
                return _file_result("error", kind, "search_unavailable")
            try:
                work.create(title=title, description=description, record=record, epic=kind == "epic")
            except WorkUnavailable:
                log.warning("tracker_file_work create did not report success")
            try:
                confirmed = work.find(record.external_ref())
            except WorkUnavailable:
                confirmed = []
    except OSError:
        return _file_result("error", kind, "filing_lock_unavailable")
    if len(confirmed) != 1:
        return _file_result("error", kind, "create_outcome_unknown")
    try:
        request_transition(
            home, f"{description}\n\n{render_external_record(record)}", "filed",
        )
    except (OSError, ValueError, EvidenceError):
        log.warning("tracker_file_work could not record the filed transition")
    return _file_result("created", kind)


@tool
def tracker_comment(
    external_ref: str,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Post a comment on the tracker issue named by this turn."""
    context, record = _bound_record(runtime, external_ref)
    if _turn_is_tainted(context):
        return _result("refused", "tainted_turn")
    if not isinstance(body, str) or not 1 <= len(body) <= _BODY_MAX or not body.strip():
        return _result("refused", "invalid_body")
    from ..worklink.orchestrator import _INTERNAL_REFERENCE_RE

    if (
        _INTERNAL_REFERENCE_RE.search(body)
        or _ISSUE_PATH_REFERENCE.search(body)
        or _CHAINLINK_WORD.search(body)
    ):
        return _result("refused", "internal_reference")
    home = _home()
    if home is None:
        return _result("error", "home_unavailable")
    config = _tracker_config(home)
    adapter = _adapter(config, record) if config is not None else None
    if adapter is None:
        return _result("error", "adapter_unavailable")
    try:
        outcome = adapter.comment(record.ref, body)
    except TrackerUnavailable as exc:
        return _result("error", _fixed_reason(exc.code, "unavailable"))
    if outcome.status in ("posted", "refused"):
        return _result(outcome.status, _fixed_reason(outcome.reason, ""))
    return _result("error", _fixed_reason(outcome.reason, "unavailable"))


def _record_agent_notice(home: Path, record: ExternalRecord, reason: str) -> None:
    if reason not in NOTICE_REASONS:
        reason = "api_error"
    created_at = datetime.now(UTC).isoformat()
    ext_ref = record.external_ref()
    key = "trk-" + hashlib.sha256(
        f"notice:{ext_ref}:agent:{reason}:{created_at}".encode()
    ).hexdigest()[:24]
    with evidence_transaction(home) as data:
        if any(
            not notice.get("notified")
            and (notice.get("adapter"), notice.get("ref"), notice.get("lifecycle_event"),
                 notice.get("reason")) == (record.adapter, record.ref, "agent", reason)
            for notice in data["notices"]
        ):
            return
        data["notices"].append({
            "delivery_key": key,
            "adapter": record.adapter,
            "ref": record.ref,
            "url": record.url,
            "lifecycle_event": "agent",
            "reason": reason,
            "created_at": created_at,
            "notified": False,
        })


@tool
def tracker_set_state(
    external_ref: str,
    state: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Move the tracker issue named by this turn to one of its configured states."""
    context, record = _bound_record(runtime, external_ref)
    if _turn_is_tainted(context):
        return _result("refused", "tainted_turn")
    if record.adapter == "github_issues":
        return _result("unsupported", "github_issues")
    if (
        not isinstance(state, str)
        or not 1 <= len(state) <= MAX_STATE_LENGTH
        or not state.isprintable()
    ):
        return _result("refused", "invalid_state")
    home = _home()
    if home is None:
        return _result("error", "home_unavailable")
    config = _tracker_config(home)
    if config is None:
        return _result("error", "config_unavailable")
    if state not in configured_states(config, record):
        return _result("refused", "state_not_configured")
    adapter = _adapter(config, record)
    if adapter is None:
        return _result("error", "adapter_unavailable")
    try:
        outcome = adapter.transition(record.ref, state)
    except TrackerUnavailable as exc:
        outcome = ActionResult("unavailable", exc.code)
    reason = _fixed_reason(outcome.reason, "unavailable" if outcome.status == "unavailable" else "")
    if outcome.status == "unavailable":
        try:
            _record_agent_notice(home, record, reason)
        except (OSError, EvidenceError):
            log.warning("tracker_set_state could not record the unavailable notice")
    if outcome.status not in ("applied", "already", "unavailable", "unsupported", "refused"):
        return _result("error", reason)
    return _result(outcome.status, reason)


TRACKER_TOOLS = tuple(
    _bind_injected_runtime(tracker_tool)
    for tracker_tool in (tracker_file_work, tracker_comment, tracker_set_state)
)
