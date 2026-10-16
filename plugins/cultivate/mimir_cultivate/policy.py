"""Cultivate's owner-bound repo and pull-request action vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


class RepoPRAction(StrEnum):
    INSPECT = "repo.inspect"
    CHECKOUT = "repo.checkout"
    TEST = "repo.test"
    WRITE = "repo.write"
    COMMIT = "repo.commit"
    PUSH = "repo.push"
    PR_COMMENT = "pr.comment"
    PR_EDIT = "pr.edit"
    PR_REREQUEST = "pr.rerequest"
    PR_REVIEW = "pr.review"


def register_action_protocol(registrar: PolicyRegistrar, validate_scope: Callable[..., bool]) -> None:
    """Bind the vocabulary and grant validator to this selected registrar."""
    registrar.register_scoped_action(
        RepoPRAction, validate_scope, checkout_action=RepoPRAction.CHECKOUT,
        live_lease=_live_scoped_pr_lease,
    )


def _live_scoped_pr_lease(scope: object, review_state: object) -> object | None:
    from mimir.access_control import _observed_checkout_state
    from mimir.pr_checkout_lease import active_pr_checkout_lease_for_path

    attached = getattr(review_state, "checkout_lease", None)
    if attached is None or not getattr(attached, "is_active", False):
        return None
    live = active_pr_checkout_lease_for_path(attached.path)
    if live is None or any(getattr(live, field, None) != getattr(attached, field, None)
                           for field in ("path", "recovery_id", "scope_id", "owner",
                                         "head_sha", "canonical_origin", "pr_number")):
        return None
    if (live.scope_id != scope.scope_id or live.owner != scope.principal
            or live.head_sha.lower() != scope.observed_head_sha.lower()
            or live.canonical_repo.lower() != scope.canonical_repo.lower()
            or getattr(review_state, "action_scope", None) is not scope):
        return None
    if _observed_checkout_state(live.path) != (
        scope.head_ref, getattr(review_state, "git_expected_head", None)
    ):
        return None
    return live


def validate_scoped_pr_action(action: RepoPRAction, scope: object, grant: object) -> bool:
    """Validate the selected owner's action against host-observed authority."""
    return (
        type(action) is RepoPRAction
        and action.value in getattr(scope, "allowed_operations", ())
        and getattr(grant, "scope_id", None) == getattr(scope, "scope_id", None)
        and getattr(grant, "owner", None) == "cultivate"
        and getattr(grant, "action", None) == action.value
    )
