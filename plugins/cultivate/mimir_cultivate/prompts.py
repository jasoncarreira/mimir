"""Selected Cultivate prompt contributions."""

from __future__ import annotations

import re
from pathlib import Path

from mimir.models import AuthContext, TurnInteractivity
from mimir.prompt_safety import sanitize_prompt_field

from .worklink.factory_state import list_factory_records
from .policy import RepoPRActionScope, RepoPRScopeProvenance


_PR_URL = re.compile(
    r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/[1-9][0-9]*"
)


def coding_status_applies(auth: AuthContext, event: object) -> bool:
    """Keep coding telemetry out of automated, service and ordinary user turns."""
    return (
        isinstance(auth, AuthContext)
        and auth.interactivity == TurnInteractivity.INTERACTIVE
        and not auth.is_service
        and "admin" in auth.roles
        and auth.trigger == "user_message"
        and getattr(event, "trigger", None) == "user_message"
    )


def render_coding_status(auth: AuthContext, event: object, reader: object) -> str:
    """Read only owner-checked durable controller records, never an open-PR list."""
    if not coding_status_applies(auth, event):
        return ""
    home = getattr(reader, "home", None)
    if not isinstance(home, Path) or not home.is_absolute():
        return ""
    listing = list_factory_records(home)
    lines: list[str] = []
    for record in listing.records:
        status = record.status
        if status is None or not status.valid:
            continue
        if record.controller_phase == "running" and status.status == "running":
            lines.append(f"- Worklink build in flight (attempt {record.attempt})")
        elif (record.controller_phase == "parked" and status.status == "needs-human"
              and status.pr_url and (match := _PR_URL.fullmatch(status.pr_url))
              and match.group("repo") == record.repository
              and _remediation_scope_matches(auth, record.repository, int(status.pr_url.rsplit("/", 1)[-1]))):
            lines.append(
                "- PR under remediation: "
                f"{sanitize_prompt_field(status.pr_url)}"
            )
    if not lines:
        return ""
    return "Coding status\n" + "\n".join(lines[:20])


def _remediation_scope_matches(auth: AuthContext, repository: str, number: int) -> bool:
    store = auth.server_discovered_pr_scope_store
    scope = store.resolve(repository, number) if store is not None else None
    return (
        isinstance(scope, RepoPRActionScope)
        and scope.owner == "cultivate"
        and scope.provenance == RepoPRScopeProvenance.SERVER_DISCOVERED
        and scope.event_type in {"pr_changes_requested_stale", "pr_ci_failure"}
        and scope.canonical_repo.lower() == repository.lower()
        and scope.pr_number == number
    )


def register_prompt_provider(registrar: object) -> None:
    registrar.register_prompt_provider(
        "coding_status", "self_state", 4096, coding_status_applies, render_coding_status,
    )
    registrar.register_prompt_provider(
        "coding_review_guidance", "feedback", 1024, coding_review_applies,
        render_coding_review_guidance, static=True,
    )


def coding_review_applies(auth: AuthContext, event: object) -> bool:
    return (
        isinstance(auth, AuthContext) and auth.is_service
        and auth.trigger == "poller"
        and getattr(event, "trigger", None) == "poller"
        and getattr(event, "channel_id", None) == "poller:github-activity"
    )


def render_coding_review_guidance(auth: AuthContext, event: object, reader: object) -> str:
    if not coding_review_applies(auth, event):
        return ""
    return (
        "Complete review work in this turn. Do not call ``approve_declassification`` "
        "(a taint-gated poller cannot approve its own release) or any spawn tool "
        "(review authority does not delegate into a coding process); neither "
        "capability is available. To read the spec of record for the PR you are "
        "remediating, call `pr_spec` only if that capability is listed for this turn."
    )
