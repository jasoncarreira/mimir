"""Run Worklink build steps under an identity that cannot write the agent home.

Why this exists
---------------
A Worklink build has a model generate code and then executes it, so what runs was
reviewed by nobody. Until this module every such step ran as the agent's own OS
user, which can write ``<home>`` -- including ``scheduler.yaml`` and
``skills/*/pollers.json``, the files that grant shell authority. A bad generation
could append a ``shell_commands`` block granting itself any binary, and the next
scheduled tick would honour it.

Threat model
------------
Repository content is TRUSTED: Worklink only builds repositories the operator
configured in ``repositories.yaml``. This module is not defending against a
hostile ``.git/config`` or a malicious test payload planted in the tree. It is
defending against the code the build GENERATES, and against the spec that drove
it, which originates in issue text.

The bar is "strictly better than today", not perfect. Today the build runs as
the agent uid with the entire home writable.

What runs where
---------------
The controller (poller and orchestrator) stays at the agent identity: it needs
``MIMIR_HOME`` for ``worklink.yaml``, lease state and the Chainlink claim locks,
and it holds the GitHub push credential.

CURRENTLY CONTAINED: the build launch only (``compute.py``).

NOT YET CONTAINED, and stated here rather than implied: the evidence gate's test
command and local Git over the checkout still run as the controller, so generated
code still executes at the agent identity during the gate. ``WorkerResult.head_oid``
is observed but not yet consumed by the push. Tracked on chainlink #1164; do not
read this module as covering them.

Why a spool, and why the supervisor stays root
----------------------------------------------
The agent cannot drop privilege: ``CapEff=0``, ``setpriv --reuid`` returns
``Operation not permitted``, unprivileged user namespaces are refused by the
seccomp profile, and ``s6-setuidgid`` run as the agent fails with ``unable to set
supplementary group list``. Something already privileged must do it, and s6 is
PID 1 as root.

So a root-supervised service takes requests from a spool, spawns each as
``worklink``, and publishes what it OBSERVED -- the exit status of the process it
spawned and the commit OID it read afterwards. The supervisor stays root rather
than running as ``worklink`` so that the thing reporting "this build succeeded,
at this commit" is not the same identity as the thing being reported on. That
costs one line of service definition, not a second user.

Spool permissions are where this boundary is silently lost, so they are asserted
rather than assumed:

    requests/   controller-writable, worklink READ-ONLY
    results/    root-published, worklink NO ACCESS

If ``worklink`` could write ``requests/`` it could rewrite its own request; if it
could write ``results/`` it could forge the verdict that gates its own push. Both
would make the root supervisor buy nothing.

Deliberately NOT built (see chainlink #1164): request schema validation, replay
handling, ``O_NOFOLLOW``, cancellation, per-attempt principals, or mediated
provider access. Each defends against a hostile payload or a second trust domain,
neither of which is in scope, and each costs more to maintain than it returns.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ContainmentUnavailable",
    "ContainmentPolicy",
    "WorkerRequest",
    "WorkerResult",
    "containment_required",
    "resolve_containment",
    "submit_request",
    "await_result",
    "run_contained",
    "worker_runtime_env",
    "worker_home",
    "observe_head_via_supervisor",
    "register_attempt_checkout",
    "is_registered_attempt_checkout",
    "spawn_argv",
    "request_dir",
    "result_dir",
]

#: Spool root. Outside the agent home on purpose -- the worker must hold no path
#: under it. On tmpfs, so a request in flight does not survive a restart; a build
#: killed by a restart is re-claimed by the controller, which is the correct
#: outcome anyway.
DEFAULT_SPOOL_ROOT = Path("/run/worklink")

#: The identity build steps run as. Created by the shipped image with a uid
#: distinct from the agent's.
DEFAULT_CONTAINED_USER = "worklink"

#: Where s6 puts its tools in the shipped image, used when ``s6-setuidgid`` is
#: not on PATH (the supervisor runs with a minimal environment).
_S6_SETUIDGID_FALLBACK = "/package/admin/s6/command/s6-setuidgid"

#: Environment variables never projected into a contained step. ``MIMIR_HOME`` is
#: the point of the exercise; the GitHub credentials stay controller-side because
#: push and PR are controller-side operations and the worker has no use for them.
_NEVER_PROJECTED = frozenset(
    {
        "MIMIR_HOME",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
    },
)


class ContainmentUnavailable(RuntimeError):
    """Containment could not be established, so the caller must not proceed.

    Raised rather than returned so a caller cannot accidentally treat the
    uncontained path as a fallback.
    """


@dataclass(frozen=True)
class ContainmentPolicy:
    """How build steps are contained, resolved once per dispatch."""

    user: str
    spool_root: Path
    #: True only when every requirement was verified, never when merely assumed.
    verified: bool
    #: Set when the operator explicitly accepted running uncontained. Distinct
    #: from ``verified`` so the two are not conflatable downstream.
    override_reason: str | None = None
    #: Set when this deployment runs no coding tools, so there is nothing to
    #: contain. A THIRD state on purpose: "verified", "bypassed" and "not
    #: applicable" mean different things to whoever reads a log, and collapsing
    #: them into one boolean is how a bypass comes to look like a pass.
    not_required_reason: str | None = None

    @property
    def contained(self) -> bool:
        """Whether steps actually run under the contained identity."""
        return self.verified

    @property
    def state(self) -> str:
        """The single word to put in an event. Never derived from a boolean."""
        if self.not_required_reason is not None:
            return "not_required"
        if self.override_reason is not None:
            return "override"
        return "verified" if self.verified else "unavailable"


@dataclass(frozen=True)
class WorkerRequest:
    """One step to run under the contained identity.

    ``cwd`` is always an attempt checkout. ``env`` is the FULL environment for
    the step: it is not merged with the controller's, so a home path cannot leak
    in by inheritance.
    """

    attempt_id: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    #: Read the checkout's HEAD after the step and report it. The controller
    #: pushes THAT oid rather than re-reading HEAD later, so a descendant
    #: surviving past the verdict cannot ride along.
    report_head: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "report_head": self.report_head,
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> WorkerRequest:
        return cls(
            attempt_id=str(raw["attempt_id"]),
            argv=tuple(str(a) for a in raw["argv"]),  # type: ignore[union-attr]
            cwd=Path(str(raw["cwd"])),
            env={str(k): str(v) for k, v in dict(raw.get("env") or {}).items()},
            timeout_seconds=(
                float(raw["timeout_seconds"])  # type: ignore[arg-type]
                if raw.get("timeout_seconds") is not None
                else None
            ),
            report_head=bool(raw.get("report_head")),
        )


@dataclass(frozen=True)
class WorkerResult:
    """What the SUPERVISOR observed. Never what the step reported about itself."""

    attempt_id: str
    exit_status: int
    stdout: str
    stderr: str
    #: The commit the supervisor read from the checkout after the step, when
    #: asked. The controller pushes this exact oid.
    head_oid: str | None = None
    timed_out: bool = False
    #: Events the step's wrapper reported, appended BY THE CONTROLLER. The worker
    #: never writes the event log: POSIX write permission is not append-only, so
    #: a buggy build could truncate the record of what it did.
    events: tuple[dict[str, object], ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_status == 0 and not self.timed_out

    def to_json(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "head_oid": self.head_oid,
            "timed_out": self.timed_out,
            "events": list(self.events),
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> WorkerResult:
        return cls(
            attempt_id=str(raw["attempt_id"]),
            exit_status=int(raw["exit_status"]),  # type: ignore[arg-type]
            stdout=str(raw.get("stdout") or ""),
            stderr=str(raw.get("stderr") or ""),
            head_oid=(str(raw["head_oid"]) if raw.get("head_oid") else None),
            timed_out=bool(raw.get("timed_out")),
            events=tuple(dict(e) for e in (raw.get("events") or [])),  # type: ignore[arg-type]
        )


def containment_required() -> bool:
    """Whether this deployment needs Worklink containment at all.

    Gated on ``MIMIR_CODING_ENABLED``. A deployment that exposes no coding tools
    never runs a Worklink build, so there is no generated code to contain and no
    service to supervise. Failing closed there would block on a risk that does
    not exist, and an operator would reasonably read that as a broken feature.

    Deliberately reads the same variable and truthy set as
    ``access_control._service_shell_coding_enabled`` rather than importing it,
    because ``config`` imports ``access_control`` and the reverse would cycle.
    """
    raw = os.environ.get("MIMIR_CODING_ENABLED")
    return bool(raw and raw.strip().lower() in {"1", "true", "yes", "on", "y"})


def request_dir(spool_root: Path) -> Path:
    """Controller-writable, worker read-only."""
    return spool_root / "requests"


def result_dir(spool_root: Path) -> Path:
    """Root-published, worker no access."""
    return spool_root / "results"


def spawn_argv(policy: ContainmentPolicy, argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The argv the SUPERVISOR execs. Only valid in the supervisor, which is root.

    Verified on the live deployment -- the agent cannot exec this itself::

        as mimir:  s6-applyuidgid: fatal: unable to set supplementary group
                   list: Operation not permitted
        as root:   1002

    An earlier revision of this module returned this prefix to the AGENT to exec,
    which fails every time. Its tests passed only because they patched
    ``shutil.which`` and never executed anything.
    """
    parts = tuple(str(a) for a in argv)
    if not parts:
        raise ValueError("spawn_argv requires a non-empty command")
    if not policy.contained:
        return parts
    launcher = shutil.which("s6-setuidgid") or _S6_SETUIDGID_FALLBACK
    return (launcher, policy.user, *parts)


def cancel_path(spool_root: Path, request_id: str) -> Path:
    """Marker the controller publishes to stop an already-claimed step."""
    return request_dir(spool_root) / f"{request_id}.cancel"


def publish_cancellation(policy: ContainmentPolicy, request_id: str) -> None:
    """Ask the supervisor to terminate a running step.

    The step runs as the contained user, so the controller cannot signal it --
    only the root supervisor can. This is the channel for that.
    """
    try:
        cancel_path(policy.spool_root, request_id).touch()
    except OSError:  # pragma: no cover - cancellation is best effort
        pass


#: Controller-derived paths that must be REPLACED, not merely inherited, when a
#: step runs as the contained user. Passing the controller's HOME through gives
#: the worker a directory it cannot write and points every tool at the agent
#: user's config; passing XDG through does the same for caches.
_CONTROLLER_RUNTIME_KEYS = ("HOME", "USER", "LOGNAME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


def worker_runtime_env(policy: ContainmentPolicy, base: dict[str, str]) -> dict[str, str]:
    """Project a base environment onto the contained user's own runtime.

    Removing ``MIMIR_HOME`` and the GitHub tokens is not enough on its own: a
    step still inherits the CONTROLLER's ``HOME``, ``USER`` and ``XDG_*``. That
    is both broken and leaky -- the worker cannot write the agent user's home
    (0700), so a coding CLI has nowhere for its config or caches, and every tool
    that resolves a path from ``HOME`` points at the identity being contained
    from.

    So the worker gets its own home and XDG tree, derived from the account
    itself rather than from configuration that could disagree with reality.
    """
    controller_homes = _controller_home_paths()
    env = {
        key: value
        for key, value in base.items()
        if key not in _NEVER_PROJECTED
        and key not in _CONTROLLER_RUNTIME_KEYS
        # Rewriting HOME/XDG is not enough on its own: variables that name a
        # controller path OUTRIGHT survive it. OPENCODE_CONFIG=/home/mimir/... is
        # the live example -- the worker cannot read it (0700) and it points at
        # the identity being contained from.
        and not _points_into(value, controller_homes)
    }
    home = worker_home(policy)
    if home is None:
        return env
    env["HOME"] = str(home)
    env["USER"] = policy.user
    env["LOGNAME"] = policy.user
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    # A coding CLI resolves its config and OAuth store from these, and the
    # controller's copies are unreadable to the worker (0600 under a 0700 home).
    # Dropping the controller values is necessary but leaves the CLI with no
    # config at all; point them at worker-owned locations instead. The
    # deployment provisions the contents -- see docs/authorization.md.
    env["OPENCODE_CONFIG"] = str(home / ".config" / "opencode" / "opencode.jsonc")
    env["CODEX_HOME"] = str(home / ".codex")
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    return env


def _controller_home_paths() -> tuple[Path, ...]:
    """Directories a contained step must never be pointed at."""
    paths: list[Path] = []
    for raw in (os.environ.get("MIMIR_HOME", ""), os.path.expanduser("~")):
        if not raw or not raw.strip():
            continue
        try:
            paths.append(Path(raw).expanduser().resolve())
        except OSError:  # pragma: no cover
            continue
    return tuple(paths)


def _points_into(value: str, roots: tuple[Path, ...]) -> bool:
    if not value or not value.startswith("/") or not roots:
        return False
    try:
        candidate = Path(value).resolve()
    except OSError:  # pragma: no cover
        return False
    return any(root == candidate or root in candidate.parents for root in roots)


def worker_home(policy: ContainmentPolicy) -> Path | None:
    """The contained user's own home directory, read from the account."""
    try:
        import pwd

        entry = pwd.getpwnam(policy.user)
    except (ImportError, KeyError):
        return None
    return Path(entry.pw_dir) if entry.pw_dir else None


def publish_terminal_result(
    policy: ContainmentPolicy,
    request_id: str,
    *,
    attempt_id: str,
    stderr: str,
    exit_status: int = 124,
) -> None:
    """Publish a result for a step the supervisor will never run.

    Cancelling a request that was never claimed removes the only thing that
    would have produced a result, so without this the waiter blocks for its full
    deadline on a step that is already gone.
    """
    result = WorkerResult(
        attempt_id=attempt_id, exit_status=exit_status, stdout="", stderr=stderr, timed_out=True,
    )
    results = result_dir(policy.spool_root)
    try:
        tmp = results / f".{request_id}.cancelled.tmp"
        tmp.write_text(json.dumps(result.to_json()), encoding="utf-8")
        tmp.rename(results / f"{request_id}.json")
    except OSError:  # pragma: no cover - best effort
        pass


def _contained_user_ids(user: str) -> tuple[int, int] | None:
    try:
        import pwd

        entry = pwd.getpwnam(user)
    except (ImportError, KeyError):
        return None
    return entry.pw_uid, entry.pw_gid


def _verify_spool_directory(path: Path, role: str, contained_gid: int | None) -> None:
    """Assert the contained user cannot WRITE ``path``.

    Checking only the world-write bit is not enough: a directory that is
    group-writable by a group the contained user belongs to is just as open, and
    would have read as verified.
    """
    info = path.stat()
    if info.st_mode & 0o002:
        raise ContainmentUnavailable(
            f"the worklink {role} {path} is world-writable, so the contained "
            "user could rewrite its own request or forge its own result; "
            "refusing to dispatch",
        )
    if contained_gid is not None and info.st_gid == contained_gid and info.st_mode & 0o020:
        raise ContainmentUnavailable(
            f"the worklink {role} {path} is group-writable by the contained "
            f"user's own group (gid {contained_gid}); that is the same exposure "
            "as world-writable, refusing to dispatch",
        )
    # NOT checked here: that ``results/`` is root-owned so the controller itself
    # cannot write it. That property is real and ``prepare_spool`` establishes it
    # (asserted separately), but it cannot be verified in a sandbox where the
    # verifying process IS the owner -- every check would either pass vacuously
    # or refuse every non-root deployment. It is an operator-verified property on
    # chainlink #1164, not one claimed here.


def resolve_containment(
    *,
    user: str | None = None,
    spool_root: Path | None = None,
    allow_uncontained: str | None = None,
) -> ContainmentPolicy:
    """Resolve and VERIFY the containment policy, or raise.

    ``allow_uncontained`` is an operator override carrying its own reason. It is
    never inferred: a missing spool is an error, not a licence to proceed.
    """
    contained_user = user or os.environ.get("MIMIR_WORKLINK_USER", DEFAULT_CONTAINED_USER)
    root = spool_root or Path(
        os.environ.get("MIMIR_WORKLINK_SPOOL", str(DEFAULT_SPOOL_ROOT)),
    )

    if not containment_required():
        # No coding tools, so no build, so nothing to contain. Distinct from an
        # override: nothing was bypassed, the risk is absent.
        return ContainmentPolicy(
            user=contained_user,
            spool_root=root,
            verified=False,
            not_required_reason="MIMIR_CODING_ENABLED is not set",
        )

    if allow_uncontained:
        # An acknowledged bypass, recorded as such so a log reader can tell it
        # apart from a passing verification.
        return ContainmentPolicy(
            user=contained_user,
            spool_root=root,
            verified=False,
            override_reason=allow_uncontained,
        )

    # The identity invariant, checked against the RUNNING process rather than
    # trusting configuration. `MIMIR_WORKLINK_USER=mimir` would otherwise resolve
    # as "verified" while containing nothing at all -- configuration that does
    # not match reality is the assumption this whole leaf exists to replace.
    ids = _contained_user_ids(contained_user)
    if ids is None:
        raise ContainmentUnavailable(
            f"the contained user {contained_user!r} does not exist on this host; "
            "the shipped image creates it, so this deployment cannot contain a "
            "build",
        )
    contained_uid, contained_gid = ids
    if contained_uid == os.getuid():
        raise ContainmentUnavailable(
            f"the contained user {contained_user!r} resolves to uid "
            f"{contained_uid}, which is the uid this controller already runs as; "
            "containing a build under the identity it is being contained FROM is "
            "a no-op",
        )

    requests = request_dir(root)
    results = result_dir(root)
    for path, role in ((requests, "request inbox"), (results, "result directory")):
        if not path.is_dir():
            raise ContainmentUnavailable(
                f"the worklink {role} {path} does not exist; the root-supervised "
                "worklink service creates it at start, and without it a build "
                "would have to run as the agent user",
            )
        _verify_spool_directory(path, role, contained_gid)
    return ContainmentPolicy(user=contained_user, spool_root=root, verified=True)


def submit_request(policy: ContainmentPolicy, request: WorkerRequest) -> str:
    """Publish a request atomically and return its id.

    Written to a temporary name in the same directory and then ``rename``d, so
    the supervisor never observes a half-written request. Atomicity is the ONE
    protocol property worth paying for here; schema validation and replay
    handling are not (the requester is the controller, not a hostile party).
    """
    for name in _NEVER_PROJECTED:
        if name in request.env:
            raise ValueError(
                f"{name} must never be projected into a contained step; push and "
                "PR are controller-side operations",
            )
    requests = request_dir(policy.spool_root)
    request_id = f"{request.attempt_id}-{uuid.uuid4().hex[:12]}"
    payload = json.dumps({"request_id": request_id, **request.to_json()})
    tmp = requests / f".{request_id}.tmp"
    tmp.write_text(payload, encoding="utf-8")
    tmp.rename(requests / f"{request_id}.json")
    return request_id


def await_result(
    policy: ContainmentPolicy,
    request_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> WorkerResult:
    """Block until the supervisor publishes this request's result."""
    path = result_dir(policy.spool_root) / f"{request_id}.json"
    deadline = time.monotonic() + timeout_seconds
    while True:
        if path.exists():
            return WorkerResult.from_json(json.loads(path.read_text(encoding="utf-8")))
        if time.monotonic() >= deadline:
            raise ContainmentUnavailable(
                f"the worklink supervisor published no result for {request_id} "
                f"within {timeout_seconds}s; the service may not be running",
            )
        time.sleep(poll_seconds)


def run_contained(
    policy: ContainmentPolicy,
    request: WorkerRequest,
    *,
    wait_seconds: float | None = None,
) -> WorkerResult:
    """Run one step under the contained identity and return what was observed.

    The single entry point used by the build backend, the evidence gate and the
    local Git steps -- one call site per surface, so a future surface cannot
    quietly skip containment by constructing its own subprocess.
    """
    if not policy.contained:
        # An override or a deployment with nothing to contain. Run in-process,
        # and let the caller's event record carry ``policy.state`` so this is
        # never mistaken for a verified run.
        return _run_uncontained(request)
    request_id = submit_request(policy, request)
    budget = wait_seconds if wait_seconds is not None else (request.timeout_seconds or 3600) + 60
    return await_result(policy, request_id, timeout_seconds=budget)


def _run_uncontained(request: WorkerRequest) -> WorkerResult:
    """The override path. Same observation discipline, no privilege drop."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv is controller-constructed
            list(request.argv),
            cwd=str(request.cwd),
            env=dict(request.env),
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WorkerResult(
            attempt_id=request.attempt_id,
            exit_status=124,
            stdout="",
            stderr=f"timed out after {request.timeout_seconds}s",
            timed_out=True,
        )
    return WorkerResult(
        attempt_id=request.attempt_id,
        exit_status=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        head_oid=observe_head(request.cwd) if request.report_head else None,
    )


def observe_head(checkout: Path) -> str | None:
    """Read the checkout's HEAD.

    Called by whoever is OBSERVING the step (the supervisor, or this module on
    the override path) -- never by the step itself. The controller pushes this
    exact oid, so a process surviving past the verdict cannot change what gets
    pushed by mutating the checkout afterwards.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    oid = proc.stdout.strip()
    return oid if proc.returncode == 0 and oid else None


def observe_head_via_supervisor(policy: ContainmentPolicy, checkout: Path) -> str | None:
    """Ask the supervisor to read HEAD and report what IT saw.

    Distinct from parsing ``git rev-parse`` stdout: that is output from a command
    run by the worker, so the value gating the push would originate on the side
    being judged. ``report_head`` makes the root observer read the checkout after
    the step, which is the provenance the push needs.
    """
    if not policy.contained:
        return None
    result = run_contained(
        policy,
        WorkerRequest(
            attempt_id=f"observe-{checkout.name}",
            argv=("true",),
            cwd=checkout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_seconds=60.0,
            report_head=True,
        ),
    )
    return result.head_oid


#: Checkouts the orchestrator has actually issued for this process. Membership is
#: the routing authority.
_ATTEMPT_CHECKOUTS: set[Path] = set()


def register_attempt_checkout(path: Path) -> None:
    """Record a checkout the orchestrator created for an attempt.

    The supervisor chowns whatever cwd it is handed, recursively, so the routing
    predicate decides what may be taken from the operator. Matching on a
    ``.worklink`` path COMPONENT was too loose: any configured repository that
    happens to sit beneath such a directory would match. Only a checkout this
    process actually issued may cross.
    """
    try:
        _ATTEMPT_CHECKOUTS.add(path.resolve())
    except OSError:  # pragma: no cover
        _ATTEMPT_CHECKOUTS.add(path)


def is_registered_attempt_checkout(path: Path) -> bool:
    """Whether ``path`` is, or is inside, a checkout this process issued."""
    if not _ATTEMPT_CHECKOUTS:
        return False
    try:
        candidate = path.resolve()
    except OSError:  # pragma: no cover
        candidate = path
    return any(
        root == candidate or root in candidate.parents for root in _ATTEMPT_CHECKOUTS
    )
