"""The protocol every tracker adapter implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..thread import TrackerIssue

REQUEST_TIMEOUT_SECONDS = 10.0

ActionStatus = Literal["posted", "applied", "already", "unavailable", "unsupported", "refused"]


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a write; ``reason`` is a fixed token, never tracker text."""

    status: ActionStatus
    reason: str = ""


class TrackerAdapter(Protocol):
    """One tracker kind (``github_issues``, ``linear`` or ``jira``).

    Network failures raise ``TrackerUnavailable``. ``trust`` returns None when the
    actor's standing cannot be established, which the delivery rule treats as
    "retry later", never as trusted.
    """

    def list_assigned(self) -> tuple[list[TrackerIssue], bool]:
        """Return issues assigned to the bot, newest-updated first, and whether the listing is complete."""
        ...

    def read(self, ref: str) -> TrackerIssue: ...

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None: ...

    def comment(self, ref: str, body: str) -> ActionResult: ...

    def transition(self, ref: str, state: str) -> ActionResult: ...

    def close(self, ref: str) -> ActionResult: ...

    def current_state(self, ref: str) -> str | None: ...
