"""Scope-bound execution of the deployment-configured project test command."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
from collections.abc import Awaitable, Callable
import uuid

from .contained_checkout import ContainedCheckout, create_repo_test_checkout
from .contained_execution import (
    CollectedExecutionResult,
    SensitiveMaterialScrubber,
    base_worker_environment,
    execute_contained,
)
from .contained_snapshot import ContainedSnapshotError, SnapshotCredentialsRefused
from .event_logger import safe_log_event
from .models import RepoPRAction, RepoReviewState
from .redaction import redact_text
from .repo_tools import GitRefusal, RepoGitTools
from .repository_config import RepositoryInventory
from .worklink.backends.registry import WorklinkConfig


_TIMEOUT_SECONDS = 300.0
_CAPTURE_BYTES = 64 * 1024
_RETURN_STDOUT_CHARS = 8_000
_RETURN_STDERR_CHARS = 4_000
_MAX_SELECTORS = 32
_MAX_SELECTOR_LENGTH = 256
_MAX_SELECTOR_BYTES = 4_096
_SELECTOR_PATTERN = re.compile(r"[A-Za-z0-9._/,:+=-]+", re.ASCII)
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class ProjectTestRefusal(RuntimeError):
    """A named policy refusal, distinct from a red test suite."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        try:
            root = RepoGitTools(self._state).validated_checkout_root()
        except GitRefusal as exc:
            raise ProjectTestRefusal(exc.code, str(exc)) from exc
        command, configured_env, command_source = _configured_command(scope.canonical_repo)
        selected = _validated_selectors(root, selectors)
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
            ) from exc
        except (ContainedSnapshotError, OSError, RuntimeError, ValueError) as exc:
            await safe_log_event(
                "repo_test_containment_refused",
                reason_code="snapshot_unavailable",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
            )
            raise ProjectTestRefusal(
                "test_snapshot_unavailable", "project test snapshot is unavailable"
            ) from exc

        identifier = str(uuid.uuid4())
        scrubber.add_path(checkout.path)
        command = _remap_command(root, command)
        environment = base_worker_environment(identifier)
        environment.update(configured_env)
        environment.pop("HOME", None)
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
                ) from exc
            raise ProjectTestRefusal(
                "test_config_invalid",
                "project test command or environment contains a controller path",
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
            except (OSError, RuntimeError, ValueError) as exc:
                await safe_log_event(
                    "repo_test_containment_refused",
                    reason_code="containment_unavailable",
                    repository=scope.canonical_repo,
                    pull_request=scope.pr_number,
                )
                raise ProjectTestRefusal(
                    "test_containment_unavailable",
                    "contained project test execution is unavailable",
                ) from exc
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
                ) from exc

        if result.timed_out:
            return ProjectTestResult(
                False, "test_timeout", None,
                command=command, command_source=command_source,
            )
        stdout = _safe_output(
            result.stdout,
            scrubber,
            _RETURN_STDOUT_CHARS,
            keep_tail=result.stdout_dropped_bytes > 0,
        )
        stderr = _safe_output(
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
        if result.exit_code != 0 or result.output_overflow:
            return ProjectTestResult(
                False, "tests_failed", result.exit_code, stdout, stderr,
                command, command_source, **truncation,
            )
        if not selectors:
            head = self._state.git_expected_head
            if head is None:
                raise ProjectTestRefusal("inactive_checkout", "the checkout has no current HEAD")
            self._state.record_full_test(scope.scope_id, head)
        return ProjectTestResult(
            True, "tests_passed", 0, stdout, stderr, command, command_source,
            **truncation,
        )
