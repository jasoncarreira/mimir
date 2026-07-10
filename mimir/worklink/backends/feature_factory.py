"""Feature-factory Worklink backend for worklink:epic issues (chainlink #833).

A thin adapter that connects Chainlink epic issues to the external opencode
feature-factory's **autonomous mode**. The factory self-drives every gate
(story/brief self-approved when unambiguous; pre_pr decided by its own
implementation-validator + security-reviewer panel), runs bounded remediation,
never auto-merges, opens a PR, and writes ``run.json.terminal_result`` at a
terminal state.

The adapter's job is therefore thin: **launch ``factory start --autonomous
--detached`` once, poll ``run.json`` (the factory's own live control plane) to a
terminal state, and mirror the outcome to Chainlink.** ``--detached`` backgrounds
opencode and returns the launcher immediately, so the orchestrator does NOT hold
the subprocess for the whole run — it launches, then polls ``run.json`` (a
re-dispatch can resume polling a detached factory left running by a prior
interrupted dispatch). There is no resume/gate-answer step — the factory
self-drives one long run to a terminal state. This module owns the factory
contract: the CLI argv and the ``run.json`` shape (incl. ``terminal_result``).
The launch+poll+mirror loop lives in the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
from typing import ClassVar

from ..compute import ComputeResult, WorkSpec
from .base import Caps, RawResult, WorkOrder


FACTORY_DIR = ".opencode/factory"
RUN_JSON = "run.json"
GATES_DIR = "gates"
QUESTION_SUFFIX = ".question.md"
ANSWER_SUFFIX = ".answer"

# Heartbeat staleness threshold (seconds). A run.json whose ``heartbeat_at`` is
# older than this — or unparseable/absent — is treated as a stuck factory.
STALE_THRESHOLD_S = 300


@dataclass(frozen=True)
class FactoryTerminalResult:
    """Parsed view of ``run.json.terminal_result`` — the factory's authoritative
    outcome, written once the autonomous run reaches a terminal state.

    ``status`` mirrors the run status at termination (``completed`` |
    ``blocked`` | ``partial`` | ``needs-human``). ``pr_url`` is the draft PR the
    factory opened (present on a shippable ``completed``). ``reason``/``summary``
    are the human-readable outcome the adapter surfaces on a non-shippable
    terminal transition.
    """

    status: str
    pr_url: str | None = None
    reason: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class FactoryRunState:
    """Parsed view of the factory's ``run.json`` control plane.

    Matches the REAL factory contract (there is no ``schema_version`` and no
    top-level ``gates_needed``): gates live under ``gates.<name>.status``, the
    panel verdicts under ``validator.verdict`` / ``security_review.verdict``,
    slice progress under ``slices[].{id,status}``, and — at a terminal state —
    the authoritative outcome under ``terminal_result``.
    """

    run_id: str
    status: str
    heartbeat_at: str
    pr_url: str | None = None
    # (gate-name, status) pairs, order-preserving, as read from run.json ``gates``.
    gate_statuses: tuple[tuple[str, str], ...] = ()
    # (slice-id, status) pairs, order-preserving, from run.json ``slices``.
    slices: tuple[tuple[str, str], ...] = ()
    validator_verdict: str | None = None
    security_verdict: str | None = None
    error: str | None = None
    # Present only at a terminal state (agent-written; may be absent even then —
    # the adapter falls back to status/pr_url/gates when it is None).
    terminal_result: FactoryTerminalResult | None = None

    # Gate order the factory presents them in (story -> brief -> pre_pr).
    GATE_ORDER: ClassVar[tuple[str, ...]] = ("story", "brief", "pre_pr")
    # Statuses that mean "no more gates will open on their own". ``running`` is
    # the only non-terminal status; ``blocked``/``partial``/``needs-human`` are
    # terminal-needs-human, ``completed`` is the success terminal.
    TERMINAL_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"completed", "blocked", "partial", "needs-human"}
    )

    @property
    def pending_gate(self) -> str | None:
        """The first pending gate in canonical order, or the first pending gate
        the factory added beyond the known set."""
        statuses = {name: status for name, status in self.gate_statuses}
        for name in self.GATE_ORDER:
            if (statuses.get(name) or "").strip().lower() == "pending":
                return name
        for name, status in self.gate_statuses:
            if (status or "").strip().lower() == "pending":
                return name
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status.strip().lower() in self.TERMINAL_STATUSES

    @property
    def is_stale(self) -> bool:
        try:
            heartbeat = datetime.fromisoformat(self.heartbeat_at.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return True
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        return (datetime.now(UTC) - heartbeat).total_seconds() > STALE_THRESHOLD_S


def _parse_gate_statuses(gates: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(gates, dict):
        return ()
    parsed: list[tuple[str, str]] = []
    for name, spec in gates.items():
        if isinstance(spec, dict):
            status = str(spec.get("status") or "")
        elif isinstance(spec, str):
            status = spec
        else:
            status = ""
        parsed.append((str(name), status))
    return tuple(parsed)


def _parse_slices(slices: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(slices, list):
        return ()
    parsed: list[tuple[str, str]] = []
    for item in slices:
        if isinstance(item, dict):
            parsed.append((str(item.get("id") or ""), str(item.get("status") or "")))
    return tuple(parsed)


def _parse_terminal_result(data: dict) -> FactoryTerminalResult | None:
    """Parse ``run.json.terminal_result`` — None when absent or malformed.

    Absent is normal for a still-running run and tolerated even at a terminal
    state (it is agent-written): the adapter falls back to status/pr_url/gates.
    """
    tr = data.get("terminal_result")
    if not isinstance(tr, dict):
        return None
    status = str(tr.get("status") or "").strip()
    if not status:
        return None

    def _clean(key: str) -> str | None:
        value = tr.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return FactoryTerminalResult(
        status=status,
        pr_url=_clean("pr_url"),
        reason=_clean("reason"),
        summary=_clean("summary"),
    )


def _panel_verdict(panel: object) -> str | None:
    if isinstance(panel, dict):
        verdict = panel.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            return verdict.strip().upper()
    return None


def _blocked_reason(data: dict) -> str | None:
    for key in ("blocked_reason", "reason", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_run_state(data: object) -> FactoryRunState | None:
    """Parse a decoded ``run.json`` payload into a FactoryRunState.

    Returns None only when the payload is not a JSON object — a missing/empty
    ``heartbeat_at`` is preserved (``is_stale`` handles it) rather than dropped,
    so a stuck factory surfaces as stale instead of vanishing.
    """
    if not isinstance(data, dict):
        return None
    pr_url = data.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        pr_url = None
    else:
        pr_url = pr_url.strip()
    return FactoryRunState(
        run_id=str(data.get("run_id") or ""),
        status=str(data.get("status") or "unknown"),
        heartbeat_at=str(data.get("heartbeat_at") or ""),
        pr_url=pr_url,
        gate_statuses=_parse_gate_statuses(data.get("gates")),
        slices=_parse_slices(data.get("slices")),
        validator_verdict=_panel_verdict(data.get("validator")),
        security_verdict=_panel_verdict(data.get("security_review")),
        error=_blocked_reason(data),
        terminal_result=_parse_terminal_result(data),
    )


@dataclass(frozen=True)
class FeatureFactoryBackend:
    """Adapter for opencode feature-factory Worklink jobs (autonomous mode).

    Builds the factory's autonomous CLI invocation for a worklink:epic issue.
    The factory self-drives every gate and writes ``run.json``; the orchestrator
    launches once, observes, and mirrors the outcome. This backend only shapes
    the argv and reads the factory's run.json.

    ``bin`` may be multi-token (e.g. ``"node /path/to/src/cli.js"``): the
    feature-factory CLI is not necessarily on PATH, so it is shlex-split when the
    command is assembled.

    ``ready_for_review`` (default on) adds ``--ready`` so the factory opens a
    ready-for-review PR rather than a draft — the mimir flow wants review-ready.
    ``reviewer`` (default from ``MIMIR_FACTORY_REVIEWER``) adds ``--reviewer
    <name>`` so the factory requests review itself; empty omits the flag.
    """

    bin: str = "opencode"
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    name: str = "feature_factory"
    heartbeat_interval_s: int = 60
    poll_interval_s: int = 10
    ready_for_review: bool = True
    reviewer: str = field(
        default_factory=lambda: os.environ.get("MIMIR_FACTORY_REVIEWER", "mimir-carreira")
    )

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="feature-factory",
            persistent_sessions=True,
            json_output=False,
            native_pr_creation=False,
            worktree_safe=False,
            quota_pool=None,
        )

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        factory_path = order.worktree / FACTORY_DIR
        run_json_path = factory_path / RUN_JSON
        command = self._factory_command(order.worktree, order.prompt)

        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            backend_config={
                "bin": self.bin,
                "args": list(self.extra_args),
                "factory_path": str(factory_path),
                "run_json_path": str(run_json_path),
            },
            local_worktree=order.worktree,
            local_argv=command,
        )

    def _bin_tokens(self) -> tuple[str, ...]:
        return tuple(shlex.split(self.bin)) if self.bin.strip() else ()

    def _factory_command(self, worktree: Path, prompt: str) -> tuple[str, ...]:
        """Autonomous DETACHED factory START argv.

        ``<bin...> factory start --autonomous --detached --repo <worktree>
        [extra_args] [--ready] [--reviewer <name>] <prompt>``. ``--detached``
        makes the CLI spawn opencode BACKGROUNDED (``detached``/``unref``'d,
        logging to ``.opencode/factory/processes/<ts>.log``) and RETURN
        IMMEDIATELY — the launcher exits while the autonomous run keeps going, so
        the orchestrator polls ``run.json`` to a terminal state rather than
        holding the subprocess for the whole run (and a re-dispatch can resume
        polling a still-running detached factory). There is no resume/gate-answer
        step: the factory self-drives every gate and writes
        ``run.json.terminal_result`` at a terminal state. The factory's opencode
        PREFERS the codex OAuth subscription when available (codex-auth plugin)
        and falls back to ``OPENAI_API_KEY`` only if that OAuth is absent.
        """
        argv: list[str] = [
            *self._bin_tokens(),
            "factory",
            "start",
            "--autonomous",
            "--detached",
            "--repo",
            str(worktree),
            *self.extra_args,
        ]
        if self.ready_for_review:
            argv.append("--ready")
        if self.reviewer.strip():
            argv.extend(["--reviewer", self.reviewer.strip()])
        argv.append(prompt)
        return tuple(argv)

    def _read_run_json(self, worktree: Path, run_id: str) -> FactoryRunState | None:
        return read_factory_run_state(worktree, run_id)

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        if not isinstance(result, ComputeResult):
            raise TypeError("FeatureFactoryBackend.interpret expects ComputeResult")

        if result.launch_error:
            return RawResult(-1, None, "backend_error", result.launch_error)

        state = self._read_run_json(order.worktree, epic_run_id(order.issue_id))
        if state is None:
            return RawResult(-1, None, "failed", "factory run.json not found")
        if state.is_stale:
            return RawResult(-1, None, "stale_heartbeat", f"factory heartbeat stale: {state.heartbeat_at}")

        status = state.status.strip().lower()
        if status == "completed":
            return RawResult(0, None, "completed", None)
        if status in ("blocked", "partial", "needs-human"):
            return RawResult(0, None, "blocked", state.error or f"factory status: {status}")
        if state.pending_gate:
            return RawResult(0, None, "blocked", f"gate required: {state.pending_gate}")
        return RawResult(0, None, "in_progress", None)


def epic_run_id(issue_id: int) -> str:
    """Factory run-id the adapter assigns a worklink:epic. The factory namespaces
    each run's control plane under ``.opencode/factory/<run-id>/``, so the START
    prompt tells the factory to use this id and the adapter reads/writes the same
    run dir."""
    return f"chainlink-{issue_id}"


def factory_run_dir(repo_path: Path, run_id: str) -> Path:
    """The factory's per-run control-plane dir: ``.opencode/factory/<run-id>/``."""
    return repo_path / FACTORY_DIR / run_id


def read_factory_run_state(repo_path: Path, run_id: str) -> FactoryRunState | None:
    """Read the factory run state for ``run_id`` from a repository/worktree.

    Standalone so the orchestrator/poller can inspect factory state without
    launching a backend. Returns None if run.json is absent or unreadable.
    """
    run_json_path = factory_run_dir(repo_path, run_id) / RUN_JSON
    if not run_json_path.exists():
        return None
    try:
        data = json.loads(run_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return _parse_run_state(data)


def gate_answer_path(repo_path: Path, run_id: str, gate: str) -> Path:
    """Path to a gate answer file (the factory's answer file protocol)."""
    return factory_run_dir(repo_path, run_id) / GATES_DIR / f"{gate}{ANSWER_SUFFIX}"


def gate_question_path(repo_path: Path, run_id: str, gate: str) -> Path:
    """Path to a gate question file the factory writes when it stops at a gate."""
    return factory_run_dir(repo_path, run_id) / GATES_DIR / f"{gate}{QUESTION_SUFFIX}"


def question_mtime(repo_path: Path, run_id: str, gate: str) -> int:
    """Nanosecond mtime of a gate's question file (0 if absent).

    Nanosecond resolution so a fast re-opened gate (the pre_pr ``changes`` loop)
    never collides with the prior instance within the same wall-clock second.
    """
    try:
        return gate_question_path(repo_path, run_id, gate).stat().st_mtime_ns
    except OSError:
        return 0


def read_gate_answer(repo_path: Path, run_id: str, gate: str) -> str | None:
    """Read a gate answer from the factory's file protocol."""
    answer_path = gate_answer_path(repo_path, run_id, gate)
    if not answer_path.exists():
        return None
    try:
        return answer_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_gate_answer(repo_path: Path, run_id: str, gate: str, answer: str) -> None:
    """Write a gate answer to the factory's file protocol."""
    answer_path = gate_answer_path(repo_path, run_id, gate)
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(answer, encoding="utf-8")


def has_concurrent_factory_session(
    repo_path: Path, *, exclude_run_id: str | None = None
) -> bool:
    """True if any OTHER factory run is non-terminal and non-stale.

    Detached runs live in per-attempt checkouts
    (``<repo>/.worklink/<issue>-<attempt>/.opencode/factory/<run-id>/run.json``),
    not under the repo root, so scan the repo-root control plane AND every
    ``.worklink`` attempt checkout — otherwise the "one factory session at a time"
    guard never sees the sessions the detached adapter actually creates.
    ``exclude_run_id`` skips the caller's own run so a resume/re-dispatch of the
    same epic is not counted as a concurrent session.
    """
    roots = [repo_path / FACTORY_DIR]
    worklink_root = repo_path / ".worklink"
    if worklink_root.is_dir():
        roots.extend(
            attempt / FACTORY_DIR
            for attempt in worklink_root.iterdir()
            if attempt.is_dir()
        )
    for factory_root in roots:
        if not factory_root.is_dir():
            continue
        for run_json in factory_root.glob(f"*/{RUN_JSON}"):
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            state = _parse_run_state(data)
            if state is None or state.is_terminal or state.is_stale:
                continue
            if exclude_run_id is not None and state.run_id == exclude_run_id:
                continue
            return True
    return False
