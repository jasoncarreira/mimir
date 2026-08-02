"""Scope-bound execution of the deployment-configured project test command."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import selectors as io_selectors
import shlex
import shutil
import signal
import secrets
import subprocess
import time
from typing import Protocol

from .models import RepoPRAction, RepoReviewState
from .redaction import redact_text
from .repo_tools import GitRefusal, RepoGitTools
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


@dataclass(frozen=True)
class ProjectTestResult:
    ok: bool
    code: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()
    command_source: str = ""


class ProjectTestRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> ProjectTestProcessResult: ...


def _bounded_project_test_runner(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
) -> ProjectTestProcessResult:
    """Run one fixed argv without a shell and cap both streams while reading."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = io_selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, io_selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    output_limited = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _events in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    selector.unregister(stream)
                    continue
                target = streams[stream]
                room = output_limit - len(target)
                target.extend(chunk[:max(room, 0)])
                if len(chunk) > room:
                    output_limited = True
                    break
            if output_limited:
                break
        if timed_out or output_limited:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        returncode = process.wait(timeout=1)
    finally:
        selector.close()
        for stream in streams:
            stream.close()
    return ProjectTestProcessResult(
        returncode,
        bytes(streams[process.stdout]).decode("utf-8", "replace"),
        bytes(streams[process.stderr]).decode("utf-8", "replace"),
        timed_out,
        output_limited,
    )


def _configured_command(repo_slug: str) -> tuple[tuple[str, ...], dict[str, str], str]:
    """Resolve Worklink's deployment command into one shell-free fixed argv."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ProjectTestRefusal("test_not_configured", "MIMIR_HOME is not configured")
    try:
        config = WorklinkConfig.load(Path(home) / "worklink.yaml")
        record = config.repository(repo_slug) if config.repository_config_declared else None
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


#: Components walked from MIMIR_HOME to the per-execution home parent. Each is
#: opened no-follow relative to the previous descriptor, never as one pathname.
_PROJECT_TEST_HOME_COMPONENTS: tuple[str, ...] = ("scratch", "project-test-homes")


def _open_project_test_home_parent() -> tuple[int, Path]:
    """Walk from MIMIR_HOME to the home parent, refusing a symlink at any depth.

    Opening the parent by pathname with ``O_NOFOLLOW`` is not enough: the flag
    applies only to the FINAL component. PR-controlled code can replace an
    intermediate component -- ``<MIMIR_HOME>/scratch`` -- with a symlink whose
    target contains a real ``project-test-homes`` directory, and then both
    ``mkdir(parents=True)`` and ``os.open(parent, O_NOFOLLOW)`` succeed while
    resolving outside the home. The descriptor and every HOME derived from it
    would sit in attacker-chosen storage, restoring the cross-run ambient-state
    channel this function exists to prevent.

    So MIMIR_HOME is the trust anchor -- it is operator-configured and a mount
    root, not something the tests can replace -- and every component below it is
    opened relative to the previous descriptor with ``O_NOFOLLOW``. A symlink at
    any depth fails with ELOOP rather than being followed.
    """
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ProjectTestRefusal("test_not_configured", "MIMIR_HOME is not configured")
    root = Path(home).resolve()
    parent = root
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ProjectTestRefusal(
            "test_cache_home_unavailable",
            f"project test home root is not usable: {root}",
        ) from exc
    try:
        for component in _PROJECT_TEST_HOME_COMPONENTS:
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ProjectTestRefusal(
                    "test_cache_home_unavailable",
                    f"project test home component is not creatable: {parent / component}",
                ) from exc
            try:
                nested = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
                )
            except OSError as exc:
                # ELOOP here means the component is a symlink: refuse rather than
                # follow it out of the home tree.
                raise ProjectTestRefusal(
                    "test_cache_home_unavailable",
                    f"project test home component is not a real directory: {parent / component}",
                ) from exc
            os.close(fd)
            fd = nested
            parent = parent / component
    except BaseException:
        os.close(fd)
        raise
    return fd, parent


def _make_project_test_home(parent_fd: int, parent: Path) -> tuple[str, Path]:
    """Create a fresh 0o700 home relative to *parent_fd*; never reuse a path."""
    for _ in range(8):
        name = f"run-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ProjectTestRefusal(
                "test_cache_home_unavailable",
                f"project test home could not be created under {parent}",
            ) from exc
        return name, parent / name
    raise ProjectTestRefusal(
        "test_cache_home_unavailable",
        f"project test home could not be created under {parent}",
    )


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


def _safe_output(text: str, root: Path, limit: int) -> str:
    scrubbed = redact_text(text).replace(str(root), "<checkout>")
    return scrubbed[:limit]


class RepoProjectTests:
    """Execute the fixed project test command in one authorized PR lease."""

    def __init__(
        self,
        review_state: RepoReviewState,
        *,
        runner: ProjectTestRunner = _bounded_project_test_runner,
        timeout: float = _TIMEOUT_SECONDS,
        output_limit: int = _CAPTURE_BYTES,
    ) -> None:
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("project test timeout and output limit must be positive")
        self._state = review_state
        self._runner = runner
        self._timeout = timeout
        self._output_limit = output_limit

    def execute(self, selectors: tuple[str, ...] = ()) -> ProjectTestResult:
        scope = self._state.action_scope
        if RepoPRAction.TEST.value not in scope.allowed_operations:
            raise ProjectTestRefusal("scope_action_denied", "scope does not grant repo.test")
        try:
            root = RepoGitTools(self._state).validated_checkout_root()
        except GitRefusal as exc:
            raise ProjectTestRefusal(exc.code, str(exc)) from exc
        command, env, command_source = _configured_command(scope.canonical_repo)
        selected = _validated_selectors(root, selectors)
        # Fresh HOME per execution, removed afterwards. mkdtemp creates a new
        # 0o700 directory and fails rather than reusing an existing path, so a
        # planted symlink cannot capture it.
        parent_fd, parent = _open_project_test_home_parent()
        try:
            run_name, run_home = _make_project_test_home(parent_fd, parent)
        except ProjectTestRefusal:
            os.close(parent_fd)
            raise
        env["HOME"] = str(run_home)
        try:
            result = self._runner(
                (*command, *selected), cwd=root, env=env.copy(),
                timeout=self._timeout, output_limit=self._output_limit,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProjectTestRefusal("test_execution_failed", "configured test runner could not start") from exc
        finally:
            # Delete relative to the descriptor, never by pathname: rmtree(dir_fd=)
            # resolves run_name inside the directory we created it in, so replacing
            # the parent mid-run cannot redirect the deletion. A leaked directory
            # costs disk, not isolation -- the next execution mints a new name.
            try:
                shutil.rmtree(run_name, dir_fd=parent_fd, ignore_errors=True)
            finally:
                os.close(parent_fd)
        if result.timed_out:
            return ProjectTestResult(
                False, "test_timeout", None,
                command=command, command_source=command_source,
            )
        if result.output_limited:
            # A token cut at the capture boundary cannot be safely redacted.
            return ProjectTestResult(
                False, "test_output_limit", result.returncode,
                command=command, command_source=command_source,
            )
        stdout = _safe_output(result.stdout, root, _RETURN_STDOUT_CHARS)
        stderr = _safe_output(result.stderr, root, _RETURN_STDERR_CHARS)
        if result.returncode != 0:
            return ProjectTestResult(
                False, "tests_failed", result.returncode, stdout, stderr,
                command, command_source,
            )
        return ProjectTestResult(
            True, "tests_passed", 0, stdout, stderr, command, command_source,
        )
