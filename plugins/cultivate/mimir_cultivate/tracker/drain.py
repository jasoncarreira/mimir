"""Drain tracker evidence: apply queued transitions and backstop closures, then emit notices.

Runs inside the tracker-poller process, whose environment carries the tracker
credentials. Every external call happens outside the evidence lock: a stage is
persisted as ``started`` first, the call is made, and the outcome is recorded in a
second transaction. A request that reached a final stage is never processed again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mimir.poller_tracker import safe_url

from ..worklink.dispatch_failures import delivery_receipt_exists
from ..worklink.merge_closure import ClosureReadError, Runner, read_pr_snapshot
from .adapters import build_adapter
from .adapters.base import ActionResult, TrackerAdapter
from .config import TrackerConfig, resolve_transition
from .evidence import evidence_transaction, read_evidence
from .record import ExternalRecord
from .transport import TrackerTransport, TrackerUnavailable

MAX_ACTIONS = 10
MAX_ATTEMPTS = 3
ADAPTER_UNAVAILABLE = "adapter_unavailable"
NOTICE_KIND = "tracker_transition_unavailable"
_OPEN_STAGES = ("requested", "started")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def poller_state_dir(home: Path) -> Path:
    return home / "state" / "pollers" / "tracker-poller"


def _split(ext_ref: str) -> tuple[str, str]:
    adapter, _, ref = ext_ref.partition(":")
    return adapter, ref


def _record(ext_ref: str) -> ExternalRecord:
    adapter, ref = _split(ext_ref)
    return ExternalRecord(adapter, ref, "")


def _notice_key(ext_ref: str, event: str, reason: str, created_at: str) -> str:
    digest = hashlib.sha256(f"notice:{ext_ref}:{event}:{reason}:{created_at}".encode())
    return "trk-" + digest.hexdigest()[:24]


class _Drain:
    def __init__(
        self,
        home: Path,
        config: TrackerConfig,
        env: Mapping[str, str],
        transport: TrackerTransport,
        gh_runner: Runner,
        emit: Callable[[dict[str, str]], None],
        clock: Callable[[], str],
        max_actions: int,
    ) -> None:
        self.home = home
        self.config = config
        self.env = env
        self.transport = transport
        self.gh_runner = gh_runner
        self.emit = emit
        self.clock = clock
        self.budget = max_actions
        self.actions = 0
        self._adapters: dict[str, TrackerAdapter | None] = {}

    def adapter(self, name: str) -> TrackerAdapter | None:
        if name not in self._adapters:
            adapter_config = self.config.adapters.get(name)
            adapter = None
            if adapter_config is not None:
                try:
                    adapter = build_adapter(name, adapter_config, self.env, self.transport)
                except ValueError:
                    adapter = None
            self._adapters[name] = adapter
        return self._adapters[name]

    def spend(self) -> bool:
        if self.actions >= self.budget:
            return False
        self.actions += 1
        return True

    def static_url(self, ext_ref: str) -> str:
        """Build the canonical issue URL from the reference alone; "" when that is not possible."""

        adapter_name, ref = _split(ext_ref)
        url: str | None = None
        if adapter_name == "github_issues":
            repo, _, number = ref.partition("#")
            url = f"https://github.com/{repo}/issues/{number}"
        elif adapter_name == "jira":
            site = self.env.get("JIRA_SITE", "").strip()
            if site:
                url = f"https://{site}/browse/{ref}"
        return safe_url(url, ref) or ""

    def resolve_url(self, ext_ref: str) -> str:
        """Resolve a notice URL read-only, without spending the action budget; "" when unavailable.

        Reads the tracker for Linear, so it must be called outside the evidence lock.
        """

        url = self.static_url(ext_ref)
        adapter_name, ref = _split(ext_ref)
        if url or adapter_name != "linear":
            return url
        adapter = self.adapter(adapter_name)
        if adapter is None:
            return ""
        try:
            return safe_url(adapter.read(ref).url, ref) or ""
        except TrackerUnavailable:
            return ""

    def _adapter_missing(self, ext_ref: str, event: str) -> None:
        """Record one notice; a missing Linear adapter or Jira without JIRA_SITE yields no deliverable notice."""

        url = self.static_url(ext_ref)
        with evidence_transaction(self.home) as data:
            adapter, ref = _split(ext_ref)
            if any(
                (n["adapter"], n["ref"], n["lifecycle_event"], n["reason"])
                == (adapter, ref, event, ADAPTER_UNAVAILABLE)
                for n in data["notices"]
            ):
                return
            self._add_notice(data, ext_ref, event, ADAPTER_UNAVAILABLE, url)

    def _add_notice(
        self, data: dict[str, Any], ext_ref: str, event: str, reason: str, url: str
    ) -> None:
        created_at = self.clock()
        adapter, ref = _split(ext_ref)
        data["notices"].append(
            {
                "delivery_key": _notice_key(ext_ref, event, reason, created_at),
                "adapter": adapter,
                "ref": ref,
                "url": url,
                "lifecycle_event": event,
                "reason": reason,
                "created_at": created_at,
                "notified": False,
            }
        )

    def notices(self) -> None:
        """Emit deliverable notices, resolving empty URLs lazily.

        Resolution is one read-only call per unresolved Linear notice per fire. It is outside
        the evidence lock and never touches the action budget, so it cannot starve actions.
        """

        pending = [n for n in read_evidence(self.home)["notices"] if not n.get("notified")]
        if not pending:
            return
        resolved: dict[str, str] = {}
        for notice in pending:
            if not notice["url"]:
                url = self.resolve_url(f"{notice['adapter']}:{notice['ref']}")
                if url:
                    resolved[notice["delivery_key"]] = url
        state_dir = poller_state_dir(self.home)
        delivered: set[str] = set()
        for notice in pending:
            key = notice["delivery_key"]
            url = notice["url"] or resolved.get(key, "")
            if delivery_receipt_exists(state_dir, key):
                delivered.add(key)
                continue
            if not url:
                continue
            self.emit(
                {
                    "kind": NOTICE_KIND,
                    "delivery_key": key,
                    "adapter": notice["adapter"],
                    "ref": notice["ref"],
                    "url": url,
                    "lifecycle_event": notice["lifecycle_event"],
                    "reason": notice["reason"],
                }
            )
        if delivered or resolved:
            with evidence_transaction(self.home) as data:
                for notice in data["notices"]:
                    if notice["delivery_key"] in delivered:
                        notice["notified"] = True
                    if not notice["url"] and notice["delivery_key"] in resolved:
                        notice["url"] = resolved[notice["delivery_key"]]

    def _finish(
        self,
        ext_ref: str,
        section: str,
        key: str | None,
        *,
        result: str,
        reason: str | None,
        notice: str | None = None,
        **extra: Any,
    ) -> None:
        with evidence_transaction(self.home) as data:
            entry = self._entry(data, ext_ref, section, key)
            if entry is None:
                return
            entry.update(
                stage="done", result=result, reason=reason, recorded_at=self.clock(), **extra
            )
            if notice is not None:
                self._add_notice(
                    data, ext_ref, notice, reason or result, self.static_url(ext_ref)
                )

    def _retry(
        self,
        ext_ref: str,
        section: str,
        key: str | None,
        code: str,
        attempts: int,
        event: str,
        **extra: Any,
    ) -> None:
        if attempts >= MAX_ATTEMPTS:
            self._finish(
                ext_ref, section, key, result="failed", reason=code, notice=event, **extra
            )
            return
        with evidence_transaction(self.home) as data:
            entry = self._entry(data, ext_ref, section, key)
            if entry is not None:
                entry.update(stage="requested", reason=code)

    @staticmethod
    def _entry(
        data: dict[str, Any], ext_ref: str, section: str, key: str | None
    ) -> dict[str, Any] | None:
        item = data["items"].get(ext_ref)
        if item is None:
            return None
        if key is None:
            return item.get(section)
        return item.get(section, {}).get(key)

    def _start(
        self, ext_ref: str, section: str, key: str | None, event: str, **fields: Any
    ) -> int | None:
        current = self._entry(read_evidence(self.home), ext_ref, section, key)
        if current is not None and current["attempts"] >= MAX_ATTEMPTS:
            if current["stage"] in _OPEN_STAGES:
                self._finish(
                    ext_ref, section, key,
                    result="failed", reason="attempts_exhausted", notice=event,
                )
            return None
        with evidence_transaction(self.home) as data:
            entry = self._entry(data, ext_ref, section, key)
            if entry is None or entry["stage"] not in _OPEN_STAGES:
                return None
            entry["attempts"] += 1
            entry.update(stage="started", **fields)
            return entry["attempts"]

    def transitions(self) -> None:
        items = read_evidence(self.home)["items"]
        for ext_ref, item in sorted(items.items()):
            for event, entry in sorted(item.get("transitions", {}).items()):
                if entry["stage"] not in _OPEN_STAGES or self.actions >= self.budget:
                    continue
                self._transition(ext_ref, event)

    def _transition(self, ext_ref: str, event: str) -> None:
        adapter_name, ref = _split(ext_ref)
        state = resolve_transition(self.config, _record(ext_ref), event)
        if state is None:
            self._finish(ext_ref, "transitions", event, result="unset", reason=None)
            return
        adapter = self.adapter(adapter_name)
        if adapter is None:
            self._adapter_missing(ext_ref, event)
            return
        if not self.spend():
            return
        attempts = self._start(ext_ref, "transitions", event, event, state=state)
        if attempts is None:
            return
        try:
            outcome = adapter.transition(ref, state)
        except TrackerUnavailable as exc:
            self._retry(ext_ref, "transitions", event, exc.code, attempts, event)
            return
        if outcome.status in ("applied", "already"):
            self._finish(ext_ref, "transitions", event, result=outcome.status, reason=None)
        else:
            self._finish(
                ext_ref,
                "transitions",
                event,
                result=outcome.status,
                reason=outcome.reason or outcome.status,
                notice=event,
            )

    def closures(self) -> None:
        if not self.config.close_on_merge:
            return
        items = read_evidence(self.home)["items"]
        for ext_ref, item in sorted(items.items()):
            closure = item.get("closure")
            if closure is None or closure["stage"] not in _OPEN_STAGES:
                continue
            if self.actions >= self.budget:
                return
            self._close(ext_ref, closure["pr_url"])

    def _drop_closure(self, ext_ref: str) -> None:
        with evidence_transaction(self.home) as data:
            item = data["items"].get(ext_ref)
            if item is None:
                return
            item.pop("closure", None)
            if not item:
                del data["items"][ext_ref]

    def _close(self, ext_ref: str, pr_url: str) -> None:
        adapter_name, ref = _split(ext_ref)
        adapter = self.adapter(adapter_name)
        if adapter is None:
            self._adapter_missing(ext_ref, "merged")
            return
        if not self.spend():
            return
        try:
            pr = read_pr_snapshot(pr_url, gh_bin="gh", runner=self.gh_runner)
        except ClosureReadError:
            self._read_failed(ext_ref)
            return
        if not pr.merged:
            if pr.state == "closed":
                self._drop_closure(ext_ref)
            return
        if pr.merge_commit_sha is None:
            return
        attempts = self._start(
            ext_ref,
            "closure",
            None,
            "merged",
            merge_commit_sha=pr.merge_commit_sha,
            base_ref=pr.base_ref,
        )
        if attempts is None:
            return
        extra = {"merge_commit_sha": pr.merge_commit_sha, "base_ref": pr.base_ref}
        state = resolve_transition(self.config, _record(ext_ref), "merged")
        try:
            outcome, result = self._apply_closure(adapter, ref, state)
        except TrackerUnavailable as exc:
            self._retry(ext_ref, "closure", None, exc.code, attempts, "merged", **extra)
            return
        if outcome.status in ("applied", "already"):
            self._finish(ext_ref, "closure", None, result=result, reason=None, **extra)
        else:
            self._finish(
                ext_ref,
                "closure",
                None,
                result=outcome.status,
                reason=outcome.reason or outcome.status,
                notice="merged",
                **extra,
            )

    @staticmethod
    def _apply_closure(
        adapter: TrackerAdapter, ref: str, state: str | None
    ) -> tuple[ActionResult, str]:
        current = adapter.current_state(ref)
        if state is not None:
            if current == state:
                return ActionResult("already"), "already_in_state"
            outcome = adapter.transition(ref, state)
            return outcome, "already_in_state" if outcome.status == "already" else "transitioned"
        if current == "closed":
            return ActionResult("already"), "already_closed"
        outcome = adapter.close(ref)
        return outcome, "already_closed" if outcome.status == "already" else "closed"

    def _read_failed(self, ext_ref: str) -> None:
        with evidence_transaction(self.home) as data:
            entry = self._entry(data, ext_ref, "closure", None)
            if entry is None:
                return
            entry["attempts"] += 1
            entry["reason"] = "pr_read_failed"
            if entry["attempts"] >= MAX_ATTEMPTS:
                entry.update(stage="done", result="failed", recorded_at=self.clock())
                self._add_notice(
                    data, ext_ref, "merged", "pr_read_failed", self.static_url(ext_ref)
                )


def drain(
    home: Path,
    config: TrackerConfig,
    env: Mapping[str, str],
    transport: TrackerTransport,
    gh_runner: Runner,
    emit: Callable[[dict[str, str]], None],
    *,
    clock: Callable[[], str] = _now,
    max_actions: int = MAX_ACTIONS,
) -> int:
    """Process pending tracker requests; return the number of external actions taken."""

    worker = _Drain(home, config, env, transport, gh_runner, emit, clock, max_actions)
    worker.notices()
    worker.transitions()
    worker.closures()
    return worker.actions
