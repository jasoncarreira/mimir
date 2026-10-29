#!/usr/bin/env python3
"""Poll configured trackers for issues assigned to the bot and hand them to the agent.

The poller never writes tracker text anywhere but its own stdout. The cursor holds
ids, enums and timestamps only. Whether a thread may reach the agent is decided by
``delivery_decision`` here and decided again, independently, by the in-server
attestor that re-reads the issue; the host compares the two prompts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _ensure_mimir_import_path() -> None:
    exe = Path(sys.executable)
    venv_root = exe.parent.parent
    script_path = globals().get("__file__")
    script = Path(script_path).resolve() if script_path else None
    candidates = [script.parents[7]] if script is not None and len(script.parents) > 7 else []
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(source_dir))
    if venv_root.name in {".venv", "venv"}:
        candidates.append(venv_root.parent)
    for candidate in candidates:
        if not (candidate / "mimir" / "__init__.py").is_file():
            continue
        path = str(candidate)
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
        provider = candidate / "plugins" / "cultivate"
        if (provider / "mimir_cultivate" / "__init__.py").is_file():
            provider_path = str(provider)
            while provider_path in sys.path:
                sys.path.remove(provider_path)
            sys.path.insert(0, provider_path)
        venv = candidate / ".venv"
        if venv.is_dir():
            for site in sorted((venv / "lib").glob("python*/site-packages")):
                site_path = str(site)
                if site_path not in sys.path:
                    sys.path.append(site_path)
        return


_ensure_mimir_import_path()
warnings.filterwarnings("ignore")

import yaml  # noqa: E402

from mimir.poller_tracker import (  # noqa: E402
    TRACKER_KINDS,
    TRACKER_STDERR_CODE_RE,
    safe_adapter,
    safe_delivery_key,
    safe_event_id,
    safe_ref,
    safe_url,
)
from mimir_cultivate.tracker import transport as tracker_transport  # noqa: E402
from mimir_cultivate.tracker.adapters import build_adapter  # noqa: E402
from mimir_cultivate.tracker.config import TrackerConfig  # noqa: E402
from mimir_cultivate.tracker.drain import NOTICE_KIND, drain  # noqa: E402
from mimir_cultivate.tracker.evidence import EvidenceError  # noqa: E402
from mimir_cultivate.tracker.lifecycle import request_transition  # noqa: E402
from mimir_cultivate.tracker.prompts import render_prompt  # noqa: E402
from mimir_cultivate.tracker.thread import (  # noqa: E402
    Decision,
    TrackerIssue,
    delivery_decision,
    parse_timestamp,
)
from mimir_cultivate.tracker.transport import TrackerUnavailable  # noqa: E402
from mimir_cultivate.tracker.work import ChainlinkWork, WorkUnavailable  # noqa: E402
from mimir_cultivate.worklink.backends.registry import WorklinkConfig  # noqa: E402
from mimir_cultivate.worklink.dispatch_failures import delivery_receipt_exists  # noqa: E402

POLLER_NAME = "tracker-poller"
STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent.parent))
CURSOR_FILE = STATE_DIR / "cursor.json"

LIST_CAP = 50
READ_CAP = 10
FIND_CAP = 20
ITEM_CAP = 5
WITHHELD_CAP = 5
DEADLINE_MARGIN_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 120.0
RETRY_AFTER = timedelta(hours=24)
TITLE_CHARS = 300
GH_TIMEOUT_SECONDS = 30

TRIAGE_STATES = ("new", "pending", "responded", "filed")
ADOPTION_ORDER = {"pending": 0, "responded": 1, "new": 2}
ADAPTER_ENV_KEYS = frozenset({
    "GITHUB_TOKEN",
    "MIMIR_GITHUB_SELF_LOGIN",
    "MIMIR_GITHUB_TRUSTED_BOT_LOGINS",
    "LINEAR_API_KEY",
    "JIRA_SITE",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
})

_ENTRY_KEY_RE = re.compile(r"[a-z][a-z_]{0,31}:[A-Za-z0-9_-]{1,64}")
_STABLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FILED = "filed"

_STATE_GITIGNORE = """\
# Transient tracker-poller state — seeded by the tracker-poller skill
# (write-if-missing; edit freely). The cursor holds ids and timestamps only and
# is rebuilt from the trackers, so it stays out of the home's tracked history.
cursor.json
*.tmp
.delivery-receipts/
"""


class CursorError(RuntimeError):
    """The cursor exists but cannot be trusted; it is never overwritten."""


def _diag(code: str) -> None:
    """Write one stderr line; only fixed code shapes may leave this process."""

    if not TRACKER_STDERR_CODE_RE.fullmatch(code):
        code = "diag_invalid"
    print(code, file=sys.stderr, flush=True)


def _emit(record: Mapping[str, Any]) -> None:
    print(json.dumps(record), flush=True)


def _digest(text: str) -> str:
    return "trk-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _item_key(kind: str, entry_key: str, event_id: str, *, retry: bool = False) -> str:
    return _digest(f"{kind}:{entry_key}:{event_id}" + (":retry" if retry else ""))


def _withheld_key(entry_key: str, latest_event_id: str, reason: str) -> str:
    return _digest(f"withheld:{entry_key}:{latest_event_id}:{reason}")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# --- cursor ---------------------------------------------------------------


def _empty_cursor() -> dict[str, Any]:
    return {"version": 1, "issues": {}}


def _new_entry(ref: str, now: datetime) -> dict[str, Any]:
    return {
        "ref": ref,
        "updated_at": None,
        "last_delivered_event_id": None,
        "pending": None,
        "triage": {"state": "new", "since": _stamp(now), "reemitted": False},
    }


def _timestamp_text(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return parse_timestamp(value).isoformat()
    except ValueError:
        return None


def _checked_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CursorError("entry")
    pending = raw.get("pending")
    triage = raw.get("triage")
    updated_at = raw.get("updated_at")
    delivered = raw.get("last_delivered_event_id")
    if (
        safe_ref(raw.get("ref")) is None
        or not (updated_at is None or _timestamp_text(updated_at) is not None)
        or not (delivered is None or safe_event_id(delivered) is not None)
        or not isinstance(triage, dict)
        or triage.get("state") not in TRIAGE_STATES
        or _timestamp_text(triage.get("since")) is None
        or not isinstance(triage.get("reemitted"), bool)
    ):
        raise CursorError("entry")
    checked_pending = None
    if pending is not None:
        if (
            not isinstance(pending, dict)
            or safe_delivery_key(pending.get("delivery_key")) is None
            or safe_event_id(pending.get("event_id")) is None
            or pending.get("kind") not in TRACKER_KINDS
            or _timestamp_text(pending.get("emitted_at")) is None
            or not isinstance(pending.get("retry"), bool)
        ):
            raise CursorError("pending")
        checked_pending = {
            key: pending[key] for key in ("delivery_key", "event_id", "kind", "emitted_at", "retry")
        }
    return {
        "ref": raw["ref"],
        "updated_at": updated_at,
        "last_delivered_event_id": delivered,
        "pending": checked_pending,
        "triage": {
            "state": triage["state"],
            "since": triage["since"],
            "reemitted": triage["reemitted"],
        },
    }


def _load_cursor() -> dict[str, Any]:
    try:
        text = CURSOR_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_cursor()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CursorError("json") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("issues"), dict):
        raise CursorError("shape")
    issues: dict[str, Any] = {}
    for key, entry in raw["issues"].items():
        if not isinstance(key, str) or _ENTRY_KEY_RE.fullmatch(key) is None:
            raise CursorError("key")
        issues[key] = _checked_entry(entry)
    return {"version": 1, "issues": issues}


def _save_cursor(cursor: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"cursor.{os.getpid()}.tmp"
    try:
        with tmp.open('w', encoding='utf-8') as handle:
            json.dump(cursor, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CURSOR_FILE)
        directory_fd = os.open(STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _seed_state_gitignore() -> None:
    """Seed STATE_DIR/.gitignore (only if absent). Best-effort; never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gi = STATE_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text(_STATE_GITIGNORE, encoding='utf-8')
    except OSError:
        pass


# --- one fire -------------------------------------------------------------


class _Fire:
    """State of one fire: the cursor, the caps spent so far and the buffered output."""

    def __init__(
        self,
        *,
        home: Path,
        config: TrackerConfig,
        env: Mapping[str, str],
        transport: Any,
        work: ChainlinkWork,
        gh_runner: Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"],
        now: datetime,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.home = home
        self.config = config
        self.env = env
        self.transport = transport
        self.work = work
        self.gh_runner = gh_runner
        self.now = now
        self.deadline = deadline
        self.clock = clock
        self.cursor = _load_cursor()
        self.out: list[dict[str, Any]] = []
        self.reads = 0
        self.finds = 0
        self.items = 0
        self.withheld = 0

    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    # step 0
    def run_drain(self) -> None:
        try:
            drain(self.home, self.config, self.env, self.transport, self.gh_runner, self._notice)
        except (OSError, ValueError, EvidenceError):
            _diag("drain_failed")

    def _notice(self, notice: Mapping[str, Any]) -> None:
        if notice.get("kind") != NOTICE_KIND:
            return
        keys = ("delivery_key", "adapter", "ref", "url", "lifecycle_event", "reason")
        if not all(isinstance(notice.get(key), str) for key in keys):
            return
        self.out.append(
            {"signal": NOTICE_KIND, "poller": POLLER_NAME, **{key: notice[key] for key in keys}}
        )

    # step 1
    def list_adapters(self) -> list[tuple[str, Any, list[TrackerIssue], bool]]:
        listings = []
        for name in sorted(self.config.adapters):
            try:
                adapter = build_adapter(name, self.config.adapters[name], self.env, self.transport)
            except ValueError:
                _diag(f"adapter_unavailable:{name}:config")
                continue
            try:
                issues, complete = adapter.list_assigned()
            except TrackerUnavailable as exc:
                _diag(f"adapter_unavailable:{name}:{exc.code}")
                continue
            if len(issues) > LIST_CAP:
                issues, complete = issues[:LIST_CAP], False
            listings.append((name, adapter, issues, complete))
        return listings

    # step 2
    def process(self, name: str, adapter: Any, issues: list[TrackerIssue]) -> list[tuple[Any, TrackerIssue, str]]:
        queue = []
        seen: set[str] = set()
        for listed in issues:
            if (
                listed.adapter != name
                or _STABLE_ID_RE.fullmatch(listed.stable_id) is None
                or safe_ref(listed.ref) is None
            ):
                continue
            key = f"{name}:{listed.stable_id}"
            if key in seen:
                continue
            seen.add(key)
            entry = self.cursor["issues"].setdefault(key, _new_entry(listed.ref, self.now))
            entry["ref"] = listed.ref
            queue.append((adapter, listed, key))
        return queue

    def adopt(self, queue: list[tuple[Any, TrackerIssue, str]]) -> None:
        candidates = [
            work_item for work_item in queue
            if self.cursor["issues"][work_item[2]]["triage"]["state"] != _FILED
        ]
        candidates.sort(key=lambda w: ADOPTION_ORDER[self.cursor["issues"][w[2]]["triage"]["state"]])
        for _, listed, key in candidates:
            if self.finds >= FIND_CAP or self.out_of_time():
                return
            self.finds += 1
            self._adopt_one(listed, self.cursor["issues"][key])

    def _adopt_one(self, listed: TrackerIssue, entry: dict[str, Any]) -> None:
        external = f"{listed.adapter}:{listed.ref}"
        try:
            ids = self.work.find(external)
            if not ids:
                return
            shown = [self.work.show(issue_id) for issue_id in ids]
            if len(ids) == 1 and self.work.never_armed(shown[0]):
                self.work.arm(ids[0])
        except WorkUnavailable:
            _diag("chainlink_unavailable")
            return
        entry["triage"] = {"state": _FILED, "since": _stamp(self.now), "reemitted": False}
        description = shown[0].get("description")
        if isinstance(description, str):
            try:
                request_transition(self.home, description, "filed")
            except (OSError, ValueError, EvidenceError):
                _diag("transition_request_failed")

    def reconcile_receipt(self, entry: dict[str, Any]) -> None:
        pending = entry["pending"]
        if pending is None or not delivery_receipt_exists(STATE_DIR, pending["delivery_key"]):
            return
        entry["last_delivered_event_id"] = pending["event_id"]
        triage = entry["triage"]
        if pending["kind"] == "triage":
            if pending["retry"]:
                triage["reemitted"] = True
            elif triage["state"] != _FILED:
                entry["triage"] = {"state": "pending", "since": _stamp(self.now), "reemitted": False}
        entry["pending"] = None

    def retry_due(self, entry: dict[str, Any]) -> bool:
        triage = entry["triage"]
        if triage["state"] != "pending" or triage["reemitted"]:
            return False
        return self.now - parse_timestamp(triage["since"]) > RETRY_AFTER

    def visit(self, adapter: Any, listed: TrackerIssue, key: str) -> None:
        entry = self.cursor["issues"][key]
        self.reconcile_receipt(entry)
        listed_at = _timestamp_text(listed.updated_at)
        changed = listed_at is None or listed_at != entry["updated_at"]
        if not (changed or entry["pending"] is not None or self.retry_due(entry)):
            return
        if self.reads >= READ_CAP or self.out_of_time():
            return
        self.reads += 1
        try:
            issue = adapter.read(listed.ref)
            decision = self._decide(issue, adapter)
        except TrackerUnavailable as exc:
            _diag(f"adapter_unavailable:{listed.adapter}:{exc.code}")
            return
        except ValueError:
            _diag(f"adapter_unavailable:{listed.adapter}:invalid")
            return
        if (issue.adapter, issue.ref, issue.stable_id) != (listed.adapter, listed.ref, listed.stable_id):
            _diag("read_mismatch")
            return
        self.apply(entry, key, issue, decision, listed_at)

    @staticmethod
    def _decide(issue: TrackerIssue, adapter: Any) -> Decision:
        return delivery_decision(issue, adapter.trust)

    def _commit(self, entry: dict[str, Any], listed_at: str | None) -> None:
        if entry["pending"] is None and listed_at is not None:
            entry["updated_at"] = listed_at

    def apply(
        self,
        entry: dict[str, Any],
        key: str,
        issue: TrackerIssue,
        decision: Decision,
        listed_at: str | None,
    ) -> None:
        if decision.kind == "unavailable":
            return
        if decision.kind == "skip":
            entry["pending"] = None
            self._commit(entry, listed_at)
            return
        if decision.kind == "withhold":
            entry["pending"] = None
            self._withhold(entry, key, issue, decision.reason or "withheld", listed_at)
            return
        self._deliver(entry, key, issue, decision, listed_at)

    def _withhold(
        self, entry: dict[str, Any], key: str, issue: TrackerIssue, reason: str, listed_at: str | None,
    ) -> None:
        others = [a for a in issue.activities if a.actor_id is None or a.actor_id != issue.bot_id]
        latest = max(others, key=lambda a: (a.at, a.id)).id if others else "none"
        delivery_key = _withheld_key(key, latest, reason)
        if delivery_receipt_exists(STATE_DIR, delivery_key):
            self._commit(entry, listed_at)
            return
        if self.withheld >= WITHHELD_CAP:
            return
        self.withheld += 1
        self.out.append({
            "signal": "tracker_issue_withheld",
            "poller": POLLER_NAME,
            "delivery_key": delivery_key,
            "adapter": issue.adapter,
            "ref": issue.ref,
            "url": issue.url,
            "reason": reason,
            "title": issue.title[:TITLE_CHARS],
        })

    def _deliver(
        self,
        entry: dict[str, Any],
        key: str,
        issue: TrackerIssue,
        decision: Decision,
        listed_at: str | None,
    ) -> None:
        cutoff = decision.cutoff
        assert cutoff is not None
        triage = entry["triage"]
        replied = any(
            a.kind == "comment" and a.actor_id == issue.bot_id and a.at > cutoff.at
            for a in issue.activities
        )
        state = "responded" if triage["state"] == "pending" and replied else triage["state"]
        if cutoff.id == entry["last_delivered_event_id"]:
            if state == "pending" and self.retry_due(entry) and not replied:
                self._emit_item(entry, key, issue, "triage", cutoff.id, retry=True)
                return
            if state != triage["state"]:
                entry["triage"] = {"state": state, "since": _stamp(self.now), "reemitted": False}
            entry["pending"] = None
            self._commit(entry, listed_at)
            return
        kind = "update" if state == _FILED else "triage"
        if self._emit_item(entry, key, issue, kind, cutoff.id, retry=False) and state != triage["state"]:
            entry["triage"] = {"state": state, "since": _stamp(self.now), "reemitted": False}

    def _emit_item(
        self,
        entry: dict[str, Any],
        key: str,
        issue: TrackerIssue,
        kind: str,
        event_id: str,
        *,
        retry: bool,
    ) -> bool:
        if self.items >= ITEM_CAP:
            return False
        url = safe_url(issue.url, issue.ref)
        if url is None or safe_adapter(issue.adapter) is None:
            _diag("issue_url_invalid")
            return False
        self.items += 1
        delivery_key = _item_key(kind, key, event_id, retry=retry)
        self.out.append({
            "poller": POLLER_NAME,
            "prompt": render_prompt(kind, issue),
            "delivery_key": delivery_key,
            "adapter": issue.adapter,
            "ref": issue.ref,
            "url": url,
            "kind": kind,
            "attested_event_id": event_id,
        })
        entry["pending"] = {
            "delivery_key": delivery_key,
            "event_id": event_id,
            "kind": kind,
            "emitted_at": _stamp(self.now),
            "retry": retry,
        }
        return True

    # step 3
    def drop_unlisted(self, name: str, listed_keys: set[str]) -> None:
        issues = self.cursor["issues"]
        for key in [k for k in issues if k.startswith(f"{name}:") and k not in listed_keys]:
            del issues[key]


def run_fire(
    *,
    home: Path,
    config: TrackerConfig,
    env: Mapping[str, str],
    transport: Any,
    work: ChainlinkWork,
    gh_runner: Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"],
    now: datetime,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Run one fire, save the cursor and return the records to print.

    Records are printed only after the cursor is durable, so a record the host has
    received always has its ``pending`` entry on disk.
    """

    fire = _Fire(
        home=home, config=config, env=env, transport=transport, work=work,
        gh_runner=gh_runner, now=now, deadline=deadline, clock=clock,
    )
    fire.run_drain()
    queues = []
    complete_listings = []
    for name, adapter, issues, complete in fire.list_adapters():
        queue = fire.process(name, adapter, issues)
        queues.append(queue)
        if complete:
            complete_listings.append((name, {key for _, _, key in queue}))
    everything = [work_item for queue in queues for work_item in queue]
    fire.adopt(everything)
    for adapter, listed, key in everything:
        fire.visit(adapter, listed, key)
    for name, listed_keys in complete_listings:
        fire.drop_unlisted(name, listed_keys)
    _save_cursor(fire.cursor)
    return fire.out


def _gh_runner(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(args), text=True, capture_output=True, check=False, timeout=GH_TIMEOUT_SECONDS,
    )


def main() -> None:
    _seed_state_gitignore()
    home_text = os.environ.get("MIMIR_HOME", "").strip()
    if not home_text:
        _diag("home_unset")
        return
    home = Path(home_text)
    try:
        config = WorklinkConfig.load(home / "worklink.yaml").tracker
    except (OSError, ValueError, yaml.YAMLError):
        _diag("config_invalid")
        return
    if not config.adapters:
        return
    env = {key: value for key, value in os.environ.items() if key in ADAPTER_ENV_KEYS}
    try:
        timeout = float(os.environ.get("POLLER_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    deadline = time.monotonic() + max(timeout - DEADLINE_MARGIN_SECONDS, 5.0)
    records = run_fire(
        home=home,
        config=config,
        env=env,
        transport=tracker_transport.default_transport(),
        work=ChainlinkWork(cwd=home),
        gh_runner=_gh_runner,
        now=_now(),
        deadline=deadline,
    )
    for record in records:
        _emit(record)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        _diag("fatal:" + type(exc).__name__.lower())
        sys.exit(1)
