#!/usr/bin/env python3
"""Opt-in DockerSibling Worklink smoke runner.

This script exercises the real agent-side DockerSibling path against a running
broker: broker client -> worker container -> pushed attempt branch ->
orchestrator remote evidence re-derivation -> PR creation. It is intentionally
not part of the default test suite because it requires Docker/OrbStack-family
host plumbing, a running broker, GitHub credentials, and a sacrificial Chainlink
leaf issue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.orchestrator import WorklinkRunner

_OPT_IN_ENV = "MIMIR_WORKLINK_DOCKER_SMOKE"


def build_registry(*, broker_url: str, image: str, network: str) -> BackendRegistry:
    """Return a Worklink registry that routes work through DockerSibling."""

    config = WorklinkConfig(
        defaults=WorklinkDefaults(backend="codex", compute_backend="docker_sibling"),
        # Configure codex like a real worklink.yaml: the worker container IS the
        # sandbox, so codex needs danger-full-access to write files + run git
        # non-interactively. Without it codex runs read-only/approval-gated,
        # produces no diff, and the run fails not-review-ready (no push).
        backend_settings={
            "codex": {"bin": "codex", "args": ["exec", "--json", "--sandbox", "danger-full-access"]}
        },
        compute_backend_settings={
            "docker_sibling": {
                "broker_url": broker_url,
                "image": image,
                "policy": {"network": network},
            }
        },
    )
    return BackendRegistry(config)


def preflight_broker_url(broker_url: str) -> None:
    """Fail fast for broker URL shapes that cannot work from this process."""

    parsed = urlparse(broker_url)
    if parsed.scheme not in {"unix", "http", "https"}:
        raise SystemExit("broker URL must use unix://, http://, or https://")
    if parsed.scheme == "unix" and not Path(parsed.path).exists():
        raise SystemExit(
            f"broker socket does not exist: {parsed.path}\n"
            "Start `mimir worklink docker-broker --policy <policy> --socket <path>` "
            "outside the agent container first, or pass --broker-url."
        )


def validate_smoke_evidence(evidence_path: Path) -> dict[str, object]:
    """Check that the orchestrator, not the worker, observed remote evidence."""

    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    commands = data.get("commands") or []
    command_text = "\n".join(str(command.get("cmd", "")) for command in commands if isinstance(command, dict))
    failures: list[str] = []
    if data.get("status") != "completed":
        failures.append(f"status={data.get('status')!r}")
    if not data.get("diff_observed", False):
        failures.append("diff_observed is false")
    if not data.get("files_changed"):
        failures.append("files_changed is empty")
    if "git fetch origin" not in command_text:
        failures.append("remote evidence did not record git fetch")
    if "origin/" not in command_text:
        failures.append("remote evidence did not diff origin/* refs")
    if failures:
        raise SystemExit("smoke evidence failed validation: " + "; ".join(failures))
    return data


async def run_smoke(args: argparse.Namespace) -> int:
    if not args.force and os.environ.get(_OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Refusing to run destructive smoke without {_OPT_IN_ENV}=1. "
            "This run may push an attempt branch and open a PR."
        )
    preflight_broker_url(args.broker_url)
    registry = build_registry(broker_url=args.broker_url, image=args.image, network=args.network)
    result = await WorklinkRunner(home=args.home, repo=args.repo, registry=registry).run(
        args.issue_id,
        backend_name=args.backend,
        test_command=args.test_command,
        base_branch=args.base,
    )
    print(
        f"worklink docker-sibling smoke issue={result.issue_id} "
        f"attempt={result.attempt} status={result.status} review_ready={result.review_ready}"
    )
    if result.pr_url:
        print(f"pr: {result.pr_url}")
    if result.evidence_path:
        print(f"evidence: {result.evidence_path}")
    if not result.review_ready or result.evidence_path is None:
        if result.reason:
            print(f"reason: {result.reason}", file=sys.stderr)
        return 1
    validate_smoke_evidence(result.evidence_path)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("issue_id", type=int, help="Sacrificial Worklink-ready Chainlink leaf issue.")
    p.add_argument("--home", type=Path, default=Path(os.environ.get("MIMIR_HOME", "/mimir-home")))
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--broker-url", default=os.environ.get("WORKLINK_DOCKER_BROKER_URL", "unix:///run/worklink-broker.sock"))
    p.add_argument("--image", default=os.environ.get("WORKLINK_DOCKER_IMAGE", "mimir-worklink:latest"))
    p.add_argument("--network", default=os.environ.get("WORKLINK_DOCKER_NETWORK", "none"))
    p.add_argument("--backend", default="codex")
    p.add_argument("--base", default=None, help="Optional PR base branch override.")
    p.add_argument("--test-command", default="env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_worklink_cli.py --tb=short")
    p.add_argument("--force", action="store_true", help=f"Bypass the {_OPT_IN_ENV}=1 guard.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.home = args.home.resolve()
    args.repo = args.repo.resolve()
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    raise SystemExit(main())
