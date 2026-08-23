"""Scope-bound execution of the deployment-configured project test command."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
from collections.abc import Awaitable, Callable
import uuid

from .contained_checkout import ContainedCheckout, create_repo_test_checkout
from .contained_execution import (
    CollectedExecutionResult,
    SensitiveMaterialScrubber,
    base_worker_environment,
    execute_contained,
)
from .contained_snapshot import (
    ContainedSnapshotError,
    SnapshotCredentialsRefused,
    SnapshotEmbeddedRepository,
)
from .event_logger import safe_log_event
from .models import RepoPRAction, RepoReviewState
from .redaction import redact_text
from .repo_tools import GitRefusal, RepoGitTools
from .repository_config import RepositoryInventory
from .worklink.backends.registry import WorklinkConfig
from .worklink.identities import get_identities
from .worklink.worker_client import StaleWorkerExecutorError


#: Wall-clock ceiling for one project-test invocation.
#:
#: 300s could not run this repository's own suite. Every measured full-suite run
#: sits between 492s and 597s, so ``repo_test`` timed out on the gate command
#: every time -- not intermittently, arithmetically. The agent reviewed pull
#: requests reporting "the bounded runner timed out with empty output" and fell
#: back to whatever counts the author supplied.
#:
#: 900s was then chosen from those same measurements, and that was the wrong
#: basis: every one of them was taken on an idle machine, while the runner's
#: normal condition is a busy one. The agent and the worklink builds share a
#: host, so the suite competes with whatever the factory is doing. Measured at
#: 1023s with two concurrent builds running -- it passed, load stretches the
#: run rather than breaking it, but 1023s is already past 900s, so the bound
#: reintroduced the same arithmetic timeout under the load the runner actually
#: sees.
#:
#: 1800s is sized against the loaded case instead, and matches the upper bound
#: CI already allows its own pytest jobs. A generous bound fails safe here: too
#: high merely delays a report that something is wrong, while too low silently
#: denies the agent any full-suite evidence of its own, which is what happened
#: through nine review rounds on #1594.
_TIMEOUT_SECONDS = 1800.0
_CAPTURE_BYTES = 64 * 1024
_RETURN_STDOUT_CHARS = 8_000
_RETURN_STDERR_CHARS = 4_000
_MAX_SELECTORS = 32
_MAX_SELECTOR_LENGTH = 256
_MAX_SELECTOR_BYTES = 4_096
_SELECTOR_PATTERN = re.compile(r"[A-Za-z0-9._/,:+=-]+", re.ASCII)
_PERMISSION_PATH_PATTERN = re.compile(
    rb"(?:failed to open file |failed to access )?[`'](?P<path>/[^`'\r\n]+)[`']"
    rb"[^\r\n]*(?:Permission denied|os error 13|EACCES)",
    re.IGNORECASE,
)
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class ProjectTestRefusal(RuntimeError):
    """A named policy refusal, distinct from a red test suite."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        execution_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.execution_started = execution_started


@dataclass(frozen=True)
class ProjectTestProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limited: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


@dataclass(frozen=True)
class ProjectTestResult:
    ok: bool
    code: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()
    command_source: str = ""
    output_limited: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    git_context: str = ""


ContainedRunner = Callable[..., Awaitable[CollectedExecutionResult]]
CheckoutFactory = Callable[..., ContainedCheckout]


def _configured_command(repo_slug: str) -> tuple[tuple[str, ...], dict[str, str], str]:
    """Resolve Worklink's deployment command into one shell-free fixed argv."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ProjectTestRefusal("test_not_configured", "MIMIR_HOME is not configured")
    try:
        config = WorklinkConfig.load(Path(home) / "worklink.yaml")
        inventory = RepositoryInventory.load(Path(home) / "repositories.yaml")
        record = inventory.repository(repo_slug) if inventory.declared else None
        if record is not None and record.test_command is not None:
            command = record.test_command
            source = "repository"
        else:
            command = config.defaults.test_command
            source = "deployment"
        words = shlex.split(command, posix=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectTestRefusal("test_config_invalid", "project test command is invalid") from exc
    if not words:
        raise ProjectTestRefusal(
            "test_command_unresolvable",
            f"project test command from {source} configuration is empty",
        )

    env = {
        "PATH": _PATH,
        # HOME is deliberately absent here. A writable HOME is required -- every
        # mainstream runner initialises a cache under it, and "/nonexistent" made
        # uv fail before reaching pytest ("Permission denied" creating
        # /nonexistent/.cache/uv) -- but it must be created FRESH PER EXECUTION and
        # removed afterwards, so ``execute`` owns it rather than this function.
        #
        # A shared HOME would be a channel between executions: the tests running in
        # it are PR-controlled, so one PR could plant ambient config (a .npmrc, a
        # pip.conf -- only Git configuration is neutralised below) for a later PR to
        # pick up. Caching is not worth a cross-PR write channel.
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if Path(words[0]).name == "env":
        index = 1
        while index < len(words) and words[index] in {"-u", "--unset"}:
            if index + 1 >= len(words) or not words[index + 1].isidentifier():
                raise ProjectTestRefusal("test_config_invalid", "invalid env unset directive")
            env.pop(words[index + 1], None)
            index += 2
        words = words[index:]
        if not words:
            raise ProjectTestRefusal("test_config_invalid", "test runner is missing")

    executable = Path(words[0])
    try:
        resolved_text = (
            str(executable.resolve(strict=True))
            if executable.is_absolute()
            else shutil.which(words[0], path=_PATH)
        )
    except OSError as exc:
        raise ProjectTestRefusal("test_command_unresolvable", "configured test runner is unavailable") from exc
    if not resolved_text:
        raise ProjectTestRefusal("test_command_unresolvable", "configured test runner is unavailable")
    resolved = Path(resolved_text)
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        raise ProjectTestRefusal("test_command_unresolvable", "configured test runner is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProjectTestRefusal("test_command_unresolvable", "configured test runner is unavailable")
    return (str(resolved), *words[1:]), env, source


def _validated_selectors(root: Path, selectors: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(selectors, tuple):
        raise ProjectTestRefusal("test_selector_invalid", "test selectors must be a tuple")
    if len(selectors) > _MAX_SELECTORS:
        raise ProjectTestRefusal("test_selector_count_exceeded", "too many test selectors")
    if any(not isinstance(item, str) for item in selectors):
        raise ProjectTestRefusal("test_selector_invalid", "test selectors must be strings")
    if sum(len(item.encode("utf-8")) for item in selectors) > _MAX_SELECTOR_BYTES:
        raise ProjectTestRefusal("test_selectors_too_large", "test selectors are too large")
    validated: list[str] = []
    for item in selectors:
        path_text, separator, node_id = item.partition("::")
        candidate = PurePosixPath(path_text)
        if (
            not item
            or len(item) > _MAX_SELECTOR_LENGTH
            or not item.isascii()
            or _SELECTOR_PATTERN.fullmatch(item) is None
            or item.startswith(("-", "/", "@"))
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in path_text
        ):
            raise ProjectTestRefusal("test_selector_invalid", "test selector is not a relative path or node id")
        try:
            unresolved = root / path_text
            if any(
                (root.joinpath(*candidate.parts[:index])).is_symlink()
                for index in range(1, len(candidate.parts) + 1)
            ):
                raise ValueError("selector contains a symlink")
            resolved = unresolved.resolve(strict=True)
            canonical = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectTestRefusal(
                "test_selector_outside_checkout",
                "test selector does not resolve inside the authorized checkout",
            ) from exc
        validated.append(f"{canonical}::{node_id}" if separator else canonical)
    return tuple(validated)


def _remap_command(root: Path, command: tuple[str, ...]) -> tuple[str, ...]:
    executable = Path(command[0])
    try:
        relative = executable.relative_to(root)
    except ValueError:
        return command
    return (f"./{relative.as_posix()}", *command[1:])


def _safe_output(
    value: bytes,
    scrubber: SensitiveMaterialScrubber,
    limit: int,
    *,
    keep_tail: bool = False,
) -> str:
    scrubbed = redact_text(scrubber.scrub_text(value))
    return scrubbed[-limit:] if keep_tail else scrubbed[:limit]


def _safe_stderr_output(
    value: bytes,
    scrubber: SensitiveMaterialScrubber,
    limit: int,
    *,
    keep_tail: bool = False,
) -> str:
    """Prefer a pytest faulthandler dump over positional stderr context."""
    scrubbed = redact_text(scrubber.scrub_text(value))
    marker = re.search(r"Timeout \([^\r\n]+\)!", scrubbed)
    if marker is not None and len(scrubbed) > limit:
        return scrubbed[marker.start():marker.start() + limit]
    return scrubbed[-limit:] if keep_tail else scrubbed[:limit]


def _worker_can_search(metadata: os.stat_result) -> bool:
    identities = get_identities()
    mode = metadata.st_mode
    if metadata.st_uid == identities.worklink_uid:
        return bool(mode & stat.S_IXUSR)
    if metadata.st_gid == identities.worklink_gid:
        return bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IXOTH)


def _permission_diagnostic(path: Path) -> dict[str, object] | None:
    identities = get_identities()
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    diagnostic: dict[str, object] | None = None
    for part in absolute.parts[1:-1]:
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            return diagnostic
        if stat.S_ISDIR(metadata.st_mode) and not _worker_can_search(metadata):
            diagnostic = {
                "path": redact_text(str(absolute)),
                "path_mode": f"0o{stat.S_IMODE(metadata.st_mode):03o}",
                "path_uid": metadata.st_uid,
                "path_gid": metadata.st_gid,
                "runner_effective_uid": identities.worklink_uid,
                "runner_effective_gid": identities.worklink_gid,
                "traversal_failed": redact_text(str(current)),
            }
    return diagnostic


def _permission_diagnostic_from_error(
    error: BaseException | bytes,
) -> dict[str, object] | None:
    path: str | None = None
    if isinstance(error, OSError) and error.errno == 13 and error.filename is not None:
        path = os.fsdecode(error.filename)
    elif isinstance(error, bytes):
        match = _PERMISSION_PATH_PATTERN.search(error)
        if match is not None:
            path = os.fsdecode(match.group("path"))
    if path is None or not os.path.isabs(path):
        return None
    return _permission_diagnostic(Path(path))


def _permission_refusal_message(diagnostic: dict[str, object]) -> str:
    fields = " ".join(f"{key}={value}" for key, value in diagnostic.items())
    return f"contained project test path permission denied: {fields}"


def _git_execution_context() -> str:
    identities = get_identities()
    return (
        "contained Git context: runner=worklink "
        f"uid={identities.worklink_uid} gid={identities.worklink_gid}; "
        f"checkout_owner=mimir uid={identities.mimir_uid} "
        f"gid={identities.worklink_gid}; global_config=/dev/null; "
        "system_config=disabled; safe.directory=*"
    )


class RepoProjectTests:
    """Execute the fixed project test command in a disposable contained checkout."""

    def __init__(
        self,
        review_state: RepoReviewState,
        *,
        runner: ContainedRunner = execute_contained,
        checkout_factory: CheckoutFactory = create_repo_test_checkout,
        timeout: float = _TIMEOUT_SECONDS,
        output_limit: int = _CAPTURE_BYTES,
    ) -> None:
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("project test timeout and output limit must be positive")
        self._state = review_state
        self._runner = runner
        self._checkout_factory = checkout_factory
        self._timeout = timeout
        self._output_limit = output_limit

    async def execute(self, selectors: tuple[str, ...] = ()) -> ProjectTestResult:
        scope = self._state.action_scope
        if RepoPRAction.TEST.value not in scope.allowed_operations:
            raise ProjectTestRefusal("scope_action_denied", "scope does not grant repo.test")
        git_tools: RepoGitTools | None = None
        try:
            git_tools = RepoGitTools(self._state)
            root = git_tools.validated_checkout_root()
        except GitRefusal as exc:
            raise ProjectTestRefusal(
                exc.code,
                str(exc),
                execution_started=bool(
                    git_tools is not None and git_tools.execution_started
                ),
            ) from exc
        try:
            command, configured_env, command_source = _configured_command(
                scope.canonical_repo
            )
            selected = _validated_selectors(root, selectors)
        except ProjectTestRefusal as exc:
            raise ProjectTestRefusal(
                exc.code,
                str(exc),
                execution_started=True,
            ) from exc
        scrubber = SensitiveMaterialScrubber(
            checkout=root,
            source_paths=(os.environ.get("MIMIR_HOME", ""),),
        )
        try:
            checkout = self._checkout_factory(
                root,
                scope_id=scope.scope_id,
                pr_number=scope.pr_number,
                known_sensitive=(),
            )
        except SnapshotCredentialsRefused as exc:
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_credentials",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
            )
            raise ProjectTestRefusal(
                "test_snapshot_credentials_refused",
                "project test snapshot contains credential-like material",
                execution_started=True,
            ) from exc
        except SnapshotEmbeddedRepository as exc:
            # Distinct from the generic branch below on purpose: this one is an
            # ordinary tree layout, not a broken snapshot, and the fix is to point
            # at a tree without a nested checkout. Folded into "unavailable" it
            # reads as a fault in the containment path itself.
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_embedded_repository",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
            )
            raise ProjectTestRefusal(
                "test_snapshot_embedded_repository",
                "project test snapshot source contains an embedded Git repository",
                execution_started=True,
            ) from exc
        except (ContainedSnapshotError, OSError, RuntimeError, ValueError) as exc:
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_unavailable",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
            )
            raise ProjectTestRefusal(
                "test_snapshot_unavailable",
                "project test snapshot is unavailable",
                execution_started=True,
            ) from exc

        identifier = str(uuid.uuid4())
        scrubber.add_path(checkout.path)
        command = _remap_command(root, command)
        environment = base_worker_environment(identifier)
        environment.update(configured_env)
        environment.pop("HOME", None)
        environment.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        })
        if Path(command[0]).name == "uv":
            # uv otherwise searches above the fd-entered checkout for uv.toml.
            # The snapshot's 0700 isolation boundary deliberately cannot be
            # traversed by the worker, so ambient config discovery must be off.
            environment["UV_NO_CONFIG"] = "1"
            # The executor may seed this disposable cache from its image-owned
            # read-only cache. Misses remain writable here and can download
            # normally without giving executions write access to shared state.
            environment["UV_CACHE_DIR"] = f'{environment["XDG_CACHE_HOME"]}/uv'
        if any(scrubber.contains_sensitive(value) for value in (*command, *environment.values())):
            try:
                checkout.close()
            except (OSError, RuntimeError, ValueError) as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="cleanup_failed",
                    repository=scope.canonical_repo,
                    pull_request=scope.pr_number,
                )
                raise ProjectTestRefusal(
                    "test_snapshot_cleanup_failed",
                    "project test snapshot cleanup failed",
                    execution_started=True,
                ) from exc
            raise ProjectTestRefusal(
                "test_config_invalid",
                "project test command or environment contains a controller path",
                execution_started=True,
            )
        try:
            try:
                result = await self._runner(
                    (*command, *selected),
                    checkout.capability,
                    environment,
                    (),
                    identifier=identifier,
                    timeout_s=self._timeout,
                    stdout_limit=self._output_limit,
                    stderr_limit=self._output_limit,
                    scrubber=scrubber,
                )
            except StaleWorkerExecutorError as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="stale_root_executor",
                    repository=scope.canonical_repo,
                    pull_request=scope.pr_number,
                )
                raise ProjectTestRefusal(
                    "test_stale_root_executor",
                    str(exc),
                    execution_started=True,
                ) from exc
            except (OSError, RuntimeError, ValueError) as exc:
                diagnostic = _permission_diagnostic_from_error(exc)
                if diagnostic is not None:
                    await safe_log_event(
                        "repo_test_containment_refused",
                        reason_code="path_permission_denied",
                        repository=scope.canonical_repo,
                        pull_request=scope.pr_number,
                        **diagnostic,
                    )
                    raise ProjectTestRefusal(
                        "test_path_permission_denied",
                        _permission_refusal_message(diagnostic),
                        execution_started=True,
                    ) from exc
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="containment_unavailable",
                    repository=scope.canonical_repo,
                    pull_request=scope.pr_number,
                )
                raise ProjectTestRefusal(
                    "test_containment_unavailable",
                    "contained project test execution is unavailable",
                    execution_started=True,
                ) from exc
            if result.exit_code not in {None, 0}:
                diagnostic = _permission_diagnostic_from_error(result.stderr)
                if diagnostic is not None:
                    await safe_log_event(
                        "repo_test_containment_refused",
                        reason_code="path_permission_denied",
                        repository=scope.canonical_repo,
                        pull_request=scope.pr_number,
                        **diagnostic,
                    )
                    raise ProjectTestRefusal(
                        "test_path_permission_denied",
                        _permission_refusal_message(diagnostic),
                        execution_started=True,
                    )
        finally:
            try:
                checkout.close()
            except (OSError, RuntimeError, ValueError) as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="cleanup_failed",
                    repository=scope.canonical_repo,
                    pull_request=scope.pr_number,
                )
                raise ProjectTestRefusal(
                    "test_snapshot_cleanup_failed",
                    "project test snapshot cleanup failed",
                    execution_started=True,
                ) from exc

        stdout = _safe_output(
            result.stdout,
            scrubber,
            _RETURN_STDOUT_CHARS,
            keep_tail=result.stdout_dropped_bytes > 0,
        )
        stderr = _safe_stderr_output(
            result.stderr,
            scrubber,
            _RETURN_STDERR_CHARS,
            keep_tail=result.stderr_dropped_bytes > 0,
        )
        truncation = {
            "output_limited": result.output_overflow,
            "stdout_dropped_bytes": result.stdout_dropped_bytes,
            "stderr_dropped_bytes": result.stderr_dropped_bytes,
        }
        if result.timed_out:
            return ProjectTestResult(
                False, "test_timeout", None, stdout, stderr,
                command, command_source, **truncation,
                git_context=_git_execution_context(),
            )
        if result.exit_code != 0 or result.output_overflow:
            return ProjectTestResult(
                False, "tests_failed", result.exit_code, stdout, stderr,
                command, command_source, **truncation,
                git_context=_git_execution_context(),
            )
        if not selectors:
            head = self._state.git_expected_head
            if head is None:
                raise ProjectTestRefusal(
                    "inactive_checkout",
                    "the checkout has no current HEAD",
                    execution_started=True,
                )
            self._state.record_full_test(scope.scope_id, head)
        return ProjectTestResult(
            True, "tests_passed", 0, stdout, stderr, command, command_source,
            **truncation, git_context=_git_execution_context(),
        )
