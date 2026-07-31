"""Typed, scope-bound Git inspection and mutation operations.

This module deliberately does not accept Git argv.  Callers select one of the
closed operation types below; repository, remote, refs, revisions, executable,
configuration overrides, and environment are supplied by the server.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import time
from typing import Literal, Protocol, TypeAlias
from urllib.parse import urlsplit

from .access_control import ToolFlowDirection, authorize_repo_pr_tool
from .git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
from .models import RepoPRAction, RepoPRActionScope, RepoReviewState


_DEFAULT_GIT = Path("/usr/bin/git")
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_OUTPUT_BYTES = 1_048_576
_MAX_PATHS = 256
_MAX_MESSAGE_BYTES = 64 * 1024
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
_FILTER_KEY_RE = re.compile(r"filter\.([^.\x00]+)\.(clean|smudge|process)")
_MERGE_KEY_RE = re.compile(r"merge\.([^.\x00]+)\.driver")

_BASE_CONFIG = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "diff.external=",
    "-c", "credential.helper=",
    "-c", "http.extraHeader=",
    "-c", "http.proxy=",
    "-c", "http.followRedirects=false",
    "-c", "commit.gpgSign=false",
    "-c", "merge.gpgSign=false",
    "-c", "push.pushOption=",
    "-c", "protocol.allow=never",
    "-c", "submodule.recurse=false",
)


class GitRefusal(RuntimeError):
    """A named policy refusal, distinct from a Git command failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limited: bool = False


class GitRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
    ) -> GitProcessResult: ...


@dataclass(frozen=True)
class GitOperationResult:
    ok: bool
    code: str
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GitFetch:
    """Fetch only the immutable head and base refs bound into the scope."""


@dataclass(frozen=True)
class GitStatus:
    include_untracked: bool = True


@dataclass(frozen=True)
class GitDiff:
    mode: Literal["working", "staged", "base"] = "working"
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitUnmerged:
    """Inspect the unmerged index, returning Git's NUL-delimited records."""


@dataclass(frozen=True)
class GitStage:
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GitCommit:
    paths: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class GitMerge:
    """Merge the server-bound observed base commit."""


@dataclass(frozen=True)
class GitMergeAbort:
    """Abort an in-progress merge; never inferred from GitMerge."""


@dataclass(frozen=True)
class GitRebase:
    """Rebase onto the server-bound observed base commit."""


@dataclass(frozen=True)
class GitRebaseAbort:
    """Abort an in-progress rebase; never inferred from GitRebase."""


@dataclass(frozen=True)
class GitRevert:
    commit: str


@dataclass(frozen=True)
class GitRevertAbort:
    """Abort an in-progress revert; separately selected and authorized."""


@dataclass(frozen=True)
class GitPush:
    """Push HEAD to the single destination ref bound into the scope."""


GitOperation: TypeAlias = (
    GitFetch | GitStatus | GitDiff | GitUnmerged | GitStage | GitCommit
    | GitMerge | GitMergeAbort | GitRebase | GitRebaseAbort
    | GitRevert | GitRevertAbort | GitPush
)


def _sanitized_git_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }
    return env


def _bounded_subprocess_runner(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    output_limit: int,
) -> GitProcessResult:
    """Run one argv without a shell, bounding wall time and captured bytes."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
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
                chunk = os.read(stream.fileno(), 64 * 1024)
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
            os.killpg(process.pid, signal.SIGKILL)
        returncode = process.wait(timeout=1)
    finally:
        selector.close()
        for stream in streams:
            stream.close()
    stdout = bytes(streams[process.stdout]).decode("utf-8", "replace")
    stderr = bytes(streams[process.stderr]).decode("utf-8", "replace")
    return GitProcessResult(returncode, stdout, stderr, timed_out, output_limited)


def _validate_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\x00" in path
        or path.startswith(("-", ":"))
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in path
    ):
        raise GitRefusal("invalid_path", "Git paths must be literal repository-relative paths")
    return f":(literal){path}"


def _validated_paths(paths: tuple[str, ...], *, required: bool) -> tuple[str, ...]:
    if not isinstance(paths, tuple):
        raise GitRefusal("invalid_shape", "Git paths must be a tuple")
    if required and not paths:
        raise GitRefusal("explicit_paths_required", "at least one explicit path is required")
    if len(paths) > _MAX_PATHS or len(set(paths)) != len(paths):
        raise GitRefusal("invalid_paths", "Git paths are duplicated or exceed the server limit")
    for path in paths:
        _validate_path(path)
    return paths


class RepoGitTools:
    """Execute closed Git operations against one active PR checkout lease."""

    def __init__(
        self,
        review_state: RepoReviewState,
        *,
        git_executable: Path = _DEFAULT_GIT,
        runner: GitRunner = _bounded_subprocess_runner,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        output_limit: int = _DEFAULT_OUTPUT_BYTES,
        enforce: bool = True,
    ) -> None:
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("Git timeout and output limit must be positive")
        if not git_executable.is_absolute():
            raise ValueError("Git executable pin must be absolute")
        try:
            executable = git_executable.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Git executable pin is missing") from exc
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("Git executable pin is not executable")
        self._state = review_state
        self._scope = review_state.action_scope
        self._git = str(executable)
        self._runner = runner
        self._timeout = timeout
        self._output_limit = output_limit
        self._enforce = enforce
        self._env = _sanitized_git_env()
        self._root = self._validate_lease()
        self._validate_scope()
        self._expected_head = (
            review_state.git_expected_head or self._scope.observed_head_sha
        ).lower()

    @property
    def environment(self) -> dict[str, str]:
        """Expose a copy for audit/tests; mutations cannot affect execution."""
        return self._env.copy()

    def _validate_scope(self) -> None:
        if not _SHA_RE.fullmatch(self._scope.observed_head_sha):
            raise GitRefusal("invalid_scope", "scope head is not a full commit id")
        if not _SHA_RE.fullmatch(self._scope.observed_base_sha):
            raise GitRefusal("invalid_scope", "scope base is not a full commit id")
        if not _REF_RE.fullmatch(self._scope.destination_ref) or any(
            token in self._scope.destination_ref for token in ("..", "//", "@{", "\\")
        ):
            raise GitRefusal("invalid_scope", "scope destination is not a branch ref")
        base_ref = f"refs/heads/{self._scope.base_ref}"
        if not _REF_RE.fullmatch(base_ref) or any(
            token in base_ref for token in ("..", "//", "@{", "\\")
        ):
            raise GitRefusal("invalid_scope", "scope base is not a branch ref")
        if self._scope.head_remote != "origin" or not self._scope.canonical_origin:
            raise GitRefusal("invalid_scope", "scope remote is not server-bound origin")

    def _validate_lease(self) -> Path:
        lease = self._state.checkout_lease
        if (
            lease is None
            or lease.scope_id != self._scope.scope_id
            or lease.owner != self._scope.principal
            or not lease.is_active
        ):
            raise GitRefusal("inactive_checkout", "an active matching checkout lease is required")
        path = Path(lease.path)
        if path.is_symlink():
            raise GitRefusal("invalid_checkout", "checkout lease may not be a symlink")
        try:
            root = path.resolve(strict=True)
            lease_root = Path(lease.lease_root).resolve(strict=True)
        except OSError as exc:
            raise GitRefusal("invalid_checkout", "checkout lease path is unavailable") from exc
        if root.parent != lease_root or root == lease_root:
            raise GitRefusal("invalid_checkout", "checkout lease escapes its server root")
        return root

    def _require(self, tool_name: str, *actions: RepoPRAction) -> None:
        decision = authorize_repo_pr_tool(
            tool_name,
            self._scope,
            service_principal=None,
            enforce=self._enforce,
            flow_direction=ToolFlowDirection.UNKNOWN,
            required_actions=tuple(action.value for action in actions),
        )
        if not decision.allowed:
            raise GitRefusal(
                "scope_action_denied",
                decision.refusal_detail or "scope does not grant the required action",
            )

    def _raw(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> GitProcessResult:
        transport: tuple[str, ...] = ()
        if network:
            origin = network_remote or self._scope.canonical_origin
            if origin.startswith(("https://", "http://")):
                transport = (
                    "-c", "protocol.https.allow=always",
                    "-c", "protocol.http.allow=always",
                )
            elif origin.startswith(("ssh://", "git@")):
                transport = ("-c", "protocol.ssh.allow=always", "-c", "core.sshCommand=/usr/bin/ssh")
            elif origin.startswith("file://") or Path(origin).is_absolute():
                transport = ("-c", "protocol.file.allow=always")
            else:
                raise GitRefusal("unsupported_transport", "scope origin transport is not allowed")
        argv = (
            self._git, "-C", str(self._root), *_BASE_CONFIG, *transport,
            "--no-pager", "--no-optional-locks", *arguments,
        )
        try:
            child_env = self._env.copy()
            if env:
                child_env.update(env)
            result = self._runner(
                argv, env=child_env, timeout=self._timeout,
                output_limit=self._output_limit,
            )
            for value in sorted(filter(None, sensitive_values), key=len, reverse=True):
                result = replace(
                    result,
                    stdout=result.stdout.replace(value, "[REDACTED]"),
                    stderr=result.stderr.replace(value, "[REDACTED]"),
                )
            return result
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitRefusal("git_failed", "pinned Git execution failed") from exc

    def _checked(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
        report_stdout_on_failure: bool = False,
    ) -> GitProcessResult:
        result = self._raw(
            arguments,
            network=network,
            network_remote=network_remote,
            env=env,
            sensitive_values=sensitive_values,
        )
        if result.timed_out:
            raise GitRefusal("timeout", "Git operation exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git operation exceeded its output limit")
        if result.returncode != 0:
            detail = result.stderr.strip()
            if not detail and report_stdout_on_failure:
                detail = result.stdout.strip()
            raise GitRefusal("git_failed", detail or "Git operation failed")
        return result

    def _config_overrides(self) -> tuple[str, ...]:
        result = self._raw((
            "config", "--local", "--null", "--name-only", "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|url\..*\.(insteadOf|pushInsteadOf))$",
        ))
        if result.timed_out:
            raise GitRefusal("timeout", "Git config inspection exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git config inspection exceeded its output limit")
        if result.returncode == 1 and not result.stdout:
            return ()
        if result.returncode != 0:
            raise GitRefusal("unsafe_repo_config", "repository configuration could not be inspected safely")
        names = result.stdout.split("\x00")
        if names[-1:] == [""]:
            names.pop()
        overrides: list[str] = []
        for name in names:
            if _FILTER_KEY_RE.fullmatch(name) or _MERGE_KEY_RE.fullmatch(name):
                overrides.extend(("-c", f"{name}="))
            elif name:
                # URL rewrites can redirect a server-bound remote and have no
                # safe generic neutral value.
                raise GitRefusal("unsafe_repo_config", "repository URL rewrite configuration is refused")
        return tuple(overrides)

    def _command(
        self,
        arguments: tuple[str, ...],
        *,
        network: bool = False,
        network_remote: str | None = None,
        diff: bool = False,
        identity: bool = False,
        overrides: tuple[str, ...] | None = None,
        env: dict[str, str] | None = None,
        sensitive_values: tuple[str, ...] = (),
        report_stdout_on_failure: bool = False,
    ) -> GitProcessResult:
        local_overrides = self._config_overrides() if overrides is None else overrides
        prefix = local_overrides
        if identity:
            prefix += (
                "-c", f"user.name={DEFAULT_USER_NAME}",
                "-c", f"user.email={DEFAULT_USER_EMAIL}",
            )
        suffix = ("--no-ext-diff", "--no-textconv") if diff else ()
        return self._checked(
            (*prefix, *arguments, *suffix),
            network=network,
            network_remote=network_remote,
            env=env,
            sensitive_values=sensitive_values,
            report_stdout_on_failure=report_stdout_on_failure,
        )

    def _assert_checkout_identity(self, *, allow_in_progress: bool = False) -> None:
        top = self._command(("rev-parse", "--show-toplevel")).stdout.strip()
        origin = self._command(("remote", "get-url", "origin")).stdout.strip()
        if Path(top).resolve(strict=False) != self._root or origin != self._scope.canonical_origin:
            raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the PR scope")
        if allow_in_progress:
            return
        branch = self._command(("symbolic-ref", "--quiet", "--short", "HEAD")).stdout.strip()
        head = self._command(("rev-parse", "--verify", "HEAD")).stdout.strip().lower()
        if branch != self._scope.head_ref or head != self._expected_head:
            raise GitRefusal("cross_pr_checkout", "checkout identity no longer matches the PR scope")

    def _refresh_expected_head(self) -> None:
        self._expected_head = self._command(("rev-parse", "--verify", "HEAD")).stdout.strip().lower()
        self._state.record_git_head(self._scope.scope_id, self._expected_head)

    def _stranded_work_message(self) -> str:
        if self._expected_head == self._scope.observed_head_sha.lower():
            return "the checkout lease was preserved for retry"
        return (
            f"local commit {self._expected_head} remains unpushed in preserved "
            f"checkout lease {self._root}"
        )

    def _push_remote(self) -> tuple[str, dict[str, str], tuple[str, ...]]:
        origin = self._scope.canonical_origin
        if origin.startswith("https://"):
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError:
                port = -1
            expected_paths = {
                f"/{self._scope.canonical_repo}",
                f"/{self._scope.canonical_repo}.git",
            }
            if (
                parsed.scheme != "https"
                or parsed.hostname != "github.com"
                or parsed.path not in expected_paths
                or parsed.username
                or parsed.password
                or port
                or parsed.query
                or parsed.fragment
            ):
                raise GitRefusal(
                    "push_auth_unavailable",
                    f"push credential is unavailable for the scoped origin; "
                    f"{self._stranded_work_message()}",
                )
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise GitRefusal(
                    "push_auth_unavailable",
                    f"push credential is unavailable; {self._stranded_work_message()}",
                )
            from .forge.github import confirm_github_identity
            from .forge import ForgeError

            try:
                confirm_github_identity(self._scope.principal, token)
            except ForgeError as exc:
                raise GitRefusal(
                    "push_identity_unverified",
                    f"push identity could not be verified: {exc}; {self._stranded_work_message()}",
                ) from exc
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            authorization = f"Authorization: Basic {encoded}"
            return origin, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{origin}.extraheader",
                "GIT_CONFIG_VALUE_0": authorization,
            }, (authorization, encoded, token)
        if origin.startswith("http://"):
            raise GitRefusal(
                "push_auth_unavailable",
                f"authenticated pushes require HTTPS; {self._stranded_work_message()}",
            )
        return origin, {}, ()

    def _unmerged_paths(self) -> set[str]:
        output = self._command(("ls-files", "--unmerged", "-z")).stdout
        paths: set[str] = set()
        for record in output.split("\x00"):
            if record:
                _metadata, separator, path = record.partition("\t")
                if not separator:
                    raise GitRefusal("invalid_git_output", "malformed unmerged index output")
                paths.add(path)
        return paths

    def is_tracked_file(self, path: Path) -> bool:
        """Return whether ``path`` is an index entry in this exact active lease."""
        root = self._validate_lease()
        if root != self._root:
            raise GitRefusal("cross_pr_checkout", "checkout lease changed during Git access")
        try:
            if not path.is_absolute() or path.is_symlink():
                return False
            path.relative_to(root)
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return False
        if not resolved.is_file():
            return False

        self._assert_checkout_identity()
        pathspec = _validate_path(relative)
        overrides = self._config_overrides()
        result = self._raw((
            *overrides, "ls-files", "--cached", "--error-unmatch", "-z", "--", pathspec,
        ))
        if result.timed_out:
            raise GitRefusal("timeout", "Git tracked-file inspection exceeded its time limit")
        if result.output_limited:
            raise GitRefusal("output_limit", "Git tracked-file inspection exceeded its output limit")
        if result.returncode == 1 and not result.stdout:
            return False
        if result.returncode != 0:
            raise GitRefusal("git_failed", "Git tracked-file inspection failed")
        return result.stdout == f"{relative}\x00"

    def validated_checkout_root(self) -> Path:
        """Revalidate the lease and Git identity immediately before non-Git use."""
        root = self._validate_lease()
        if root != self._root:
            raise GitRefusal("cross_pr_checkout", "checkout lease changed during access")
        self._assert_checkout_identity()
        return root

    def _stage(self, paths: tuple[str, ...]) -> None:
        unmerged = self._unmerged_paths()
        if unmerged and not set(paths).issubset(unmerged):
            raise GitRefusal(
                "unproven_conflict_path",
                "while conflicts exist, every staged path must be proven by the unmerged index",
            )
        pathspecs = tuple(_validate_path(path) for path in paths)
        self._command(("add", "--", *pathspecs))

    def execute(self, operation: GitOperation) -> GitOperationResult:
        """Authorize one typed operation and execute only server-built argv."""
        self._root = self._validate_lease()
        self._assert_checkout_identity(allow_in_progress=isinstance(
            operation, (GitMergeAbort, GitRebaseAbort, GitRevertAbort),
        ))

        if isinstance(operation, GitFetch):
            self._require("repo_fetch", RepoPRAction.CHECKOUT)
            lease = self._state.checkout_lease
            for ref, expected, identity in (
                (self._scope.checkout_ref or self._scope.destination_ref,
                 self._scope.observed_head_sha, "head"),
                (f"refs/heads/{self._scope.base_ref}", lease.base_sha, "base"),
            ):
                self._command((
                    "fetch", "--no-tags", "--no-recurse-submodules",
                    self._scope.canonical_origin, ref,
                ), network=True)
                actual = self._command(("rev-parse", "--verify", "FETCH_HEAD^{commit}")).stdout.strip()
                if actual.lower() != expected.lower():
                    if identity == "head":
                        raise GitRefusal(
                            "stale_scope",
                            f"PR head advanced: scoped head {expected.lower()} is stale; "
                            f"fetched head is {actual.lower()}",
                        )
                    ancestry = self._raw((
                        "merge-base", "--is-ancestor", expected.lower(), actual.lower(),
                    ))
                    if ancestry.returncode == 0:
                        raise GitRefusal(
                            "base_advanced",
                            f"PR base advanced during remediation: checked-out base "
                            f"{expected.lower()} is stale; fetched base is {actual.lower()}; "
                            "restart checkout before rebasing",
                        )
                    raise GitRefusal(
                        "base_history_rewritten",
                        f"PR base history was rewritten during remediation: checked-out base "
                        f"{expected.lower()} is stale; fetched base is {actual.lower()}",
                    )
            return GitOperationResult(True, "ok")

        if isinstance(operation, GitStatus):
            self._require("repo_status", RepoPRAction.INSPECT)
            untracked = "all" if operation.include_untracked else "no"
            result = self._command(("status", "--porcelain=v2", "-z", f"--untracked-files={untracked}"))
        elif isinstance(operation, GitDiff):
            self._require("repo_diff", RepoPRAction.INSPECT)
            paths = _validated_paths(operation.paths, required=False)
            if operation.mode not in {"working", "staged", "base"}:
                raise GitRefusal("invalid_shape", "unsupported diff mode")
            revisions: tuple[str, ...] = ()
            if operation.mode == "staged":
                revisions = ("--cached",)
            elif operation.mode == "base":
                revisions = (f"{self._state.checkout_lease.base_sha}...HEAD",)
            pathspecs = tuple(_validate_path(path) for path in paths)
            separator = ("--", *pathspecs) if pathspecs else ()
            result = self._command((
                "diff", "--no-color", "--no-ext-diff", "--no-textconv",
                *revisions, *separator,
            ))
        elif isinstance(operation, GitUnmerged):
            self._require("repo_unmerged", RepoPRAction.INSPECT)
            result = self._command(("ls-files", "--unmerged", "-z"))
        elif isinstance(operation, GitStage):
            self._require("repo_stage", RepoPRAction.WRITE)
            paths = _validated_paths(operation.paths, required=True)
            self._stage(paths)
            result = GitProcessResult(0)
        elif isinstance(operation, GitCommit):
            self._require("repo_commit", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            paths = _validated_paths(operation.paths, required=True)
            if (
                not isinstance(operation.message, str)
                or not operation.message.strip()
                or "\x00" in operation.message
                or len(operation.message.encode("utf-8")) > _MAX_MESSAGE_BYTES
            ):
                raise GitRefusal("invalid_message", "commit message is empty or exceeds the server limit")
            staged_before = set(filter(None, self._command((
                "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            )).stdout.split("\x00")))
            if not staged_before.issubset(paths):
                raise GitRefusal("dirty_out_of_scope", "the index contains paths outside this commit")
            self._stage(paths)
            staged_after = set(filter(None, self._command((
                "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            )).stdout.split("\x00")))
            if not staged_after or not staged_after.issubset(paths):
                raise GitRefusal("dirty_out_of_scope", "staged paths do not match the explicit commit scope")
            result = self._command(("commit", "-m", operation.message), identity=True)
            self._refresh_expected_head()
        elif isinstance(operation, GitMerge):
            self._require("repo_merge", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            try:
                result = self._command(
                    ("merge", "--no-edit", "--", self._state.checkout_lease.base_sha),
                    identity=True,
                    report_stdout_on_failure=True,
                )
            except GitRefusal as exc:
                if exc.code != "git_failed":
                    raise
                unmerged = self._unmerged_paths()
                if not unmerged:
                    raise
                paths = ", ".join(repr(path) for path in sorted(unmerged))
                raise GitRefusal(
                    "merge_conflict",
                    f"merge conflict in unmerged path(s): {paths}; inspect the unmerged "
                    f"index with repo_unmerged, or abort the merge with repo_merge_abort; "
                    f"Git output: {exc}",
                ) from exc
            self._refresh_expected_head()
        elif isinstance(operation, GitMergeAbort):
            self._require("repo_merge_abort", RepoPRAction.WRITE)
            result = self._command(("merge", "--abort"))
        elif isinstance(operation, GitRebase):
            self._require("repo_rebase", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            result = self._command(
                ("rebase", "--", self._state.checkout_lease.base_sha), identity=True,
            )
            self._refresh_expected_head()
        elif isinstance(operation, GitRebaseAbort):
            self._require("repo_rebase_abort", RepoPRAction.WRITE)
            result = self._command(("rebase", "--abort"))
        elif isinstance(operation, GitRevert):
            self._require("repo_revert", RepoPRAction.WRITE, RepoPRAction.COMMIT)
            if not _SHA_RE.fullmatch(operation.commit):
                raise GitRefusal("invalid_commit", "revert requires one full commit id")
            allowed = set(filter(None, self._command((
                "rev-list", "HEAD", f"^{self._state.checkout_lease.base_sha}", "--",
            )).stdout.splitlines()))
            if operation.commit.lower() not in {commit.lower() for commit in allowed}:
                raise GitRefusal("invalid_revert_ancestry", "revert commit is outside head ^base")
            result = self._command(("revert", "--no-edit", operation.commit), identity=True)
            self._refresh_expected_head()
        elif isinstance(operation, GitRevertAbort):
            self._require("repo_revert_abort", RepoPRAction.WRITE)
            result = self._command(("revert", "--abort"))
        elif isinstance(operation, GitPush):
            self._require("repo_push", RepoPRAction.PUSH)
            overrides = self._config_overrides()
            ancestry = self._raw((
                "merge-base", "--is-ancestor", self._scope.observed_head_sha,
                self._expected_head,
            ))
            if ancestry.returncode != 0:
                raise GitRefusal("force_push_refused", "push would not be a fast-forward")
            try:
                push_remote, auth_env, sensitive_values = self._push_remote()
                remote = self._command((
                    "ls-remote", "--heads", push_remote,
                    self._scope.destination_ref,
                ), network=True, network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values).stdout
                records = [line.split("\t", 1) for line in remote.splitlines() if line]
                if (
                    len(records) != 1
                    or len(records[0]) != 2
                    or records[0][0].lower() != self._scope.observed_head_sha.lower()
                    or records[0][1] != self._scope.destination_ref
                ):
                    return GitOperationResult(
                        False, "stale_scope", stderr=self._stranded_work_message(),
                    )
                result = self._command((
                    "push", "--porcelain", push_remote,
                    f"HEAD:{self._scope.destination_ref}",
                ), network=True, network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values)
                self._command((
                    "fetch", "--no-tags", "--no-recurse-submodules", push_remote,
                    self._scope.destination_ref,
                ), network=True, network_remote=push_remote, overrides=overrides,
                    env=auth_env, sensitive_values=sensitive_values)
                observed = self._command((
                    "rev-parse", "--verify", "FETCH_HEAD^{commit}",
                ), overrides=overrides).stdout.strip().lower()
                reachability = self._raw((
                    *overrides, "merge-base", "--is-ancestor",
                    self._expected_head, observed,
                ))
                if reachability.timed_out:
                    raise GitRefusal("timeout", "push verification exceeded its time limit")
                if reachability.output_limited:
                    raise GitRefusal("output_limit", "push verification exceeded its output limit")
                if reachability.returncode == 1:
                    raise GitRefusal(
                        "push_not_applied",
                        f"push did not update {self._scope.destination_ref} to contain expected "
                        f"commit {self._expected_head}; observed remote commit {observed}; "
                        f"{self._stranded_work_message()}",
                    )
                if reachability.returncode != 0:
                    raise GitRefusal(
                        "git_failed",
                        reachability.stderr.strip() or "push reachability verification failed",
                    )
            except GitRefusal as exc:
                if exc.code == "git_failed":
                    raise GitRefusal(
                        "git_failed", f"push failed: {exc}; {self._stranded_work_message()}",
                    ) from exc
                raise
        else:
            raise GitRefusal("invalid_shape", "unsupported typed Git operation")
        return GitOperationResult(True, "ok", result.stdout, result.stderr)


__all__ = [
    "GitCommit", "GitDiff", "GitFetch", "GitMerge", "GitMergeAbort",
    "GitOperation", "GitOperationResult", "GitProcessResult", "GitPush",
    "GitRebase", "GitRebaseAbort", "GitRefusal", "GitRevert",
    "GitRevertAbort", "GitStage", "GitStatus", "GitUnmerged", "RepoGitTools",
]
