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
    from mimir.access_control import _live_scoped_pr_lease

    registrar.register_scoped_action(
        RepoPRAction, validate_scope, checkout_action=RepoPRAction.CHECKOUT,
        live_lease=_live_scoped_pr_lease,
    )


def validate_scoped_pr_action(action: RepoPRAction, scope: object, grant: object) -> bool:
    """Validate the selected owner's action against host-observed authority."""
    return (
        type(action) is RepoPRAction
        and action.value in getattr(scope, "allowed_operations", ())
        and getattr(grant, "scope_id", None) == getattr(scope, "scope_id", None)
        and getattr(grant, "owner", None) == "cultivate"
        and getattr(grant, "action", None) == action.value
    )
