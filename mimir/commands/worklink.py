"""CLI plumbing for ``mimir worklink``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import yaml

from ..worklink.docker_broker import DockerBrokerPolicy, DockerBrokerPolicyError, run_broker
from ..worklink.orchestrator import (
    LeafValidationError,
    WorklinkError,
    run_worklink,
    run_worklink_reattach,
)
from ..worklink.worker import payload_from_json, run_worker_payload
from ..worklink.factory import run_factory_epic


def add_argparse(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    worklink_p = sub.add_parser("worklink", help="Run Worklink executor jobs.")
    worklink_sub = worklink_p.add_subparsers(dest="worklink_action")

    run_p = worklink_sub.add_parser("run", help="Run one ready Chainlink leaf issue.")
    run_p.add_argument("issue_id", type=int, help="Chainlink issue id to execute.")
    run_p.add_argument(
        "--backend", default=None, help="Backend name (default: route from worklink.yaml)."
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the WorkOrder and stop before claiming/spawning.",
    )
    run_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    run_p.add_argument(
        "--repo", type=Path, default=None, help="Git repo to work in (default: cwd)."
    )
    run_p.add_argument(
        "--test-command", default=None, help="Override the configured evidence test command."
    )
    run_p.add_argument(
        "--base",
        default=None,
        help=(
            "Base branch to cut the worktree from and target the PR at "
            "(overrides worklink.yaml defaults.base_branch; default: main)."
        ),
    )
    run_p.add_argument(
        "--reattach",
        action="store_true",
        help=(
            "Resume an in-flight run after a controller restart (#561): wait on "
            "the persisted worker handle, harvest evidence, and open the PR "
            "instead of re-claiming and re-running from scratch. Used by the "
            "startup reconcile; no-op (exit 1) if no run state exists for the issue."
        ),
    )
    run_p.add_argument(
        "--autonomous",
        action="store_true",
        help=(
            "Mark this an autonomous dispatch (set by the ready-queue poller). "
            "Enforces the compute-backend autonomy policy: refuses the "
            "unsandboxed local_subprocess substrate unless "
            "defaults.allow_autonomous_local_subprocess is set. Omit for "
            "operator-invoked runs — they always proceed (accept-the-risk; the "
            "local_subprocess backend runs with full container filesystem access)."
        ),
    )

    factory_p = worklink_sub.add_parser(
        "factory",
        help="Drive the opencode feature-factory for one worklink:epic issue (chainlink #834).",
    )
    factory_p.add_argument("issue_id", type=int, help="Chainlink worklink:epic issue id to build.")
    factory_p.add_argument(
        "--home", type=Path, default=None, help="Agent home (overrides MIMIR_HOME; default: cwd)."
    )
    factory_p.add_argument(
        "--repo", type=Path, default=None, help="Git repo the factory builds in (default: cwd)."
    )
    factory_p.add_argument(
        "--autonomous",
        action="store_true",
        help="Mark this an autonomous dispatch (set by the ready-queue poller).",
    )

    worker_p = worklink_sub.add_parser("worker", help="Run one portable Worklink worker payload.")
    worker_p.add_argument("payload", type=Path, nargs="?", help="Path to worker payload JSON.")
    worker_p.add_argument(
        "--payload-json",
        default=None,
        help="Inline worker payload JSON (used by remote compute substrates).",
    )

    broker_p = worklink_sub.add_parser(
        "docker-broker",
        help="Run the narrow Docker-sibling broker for isolated Worklink workers.",
    )
    broker_p.add_argument(
        "--policy",
        type=Path,
        required=True,
        help="YAML/JSON broker policy file (allowed images, network, env allowlist).",
    )
    broker_p.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Unix socket path to bind instead of TCP (e.g. /run/worklink-broker.sock).",
    )
    broker_p.add_argument("--host", default="127.0.0.1", help="TCP host when --socket is not set.")
    broker_p.add_argument(
        "--port", type=int, default=8765, help="TCP port when --socket is not set."
    )

    return worklink_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.worklink_action is None:
        parser.print_help()
        return 1
    if args.worklink_action == "docker-broker":
        data = yaml.safe_load(args.policy.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            print("error: docker broker policy root must be a mapping", file=sys.stderr)
            return 2
        try:
            policy = DockerBrokerPolicy.from_mapping(data)
        except DockerBrokerPolicyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            asyncio.run(
                run_broker(policy=policy, host=args.host, port=args.port, socket_path=args.socket)
            )
        except KeyboardInterrupt:
            return 0
        return 0

    if args.worklink_action == "worker":
        if args.payload_json is not None:
            payload_data = json.loads(args.payload_json)
        elif args.payload is not None:
            payload_data = json.loads(args.payload.read_text(encoding="utf-8"))
        else:
            print("error: worklink worker requires payload path or --payload-json", file=sys.stderr)
            return 2
        payload = payload_from_json(payload_data)
        validation = asyncio.run(run_worker_payload(payload))
        suffix = " review-ready" if validation.review_ready else ""
        reason_suffix = _worker_reason_suffix(validation)
        print(f"worklink worker: {validation.status}{suffix}{reason_suffix}")
        return 0 if validation.status in {"completed", "blocked"} else 1

    if args.worklink_action == "factory":
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        repo = (args.repo or Path.cwd()).resolve()
        os.environ["MIMIR_HOME"] = str(home)
        try:
            from ..event_logger import init_logger

            init_logger(home / "logs" / "events.jsonl", session_id=f"factory-{args.issue_id}")
        except Exception:
            pass
        try:
            result = run_factory_epic(
                home=home, repo=repo, issue_id=args.issue_id, autonomous=args.autonomous
            )
        except WorklinkError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if result.status == "refused":
            print(f"factory #{result.issue_id}: refused — {result.reason}", file=sys.stderr)
            return 1
        print(
            f"factory #{result.issue_id}: {result.status}"
            + (f" PR {result.pr_url}" if result.pr_url else "")
            + (f" — {result.reason}" if result.reason else "")
        )
        return 0 if result.status in {"review_ready", "blocked"} else 1

    if args.worklink_action != "run":
        parser.print_help()
        return 1

    home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
    repo = (args.repo or Path.cwd()).resolve()
    os.environ["MIMIR_HOME"] = str(home)
    try:
        from ..event_logger import init_logger

        init_logger(
            home / "logs" / "events.jsonl",
            session_id=f"worklink-{args.issue_id}",
        )
    except Exception:
        # Worklink events are algedonic telemetry, not a reason to skip the
        # deterministic Chainlink/git state transition.
        pass
    try:
        if args.reattach:
            result = run_worklink_reattach(home=home, repo=repo, issue_id=args.issue_id)
        else:
            result = run_worklink(
                home=home,
                repo=repo,
                issue_id=args.issue_id,
                backend=args.backend,
                dry_run=args.dry_run,
                test_command=args.test_command,
                base_branch=args.base,
                autonomous=args.autonomous,
            )
    except LeafValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorklinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.dry_run:
        return 0
    if result.status == "refused":
        # Autonomy policy declined this run (e.g. unsandboxed compute without
        # opt-in). Not a failure of the work — surface the reason and exit 1 so
        # an autonomous caller treats it as "did not run".
        print(f"worklink #{result.issue_id}: refused — {result.reason}", file=sys.stderr)
        return 1
    print(
        f"worklink #{result.issue_id} attempt {result.attempt}: {result.status}"
        + (" review-ready" if result.review_ready else "")
        + (f" PR {result.pr_url}" if result.pr_url else "")
        + (f" — {result.reason}" if result.reason else "")
    )
    if result.evidence_path:
        print(f"evidence: {result.evidence_path}")
    return 0 if result.status in {"completed", "blocked"} else 1


def _worker_reason_suffix(validation: object) -> str:
    reasons = tuple(str(item) for item in getattr(validation, "reasons", ()) if item)
    evidence = getattr(validation, "evidence", None)
    blocked_reason = getattr(evidence, "blocked_reason", None) if evidence is not None else None
    if blocked_reason:
        return f" — {blocked_reason}"
    if reasons:
        return " — " + ", ".join(reasons)
    return ""
