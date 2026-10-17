"""Register Cultivate contributions when the provider is selected."""

from __future__ import annotations

from typing import TYPE_CHECKING
import os
from pathlib import Path
import stat

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


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
        from .policy import register_action_protocol, validate_scoped_pr_action
        from .forge import github_identity_is_degraded
        from .worklink.continuation import maybe_create_worklink_budget_continuation
        from .worklink.dispatch_failures import is_dispatch_failure_intervention

        register_action_protocol(registrar, validate_scoped_pr_action)
        registrar.register_retained_checkout_operation(_retained_checkout_operation)
        registrar.register_turn_hook("tool_readiness", lambda: not github_identity_is_degraded())
        registrar.register_turn_hook("failure_intervention", is_dispatch_failure_intervention)
        registrar.register_turn_hook("budget_continuation", maybe_create_worklink_budget_continuation)
        from . import server

        registrar.register_server_hook("capture_source", server.capture_controller_source_commit)
        registrar.register_server_hook("health", server.executor_health)
        registrar.register_server_hook("process_identity", server.process_identity)
        registrar.register_server_hook("repository_alerts", server.repository_binding_alerts)
        registrar.register_server_hook("channel_drained", server.channel_drained)
        registrar.register_server_hook("workspace_hooks", server.install_workspace_hooks)
        registrar.register_server_hook("configure_jobs", server.configure_worklink)
        registrar.register_server_hook("reconcile_startup", server.reconcile_startup)
        registrar.register_server_hook("validate_config", server.validate_worklink)
        registrar.register_server_hook("shutdown_claims", server.release_claims)


provider = CultivateProvider()
