from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..models import RetainedFactoryScope
from .dispatch_failures import (
    POLLER_NAME,
    current_failure_record,
    dispatch_failure_state_dir,
    is_dispatch_failure_intervention,
)
from .factory_state import (
    RETAINED_CONTROLLER_PHASES,
    FactoryRunRecord,
    factory_checkout_interlock,
    factory_issue_resource_lock,
    factory_process_is_alive,
    factory_process_is_verified_dead,
    load_factory_records_for_issue,
)
from .worker_client import factory_checkout_for_path


@dataclass(frozen=True)
class RetainedFactoryScopeResolution:
    scope: RetainedFactoryScope | None = None
    refusal_reason: str | None = None


def _record_matches_scope(record: FactoryRunRecord, scope: RetainedFactoryScope) -> bool:
    return (
        record.issue_id == scope.issue_id
        and record.run_id == scope.run_id
        and record.attempt == scope.attempt
        and record.session == scope.session
        and record.repository == scope.repository
        and record.branch == scope.branch
        and record.sandbox == scope.sandbox
    )


def derive_retained_factory_scope(
    event: Any, service: Any, *, home: Path | None = None,
) -> RetainedFactoryScopeResolution:
    """Mint exact retained authority from one trusted current incident."""
    if (
        getattr(service, "canonical", None) != "poller:worklink-ready-queue"
        or getattr(service, "trigger", None) != "poller"
        or getattr(service, "authority_profile", None) != "github"
        or getattr(event, "service_principal", None) != getattr(service, "canonical", None)
    ):
        return RetainedFactoryScopeResolution(
            refusal_reason="service is not the Worklink ready queue"
        )
    if not is_dispatch_failure_intervention(event):
        return RetainedFactoryScopeResolution(
            refusal_reason="ready-queue prompt is not an incident"
        )
    extra = event.extra
    item: Mapping[str, Any] = extra["items"][0]
    issue_id = item["issue_id"]
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if home is None and not home_value:
        return RetainedFactoryScopeResolution(refusal_reason="MIMIR_HOME is unavailable")
    root = home if home is not None else Path(home_value)
    try:
        incident = current_failure_record(dispatch_failure_state_dir(root), issue_id)
    except (OSError, ValueError) as exc:
        return RetainedFactoryScopeResolution(refusal_reason=str(exc))
    if incident is None or (
        incident["signature"] != item["error_signature"]
        or incident["occurrence_id"] != item["failure_occurrence_id"]
    ):
        return RetainedFactoryScopeResolution(refusal_reason="incident occurrence is stale")
    run_id = incident.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return RetainedFactoryScopeResolution(refusal_reason="incident is not a retained factory run")
    incident_attempt = incident.get("attempt")
    work_path = incident.get("work_path")
    if (
        not isinstance(incident_attempt, int)
        or isinstance(incident_attempt, bool)
        or incident_attempt < 1
        or not isinstance(work_path, str)
        or not work_path
    ):
        return RetainedFactoryScopeResolution(refusal_reason="incident factory target is invalid")
    try:
        records = load_factory_records_for_issue(root, issue_id)
    except Exception as exc:
        return RetainedFactoryScopeResolution(refusal_reason=f"retained factory record unavailable: {exc}")
    if not records:
        return RetainedFactoryScopeResolution(refusal_reason="retained factory record is missing")
    if len(records) != 1:
        return RetainedFactoryScopeResolution(refusal_reason="retained factory record is ambiguous")
    record = records[0]
    if (
        record.run_id != run_id
        or record.attempt != incident_attempt
        or record.sandbox != work_path
    ):
        return RetainedFactoryScopeResolution(refusal_reason="retained factory target was replaced")
    if record.controller_phase not in RETAINED_CONTROLLER_PHASES | {"running"}:
        return RetainedFactoryScopeResolution(refusal_reason="factory run is not retained")
    if not record.session:
        return RetainedFactoryScopeResolution(refusal_reason="retained factory session is unavailable")
    sandbox = Path(record.sandbox)
    binding = factory_checkout_for_path(sandbox)
    try:
        resolved = sandbox.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved = None
    if (
        binding is None
        or binding[1:] != (record.issue_id, record.attempt)
        or resolved != sandbox
        or sandbox.parent.name != ".factory-sandboxes"
        or sandbox.name != record.run_id
        or not sandbox.is_dir()
    ):
        return RetainedFactoryScopeResolution(refusal_reason="retained factory sandbox is unsafe")
    if factory_process_is_alive(record):
        return RetainedFactoryScopeResolution(refusal_reason="retained factory process is alive")
    if not factory_process_is_verified_dead(record):
        return RetainedFactoryScopeResolution(
            refusal_reason="retained factory process death cannot be verified"
        )
    return RetainedFactoryScopeResolution(scope=RetainedFactoryScope(
        issue_id=issue_id,
        signature=incident["signature"],
        occurrence_id=incident["occurrence_id"],
        run_id=record.run_id,
        attempt=record.attempt,
        session=record.session,
        repository=record.repository,
        branch=record.branch,
        sandbox=record.sandbox,
    ))


def _factory_session_lock_is_fresh(record: FactoryRunRecord) -> bool:
    from .backends.feature_factory import FeatureFactoryBackend

    status = FeatureFactoryBackend(entrypoint=record.launcher).status(
        record.run_id, sandbox=Path(record.sandbox), launcher=record.launcher,
    )
    if (
        not status.valid
        or status.run_id != record.run_id
        or status.sandbox_path != record.sandbox
        or status.lock not in {"absent", "stale", "fresh"}
        or status.dead_lock is None
        or status.lock_session not in {None, record.session}
        or (status.lock == "absent" and status.lock_session is not None)
        or (status.lock in {"stale", "fresh"} and status.lock_session != record.session)
    ):
        raise RuntimeError("factory status identity changed")
    return status.lock == "fresh"


@contextmanager
def retained_factory_effect_lease(
    home: Path, scope: RetainedFactoryScope,
) -> Iterator[RetainedFactoryScopeResolution]:
    """Lease and revalidate one retained effect for its complete operation."""
    with factory_checkout_interlock(home) as checkout_acquired:
        if not checkout_acquired:
            yield RetainedFactoryScopeResolution(
                refusal_reason="factory checkout interlock unavailable"
            )
            return
        with factory_issue_resource_lock(home, scope.issue_id) as resource_acquired:
            if not resource_acquired:
                yield RetainedFactoryScopeResolution(
                    refusal_reason="factory issue resource lock unavailable"
                )
                return
            try:
                incident = current_failure_record(
                    dispatch_failure_state_dir(home), scope.issue_id,
                )
                if incident is None or (
                    incident["signature"] != scope.signature
                    or incident["occurrence_id"] != scope.occurrence_id
                ):
                    raise RuntimeError("incident occurrence is stale")
                records = load_factory_records_for_issue(home, scope.issue_id)
                if len(records) != 1 or not _record_matches_scope(records[0], scope):
                    raise RuntimeError("retained factory target was replaced")
                record = records[0]
                if factory_process_is_alive(record):
                    raise RuntimeError("retained factory process is alive")
                if not factory_process_is_verified_dead(record):
                    raise RuntimeError("retained factory process death cannot be verified")
                if _factory_session_lock_is_fresh(record):
                    raise RuntimeError("factory session lock is fresh")
            except Exception as exc:
                yield RetainedFactoryScopeResolution(refusal_reason=str(exc))
                return
            yield RetainedFactoryScopeResolution(scope=scope)
