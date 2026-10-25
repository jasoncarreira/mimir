"""Register Cultivate contributions when the provider is selected."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from mimir.models import AuthContext
from .policy import RetainedFactoryScope

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


@tool
async def worklink_run(issue_id: int, backend: str | None = None) -> str:
    """Dispatch a ready Chainlink leaf through Cultivate's isolated Worklink runner."""
    from mimir.tools import registry as core_tools
    from .worklink.autonomy import check_concurrency, worklink_priority, worklink_repo
    from .worklink.orchestrator import run_worklink

    home_value = os.environ.get("MIMIR_HOME")
    if not home_value:
        raise ToolException("worklink_run failed: MIMIR_HOME not set")
    home = Path(home_value)
    try:
        repo = Path(worklink_repo())
    except Exception as exc:
        raise ToolException(f"worklink_run failed: {exc}") from exc
    arbiter = core_tools._STATE.get("arbiter")
    if arbiter is not None:
        try:
            priority = worklink_priority(home)
        except Exception:
            priority = "normal"
        try:
            decision = await asyncio.to_thread(
                arbiter.should_fire, priority=priority, event_loop=asyncio.get_running_loop(),
            )
        except Exception as exc:
            log.warning("worklink_run arbiter check failed: %s", exc)
            decision = None
        if decision is not None and not decision.fire:
            raise ToolException(
                f"worklink_run shed: resource pressure {decision.severity.name} "
                f"(priority={decision.priority}) — {decision.reason}. "
                "Try again later, or ask the operator to run `mimir worklink run`."
            )
    try:
        concurrency = check_concurrency(home)
    except Exception as exc:
        raise ToolException(f"worklink_run failed: concurrency check error: {exc}") from exc
    if not concurrency.allowed:
        raise ToolException(f"worklink_run skipped: {concurrency.reason} — try again when a slot frees.")
    try:
        result = await asyncio.to_thread(
            run_worklink, home=home, repo=repo, issue_id=int(issue_id),
            backend=backend, autonomous=True,
        )
    except Exception as exc:
        raise ToolException(f"worklink_run failed: {exc}") from exc
    parts = [f"worklink_run #{result.issue_id}: {result.status}"]
    if result.attempt is not None:
        parts.append(f"attempt={result.attempt}")
    if result.review_ready:
        parts.append("review-ready")
    if result.pr_url:
        parts.append(f"PR {result.pr_url}")
    if result.evidence_path:
        parts.append(f"evidence={result.evidence_path}")
    if result.reason:
        parts.append(f"reason={result.reason}")
    content = " ".join(parts)
    if result.status in {"blocked", "failed"}:
        raise ToolException(content)
    return content


worklink_run.handle_tool_error = True


@tool
async def worklink_resume(runtime: ToolRuntime[AuthContext] = None) -> str:
    """Resume only the server-bound retained factory incident for this turn."""
    from mimir.tools import registry as core_tools
    from .worklink.autonomy import factory_max_concurrent, make_claims, worklink_priority, worklink_repo
    from .worklink.claims import WORKLINK_EPIC_LABEL
    from .worklink.detached_dispatch import FactoryRecoveryIdentity, launch_detached_worklink
    from .worklink.dispatch_failures import current_failure_record, dispatch_failure_state_dir
    from .worklink.factory_state import (
        factory_process_is_alive, factory_process_is_verified_dead, load_factory_records_for_issue,
    )

    context = getattr(runtime, "context", None)
    scope = getattr(context, "retained_factory_scope", None)
    if not isinstance(scope, RetainedFactoryScope):
        reason = getattr(context, "retained_factory_scope_refusal", None)
        return ("worklink_resume refused: "
                f"{reason or 'this turn has no exact retained factory incident'}; "
                "leaf incidents must be diagnosed and reported to the operator")
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if not home_value:
        return "worklink_resume refused: MIMIR_HOME not set"
    home = Path(home_value)
    try:
        repo = Path(worklink_repo())
    except Exception as exc:
        return f"worklink_resume refused: {exc}"
    arbiter = core_tools._STATE.get("arbiter")
    if arbiter is not None:
        try:
            priority = worklink_priority(home)
        except Exception:
            priority = "normal"
        try:
            decision = await asyncio.to_thread(
                arbiter.should_fire, priority=priority, event_loop=asyncio.get_running_loop(),
            )
        except Exception as exc:
            log.warning("worklink_resume arbiter check failed: %s", exc)
            decision = None
        if decision is not None and not decision.fire:
            return f"worklink_resume shed: resource pressure {decision.severity.name}: {decision.reason}"
    try:
        incident = current_failure_record(dispatch_failure_state_dir(home), scope.issue_id)
        records = load_factory_records_for_issue(home, scope.issue_id)
        if incident is None or (incident["signature"] != scope.signature
                                or incident["occurrence_id"] != scope.occurrence_id):
            return "worklink_resume refused: incident occurrence is not current"
        if len(records) != 1:
            return ("worklink_resume refused: retained factory record is missing" if not records
                    else "worklink_resume refused: retained factory record is ambiguous")
        record = records[0]
        if (record.run_id != scope.run_id or record.attempt != scope.attempt
                or record.session != scope.session or record.repository != scope.repository
                or record.branch != scope.branch or record.sandbox != scope.sandbox
                or incident.get("run_id") != scope.run_id
                or incident.get("attempt") != scope.attempt
                or incident.get("work_path") != scope.sandbox):
            return "worklink_resume refused: retained factory target was replaced"
        if record.status is None or record.status.status != "needs-human":
            return "worklink_resume refused: retained factory run is not needs-human"
        if factory_process_is_alive(record):
            return "worklink_resume refused: retained factory process is alive"
        if not factory_process_is_verified_dead(record):
            return "worklink_resume refused: retained factory process death cannot be verified"
        claims = make_claims(home)
        active_ids = claims._active_worklink_lock_ids_for_scope(label=WORKLINK_EPIC_LABEL)
        if scope.issue_id in active_ids:
            return "worklink_resume refused: another run for this issue is live"
        cap = factory_max_concurrent()
        if len(active_ids) >= cap:
            return f"worklink_resume refused: factory concurrency cap reached ({len(active_ids)}/{cap})"
    except Exception as exc:
        return f"worklink_resume refused: recovery preflight failed: {exc}"
    state_dir = dispatch_failure_state_dir(home)
    try:
        launched = launch_detached_worklink(
            command="run-epic", issue_id=scope.issue_id, home=home, repo=repo,
            state_dir=state_dir,
            run_bin=shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir"),
            recovery=FactoryRecoveryIdentity(
                signature=scope.signature, occurrence_id=scope.occurrence_id,
                run_id=scope.run_id, attempt=scope.attempt, session=scope.session,
            ),
            env_overrides={"STATE_DIR": str(state_dir)},
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"worklink_resume failed: detached recovery dispatch failed: {exc}"
    return (
        f"worklink_resume: recovery dispatched run_id={scope.run_id} "
        f"retained_attempt={scope.attempt} pid={launched.pid} log={launched.log_path}. "
        "The child may still refuse under the claim; only a successful child claim "
        "consumes a new Chainlink claim attempt."
    )


async def _repository_binding_alerts(log_event: object) -> None:
    from .repository import repo_binding_startup_alerts

    for alert in await asyncio.to_thread(repo_binding_startup_alerts):
        await log_event("github_repo_binding_attention_required", **alert)


def _retained_checkout_operation(
    operation: str, file_path: str, absolute: Path, scope: object, **arguments: object,
) -> dict[str, object]:
    from mimir.readonly_backend import _replace_shrink_error, _write_failure_error
    from .worklink.retained_scope import retained_factory_effect_lease
    from .worklink.worker_client import WorkerClient, factory_checkout_for_path

    home_value = os.environ.get("MIMIR_HOME", "").strip()
    binding = factory_checkout_for_path(Path(scope.sandbox))
    if not home_value or binding is None:
        return {"error": "retained factory checkout binding is unavailable"}
    checkout, issue_id, attempt = binding
    with retained_factory_effect_lease(Path(home_value), scope) as lease:
        if lease.scope is None:
            return {"error": lease.refusal_reason or "retained factory lease refused"}
        relative = absolute.relative_to(checkout).as_posix()
        if operation == "replace_file":
            allow_shrink = arguments.pop("allow_shrink", False)
            try:
                metadata = absolute.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    return {"error": f"File '{file_path}' is not a regular file."}
                content = arguments["content"]
                if not isinstance(content, str):
                    return {"error": "factory replace content is invalid"}
                new_bytes = len(content.encode("utf-8"))
            except FileNotFoundError:
                return {"error": "file does not exist"}
            except (OSError, UnicodeEncodeError) as exc:
                return {"error": _write_failure_error(file_path, str(exc))}
            shrink_error = _replace_shrink_error(metadata.st_size, new_bytes, allow_shrink)
            if shrink_error:
                return {"error": shrink_error}
        client = WorkerClient.for_factory_checkout(
            checkout, issue_id=issue_id, attempt=attempt,
        )
        return client.factory_file_operation(
            operation, relative, run_id=scope.run_id, **arguments,
        )


class CultivateProvider:
    def register(self, registrar: PolicyRegistrar) -> None:
        from .policy import (
            CultivateState, register_action_protocol, register_tools, validate_scoped_pr_action,
        )
        from .forge import (
            github_identity_is_degraded, github_identity_recovery_pending,
            initialize_github_forge_identity, set_forge_client,
            set_github_identity_degraded_callback,
        )
        from .worklink.continuation import maybe_create_worklink_budget_continuation
        from .worklink.dispatch_failures import is_dispatch_failure_intervention

        registrar.register_state_slot(CultivateState)
        register_action_protocol(
            registrar, validate_scoped_pr_action, checkout_tool="repo_checkout",
        )
        register_tools(registrar)
        from .prompts import register_prompt_provider

        register_prompt_provider(registrar)
        from .worklink.retained_scope import retained_factory_effect_lease
        from .repository import retained_factory_subprocess_runner

        registrar.register_retained_effect_lease(
            retained_factory_effect_lease, retained_factory_subprocess_runner,
        )
        from .github_trust import probe_github_token

        registrar.register_credential_probe(
            "GITHUB_TOKEN", "A", ("GITHUB_TOKEN",),
            "GitHub PAT (Cultivate identity and git push)", probe_github_token,
        )
        registrar.register_retained_checkout_operation(_retained_checkout_operation)
        registrar.register_turn_hook("tool_readiness", lambda: not github_identity_is_degraded())
        registrar.register_server_hook(
            "github_identity_callback",
            lambda callback: set_github_identity_degraded_callback(
                callback, notify_current=callback is not None,
            ),
        )
        registrar.register_server_hook("github_identity_initialize", initialize_github_forge_identity)
        registrar.register_server_hook("github_identity_recovery_pending", github_identity_recovery_pending)
        registrar.register_server_hook("forge_client_clear", lambda: set_forge_client(None))
        registrar.register_turn_hook("failure_intervention", is_dispatch_failure_intervention)
        registrar.register_turn_hook("budget_continuation", maybe_create_worklink_budget_continuation)
        from .feedback import feedback_extension

        registrar.register_turn_hook("feedback", feedback_extension)
        from . import server

        registrar.register_server_hook("capture_source", server.capture_controller_source_commit)
        registrar.register_server_hook("health", server.executor_health)
        registrar.register_server_hook("process_identity", server.process_identity)
        registrar.register_server_hook("repository_alerts", _repository_binding_alerts)
        registrar.register_server_hook("channel_drained", server.channel_drained)
        registrar.register_server_hook("configure_jobs", server.configure_worklink)
        registrar.register_server_hook("reconcile_startup", server.reconcile_startup)
        registrar.register_server_hook("validate_config", server.validate_worklink)
        registrar.register_server_hook("shutdown_claims", server.release_claims)
        from .scheduler import worklink_poller_hooks

        registrar.register_poller_hook("worklink", worklink_poller_hooks)
        from mimir.plugins.core import PollerProperties

        registrar.register_poller_properties(
            "poller:github-activity", PollerProperties(github_activity=True),
        )
        registrar.register_poller_properties(
            "poller:github-ci-watch", PollerProperties(github_ci_watch=True),
        )
        registrar.register_poller_properties(
            "poller:worklink-ready-queue", PollerProperties(worklink_retained_checkout=True),
        )
        from . import commands

        registrar.register_cli_command("worklink", commands.add_argparse, commands.dispatch_worklink)
        from . import setup

        registrar.register_cli_command("cultivate", setup.add_argparse, setup.dispatch, operator=True)
        registrar.register_cli_command(
            "opencode-bootstrap",
            commands.configure_opencode_bootstrap,
            commands.dispatch_opencode_bootstrap,
        )
        from .board import worklink_evidence
        from .web import register_web

        registrar.register_server_hook("chainlink_board", worklink_evidence)
        register_web(registrar)

    def host_membership_projection(self) -> dict[str, tuple[str, ...]]:
        from .policy import host_membership_projection

        return host_membership_projection()


provider = CultivateProvider()
