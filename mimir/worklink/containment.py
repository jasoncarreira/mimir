"""Run Worklink build steps under an identity that cannot write the agent home.

Why this exists
---------------
A Worklink build executes repository-controlled code. Until this module, every
such step ran as the agent's own OS user, which can write ``<home>`` — including
``scheduler.yaml`` and ``skills/*/pollers.json``, the files that grant shell
authority. A build could append a ``shell_commands`` block granting itself any
binary and the next scheduled tick would honour it.

Containing only the build process is not enough. The evidence gate runs the
configured test command against the worker-mutated checkout, and the controller
then runs Git over that same checkout, so a planted test payload or ``.git`` hook
regains agent-uid execution. Every step influenced by the checkout has to run
contained, which is why this is a shared helper rather than a change inside one
call site.

Why a broker
------------
The agent process runs with ``CapEff=0`` and cannot switch to a sibling uid --
``setpriv --reuid`` returns ``Operation not permitted`` -- and unprivileged user
namespaces are refused by the container's seccomp profile. So privilege must be
dropped by something that already holds it. A root-supervised broker does that.

The broker can ONLY drop privilege: it execs as the contained identity and
nothing else, so a caller cannot ask it to run anything with more authority than
the caller already had. That is what makes it safe to expose to the agent uid.

Fail closed
-----------
An autonomous dispatch whose containment cannot be established does not run.
"Warned and continued" and "verified" must never look alike in the logs, so the
override path is a distinct, explicitly logged decision rather than a fallback.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ContainmentUnavailable",
    "containment_required",
    "ContainmentPolicy",
    "contained_argv",
    "resolve_containment",
]

#: Socket the root-supervised broker listens on. Root-owned; the agent uid may
#: connect but cannot replace it.
DEFAULT_BROKER_SOCKET = Path("/run/worklink/broker.sock")

#: The identity build steps run as. Distinct from the agent user, and created by
#: the shipped image with no write access to the agent home.
DEFAULT_CONTAINED_USER = "worklink"


class ContainmentUnavailable(RuntimeError):
    """Containment could not be established, so the caller must not proceed.

    Raised rather than returned so a caller cannot accidentally treat the
    uncontained path as a fallback.
    """


@dataclass(frozen=True)
class ContainmentPolicy:
    """How build steps are contained, resolved once per dispatch."""

    user: str
    broker_socket: Path
    launcher: tuple[str, ...]
    #: True only when every requirement was verified, never when merely assumed.
    verified: bool
    #: Set when the operator explicitly accepted running uncontained. Distinct
    #: from ``verified`` so the two are not conflatable downstream.
    override_reason: str | None = None
    #: Set when this deployment runs no coding tools, so there is nothing to
    #: contain. A THIRD state on purpose: "verified", "bypassed" and "not
    #: applicable" have different meanings to anyone reading a log, and
    #: collapsing them into one boolean is how a bypass comes to look like a
    #: pass.
    not_required_reason: str | None = None


def containment_required() -> bool:
    """Whether this deployment needs Worklink containment at all.

    Gated on ``MIMIR_CODING_ENABLED``. A deployment that exposes no coding tools
    never runs a Worklink build, so there is no repository-controlled code to
    contain and no service to supervise. Requiring the broker there would fail
    closed on a risk that does not exist, and an operator would reasonably read
    that as the feature being broken.

    Deliberately reads the same variable and truthy set as
    ``access_control._service_shell_coding_enabled`` rather than importing it,
    because ``config`` imports ``access_control`` and the reverse would cycle.
    """
    raw = os.environ.get("MIMIR_CODING_ENABLED")
    return bool(raw and raw.strip().lower() in {"1", "true", "yes", "on", "y"})


def _looks_like_agent_home(path: Path) -> Path | None:
    """Return the agent home if one is configured, else ``None``."""
    raw = os.environ.get("MIMIR_HOME", "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def resolve_containment(
    *,
    user: str | None = None,
    broker_socket: Path | None = None,
    allow_uncontained: str | None = None,
) -> ContainmentPolicy:
    """Resolve and VERIFY the containment policy, or raise.

    ``allow_uncontained`` is an operator override carrying its own reason. It is
    never inferred: a missing broker is an error, not a licence to proceed.
    """
    contained_user = user or os.environ.get(
        "MIMIR_WORKLINK_USER", DEFAULT_CONTAINED_USER,
    )
    socket_path = broker_socket or Path(
        os.environ.get("MIMIR_WORKLINK_BROKER_SOCKET", str(DEFAULT_BROKER_SOCKET)),
    )

    if not containment_required():
        # No coding tools, so no build, so nothing to contain. Distinct from an
        # override: nothing was bypassed, the risk is absent.
        return ContainmentPolicy(
            user=contained_user,
            broker_socket=socket_path,
            launcher=(),
            verified=False,
            not_required_reason="MIMIR_CODING_ENABLED is not set",
        )

    if allow_uncontained:
        # An acknowledged bypass. Recorded as such so a log reader can tell it
        # apart from a passing verification -- the distinction the review asked
        # for, and the reason this is not simply a boolean.
        return ContainmentPolicy(
            user=contained_user,
            broker_socket=socket_path,
            launcher=(),
            verified=False,
            override_reason=allow_uncontained,
        )

    launcher = shutil.which("s6-setuidgid") or "/package/admin/s6/command/s6-setuidgid"
    if not Path(launcher).exists():
        raise ContainmentUnavailable(
            f"no privilege-dropping launcher found (looked for {launcher!r}); "
            "the agent process cannot switch uid on its own (CapEff=0), so a "
            "build cannot be contained on this deployment",
        )
    if not socket_path.exists():
        raise ContainmentUnavailable(
            f"the worklink broker socket {socket_path} does not exist; the "
            "root-supervised broker is what drops privilege, and without it a "
            "build would run as the agent user",
        )
    return ContainmentPolicy(
        user=contained_user,
        broker_socket=socket_path,
        launcher=(launcher, contained_user),
        verified=True,
    )


def contained_argv(
    policy: ContainmentPolicy, command: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Return the argv to hand the BROKER, not one for this process to exec.

    The agent process cannot drop privilege itself. Verified on the live
    deployment::

        as mimir:  s6-applyuidgid: fatal: unable to set supplementary group
                   list: Operation not permitted
        as root:   1002

    so prefixing ``s6-setuidgid worklink`` and exec'ing it here would fail every
    time. The privilege drop happens inside the broker, which already runs as
    root; this function only assembles the request. Callers must send the result
    to the broker rather than spawning it.
    """
    argv = tuple(str(part) for part in command)
    if not argv:
        raise ValueError("contained_argv requires a non-empty command")
    if policy.override_reason is not None or policy.not_required_reason is not None:
        return argv
    return (*policy.launcher, *argv)


def agent_can_drop_privilege() -> bool:
    """True only if THIS process could switch uid on its own.

    Exists so the impossibility is asserted rather than assumed. It is false on
    the shipped deployment, which is the whole reason a broker is required; a
    deployment where it is true has a different and larger problem.
    """
    try:
        return os.getuid() == 0
    except AttributeError:  # pragma: no cover - non-POSIX
        return False
