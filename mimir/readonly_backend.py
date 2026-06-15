"""Filesystem backends that enforce per-directory write permissions.

``WriteGuardBackend`` wraps ``FilesystemBackend`` and blocks writes/edits
outside an operator-configured allowlist (``Config.writable_dirs``).
``ReadOnlyFilesystemBackend`` blocks every write. Both delegate read
operations to the underlying ``FilesystemBackend`` unchanged, so file
search and read tools keep working.

Wired into ``create_deep_agent(backend=...)`` from ``mimir.agent``.
Ported from open-strix; the ``LoggingWriteGuardBackend`` variant is
intentionally not carried over — mimir's ``turn_logger`` already
records the relevant events on the dispatcher side.

Security boundary: lexical ``..`` collapse + ``Path.resolve()``
defeat both ``"/state/../logs/x.txt"``-style traversal and symlink
escapes (a symlink under ``state/`` pointing into ``.mimir/`` resolves
out of the writable root and is rejected). Read methods are an
explicit allowlist — no ``__getattr__`` passthrough — so future
mutator methods added by deepagents (``delete_file``, ``rename``,
``mkdir``, …) don't silently bypass the guard.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult

log = logging.getLogger(__name__)


class _RootAwareFilesystemBackend(FilesystemBackend):
    """FilesystemBackend that treats absolute paths under ``cwd`` as virtual.

    Upstream's ``virtual_mode=True`` resolves every incoming path as a
    virtual path under ``cwd``, so ``/mimir-home/state/foo.md`` becomes
    ``cwd/mimir-home/state/foo.md`` on disk — i.e. double-prefixed.
    Agents that see container-absolute paths in shell output (or in the
    feedback signals at the top of the prompt) naturally call
    ``read_file("/mimir-home/...")`` and get a misleading "not found",
    even when the file is right there.

    The fix: if an incoming absolute path is already rooted at ``cwd``,
    strip the prefix before ``_resolve_path`` sees it. The virtual form
    (``/state/foo.md``) still works unchanged, so existing callers are
    not affected.
    """

    def _resolve_path(self, key: str) -> Path:
        if self.virtual_mode and key.startswith("/"):
            root_str = str(self.cwd).rstrip("/")
            if key == root_str:
                key = "/"
            elif key.startswith(root_str + "/"):
                key = "/" + key[len(root_str) + 1:]
        return super()._resolve_path(key)


def _normalize_writable_dir(name: str) -> str | None:
    """Reject unsafe names (``.``, ``..``, absolute, traversal) up-front.

    Returns the cleaned name (no leading/trailing slash) or ``None`` if
    the input should be ignored. Logging the rejection at warning level
    keeps operator typos visible without crashing startup.
    """
    raw = name
    cleaned = name.strip().strip("/")
    if not cleaned:
        log.warning("WriteGuardBackend: ignoring empty writable_dir %r", raw)
        return None
    if cleaned in (".", ".."):
        log.warning(
            "WriteGuardBackend: ignoring writable_dir %r (would alias root)", raw,
        )
        return None
    parts = PurePosixPath(cleaned).parts
    if any(p in ("..", ".") for p in parts):
        log.warning(
            "WriteGuardBackend: ignoring writable_dir %r (contains traversal)", raw,
        )
        return None
    return cleaned


class WriteGuardBackend:
    """Filesystem backend that restricts writes to specific directories.

    ``writable_dirs`` are path fragments (e.g. ``"state"`` or
    ``"state/agent"``) interpreted relative to ``root_dir``. A write is
    allowed when the request's target, after lexical ``..`` collapse
    AND symlink resolution, resolves under one of the writable roots
    (which themselves must resolve under ``root_dir``).
    """

    # Methods we explicitly forward to the underlying FilesystemBackend.
    # Anything not in this list raises AttributeError — default-deny so
    # a future deepagents release adding ``delete_file`` / ``rename`` /
    # ``mkdir`` can't bypass the guard until we audit + wrap it.
    #
    # The ``*_info`` / ``*_raw`` variants are pre-deepagents-0.6 low-
    # level shapes (return raw structs). The bare names (``ls``,
    # ``als``, ``grep``, ``agrep``, ``glob``, ``aglob``) are the
    # deepagents-0.6+ high-level wrappers — they're what the agent
    # actually calls as filesystem tools. Both kinds are read-only
    # (audited against ``deepagents/backends/composite.py``); allow-
    # listing both keeps back-compat with older deepagents versions
    # while making the 0.6+ tool surface work. Pre-fix, an agent on
    # deepagents 0.6+ hit ``AttributeError: WriteGuardBackend does
    # not forward 'agrep'`` every turn it tried to grep — surfaced
    # during muninn-mimir cutover 2026-05-20.
    _ALLOWED_READS = frozenset({
        "read", "aread",
        "ls", "als", "ls_info", "als_info",
        "grep", "agrep", "grep_raw", "agrep_raw",
        "glob", "aglob", "glob_info", "aglob_info",
        "execute", "aexecute",  # bash via backend — read-shaped from FS perspective
        "download_files", "adownload_files",
    })

    def __init__(
        self,
        root_dir: Path,
        writable_dirs: list[str],
        *,
        enforce_core_memory_readonly: bool = True,
    ) -> None:
        self._root = Path(root_dir).resolve()
        self._fs = _RootAwareFilesystemBackend(root_dir=root_dir, virtual_mode=True)
        cleaned: list[str] = []
        for d in writable_dirs:
            normalized = _normalize_writable_dir(d)
            if normalized is not None:
                cleaned.append(normalized)
        # Pre-resolve writable roots so the per-write check just compares
        # resolved-target vs resolved-root. resolve(strict=False) handles
        # roots that don't exist yet (created lazily on first write).
        self._writable_roots: list[Path] = [
            (self._root / d).resolve() for d in cleaned
        ]
        # For error messages — the friendlier "/state/" form.
        self._writable_labels: list[str] = ["/" + d for d in cleaned]
        # Pre-resolved memory/core/ root for the runtime read-only gate.
        # When ``enforce_core_memory_readonly`` is True, writes under this
        # path are blocked during ANY active turn (chainlink #342): core
        # memory is the agent's constitution, and changes go through the PR
        # proposal flow (open_proposal) for operator review — not
        # an in-turn write, reflection included. Turns with no TurnContext
        # (the scaffold genesis seed, ``mimir setup``, tests, non-turn
        # callables) are unaffected, so the initial seed still works. Policy:
        # ``memory/core/30-reflection-policy.md``.
        self._memory_core_root: Path = (self._root / "memory" / "core").resolve()
        # Pre-resolved prompts/ root. prompts is operator-managed and (by
        # default) not a writable dir, so live writes are already blocked by the
        # writable-root check; this lets the deny message point at the proposal
        # flow (chainlink #344) instead of a generic "not writable".
        self._prompts_root: Path = (self._root / "prompts").resolve()
        self._enforce_core_memory_readonly: bool = enforce_core_memory_readonly
        # Recorded denials, one per blocked Write/Edit/upload. The agent
        # drains entries for its own turn at end-of-turn
        # (``drain_denials(turn_id=...)``) into TurnRecord.permission_denials
        # so the audit trail is visible in the turn viewer instead of
        # silently empty. Entries carry the active turn id because the backend
        # is process-global and cross-channel turns can run concurrently.
        self._denials: list[dict[str, Any]] = []

    def drain_denials(self, turn_id: str | None = None) -> list[dict[str, Any]]:
        """Return + clear recorded permission denials.

        ``turn_id=None`` preserves the historical behavior: drain everything,
        useful for tests and non-turn callers. Agent.run_turn passes its
        concrete turn id so concurrent cross-channel turns sharing the
        process-global backend cannot drain and misattribute each other's
        write-guard denials.
        """
        if turn_id is None:
            snapshot = list(self._denials)
            self._denials.clear()
            return snapshot

        matched: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for denial in self._denials:
            if denial.get("turn_id") == turn_id:
                matched.append(denial)
            else:
                remaining.append(denial)
        self._denials = remaining
        return matched

    def _record_denial(self, op: str, file_path: str) -> None:
        denial: dict[str, Any] = {
            "op": op,
            "file_path": file_path,
            "writable_dirs": list(self._writable_labels),
        }
        # Lazy import to avoid a module cycle. A missing context is valid for
        # setup/tests/non-turn callables; in-turn denials get scoped so the
        # process-global backend can be drained safely by concurrent turns.
        from ._context import get_current_turn
        ctx = get_current_turn()
        if ctx is not None:
            denial["turn_id"] = ctx.turn_id
        self._denials.append(denial)

    def __getattr__(self, name: str) -> Any:
        # Default-deny passthrough: only explicit reads forward.
        if name in self._ALLOWED_READS:
            return getattr(self._fs, name)
        raise AttributeError(
            f"{type(self).__name__} does not forward {name!r} — "
            f"add it to _ALLOWED_READS after auditing whether it's a mutator."
        )

    def _canonicalize_path(self, file_path: str) -> str:
        """Strip the container-root prefix from absolute paths.

        Agents see container-absolute paths (``/mimir-home/state/x.md``)
        in shell output and feedback signals, then pass them straight to
        read/write tools. Without this collapse, both the FilesystemBackend
        (with ``virtual_mode=True``) and ``_resolve_target`` end up
        double-prefixing the path with ``root_dir``.
        """
        if not file_path.startswith("/"):
            return file_path
        root_str = str(self._root).rstrip("/")
        if file_path == root_str:
            return "/"
        if file_path.startswith(root_str + "/"):
            return "/" + file_path[len(root_str) + 1:]
        return file_path

    def _resolve_target(self, file_path: str) -> Path | None:
        """Resolve a tool-supplied path to a real filesystem location.

        Returns ``None`` when the input contains lexical traversal that
        survives normalization (cheap check before hitting the disk).
        """
        # Strip the container-root prefix (no-op when the path is already
        # in virtual form) so the writable-root check sees the same path
        # the FilesystemBackend will receive after canonicalization.
        file_path = self._canonicalize_path(file_path)
        # Strip leading slashes so the input is always relative to root.
        relative = PurePosixPath(file_path.lstrip("/"))
        # Lexical traversal rejection before touching the disk: anything
        # whose normalized form starts with ``..`` is escaping.
        if any(p == ".." for p in relative.parts):
            return None
        try:
            return (self._root / relative).resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _is_write_allowed(self, file_path: str) -> bool:
        resolved = self._resolve_target(file_path)
        if resolved is None:
            return False
        # Resolved target must live under at least one writable root.
        # ``is_relative_to`` was added in Python 3.9; we depend on >=3.11.
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in self._writable_roots
        )

    def _is_core_memory_write_blocked(self, file_path: str) -> bool:
        """True iff this write should be refused — ``memory/core/`` is
        read-only at runtime (chainlink #342).

        Returns True only when ALL of these hold:
          1. ``enforce_core_memory_readonly`` is True (default)
          2. the resolved target is under ``memory/core/``
          3. there is an active ``TurnContext``

        No active turn (the scaffold genesis seed, ``mimir setup``, backend
        tests, non-turn cron callables) → False, so the initial core seed and
        tooling are unaffected. There is no reflection exception and no
        onboarding bypass: every in-turn core change — reflection included —
        goes through the PR proposal flow (open_proposal). The
        first check (writable-root membership) is done by ``_is_write_allowed``;
        this gate stacks on top.
        """
        if not self._enforce_core_memory_readonly:
            return False
        resolved = self._resolve_target(file_path)
        if resolved is None:
            return False
        under_core = (
            resolved == self._memory_core_root
            or resolved.is_relative_to(self._memory_core_root)
        )
        if not under_core:
            return False
        # Lazy import to avoid a module cycle (mimir._context → models →
        # potentially back into the agent layer that constructs the backend).
        from ._context import get_current_turn
        return get_current_turn() is not None

    def _is_prompts_path(self, file_path: str) -> bool:
        """True if ``file_path`` resolves under ``prompts/`` — used only to
        choose a more helpful deny message (point at the proposal flow) when a
        prompts write is blocked. Does NOT itself block: prompts is gated by
        the writable-root check (it isn't a writable dir by default)."""
        resolved = self._resolve_target(file_path)
        if resolved is None:
            return False
        return (
            resolved == self._prompts_root
            or resolved.is_relative_to(self._prompts_root)
        )

    _CORE_MEMORY_DENY_REASON = (
        "Write blocked: memory/core/ is read-only at runtime by policy "
        "(memory/core/30-reflection-policy.md). To change core memory, open a "
        "proposal with open_proposal, edit it there, then submit_proposal — the "
        "operator reviews and merges the PR. For a non-diff suggestion, write to "
        "state/proposed-changes.md."
    )

    _PROMPTS_DENY_REASON = (
        "Write blocked: prompts/ is operator-managed and not writable at "
        "runtime. To change a prompt template, open a proposal with "
        "open_proposal, edit it there, then submit_proposal — the operator "
        "reviews and merges the PR."
    )

    def _allowed_dirs_label(self) -> str:
        return ", ".join(f"{label}/" for label in self._writable_labels)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._is_write_allowed(file_path):
            if self._is_prompts_path(file_path):
                self._record_denial("write_prompts_readonly", file_path)
                return WriteResult(error=self._PROMPTS_DENY_REASON)
            self._record_denial("write", file_path)
            return WriteResult(
                error=f"Write blocked. Writable directories: {self._allowed_dirs_label()}",
            )
        if self._is_core_memory_write_blocked(file_path):
            self._record_denial("write_core_memory_readonly", file_path)
            return WriteResult(error=self._CORE_MEMORY_DENY_REASON)
        # Idempotent with the strip already applied inside
        # ``_resolve_target`` during the writable-root check — the
        # explicit call here keeps the forward to ``self._fs`` self-
        # contained against future refactors of ``_resolve_target``.
        return self._fs.write(
            file_path=self._canonicalize_path(file_path),
            content=content,
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path=file_path, content=content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not self._is_write_allowed(file_path):
            if self._is_prompts_path(file_path):
                self._record_denial("edit_prompts_readonly", file_path)
                return EditResult(error=self._PROMPTS_DENY_REASON)
            self._record_denial("edit", file_path)
            return EditResult(
                error=f"Edit blocked. Writable directories: {self._allowed_dirs_label()}",
            )
        if self._is_core_memory_write_blocked(file_path):
            self._record_denial("edit_core_memory_readonly", file_path)
            return EditResult(error=self._CORE_MEMORY_DENY_REASON)
        # Idempotent with ``_resolve_target`` — see ``write`` above.
        return self._fs.edit(
            file_path=self._canonicalize_path(file_path),
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        # Atomic: if ANY path is blocked we reject the entire batch.
        # The response shape is one entry per input path so the caller
        # can match input order; allowed-but-not-written paths get
        # ``permission_denied`` too (because nothing was uploaded).
        # This is the documented semantic — partial uploads with a
        # mixed response shape would be more surprising than failing
        # the batch cleanly.
        blocked_paths = {p for p, _ in files if not self._is_write_allowed(p)}
        core_blocked = {p for p, _ in files if self._is_core_memory_write_blocked(p)}
        if blocked_paths or core_blocked:
            for p in blocked_paths:
                self._record_denial("upload", p)
            for p in core_blocked - blocked_paths:
                self._record_denial("upload_core_memory_readonly", p)
            return [
                FileUploadResponse(path=p, error="permission_denied")
                for p, _ in files
            ]
        return self._fs.upload_files(files)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)


class ReadOnlyFilesystemBackend:
    """Read-only wrapper: every write/edit/upload returns an error.

    Use via ``CompositeBackend`` to scope read-only treatment to a
    specific subtree (e.g. operator-shipped reference docs).
    """

    _ALLOWED_READS = WriteGuardBackend._ALLOWED_READS

    def __init__(self, root_dir: Path) -> None:
        self._fs = _RootAwareFilesystemBackend(root_dir=root_dir, virtual_mode=True)

    def __getattr__(self, name: str) -> Any:
        if name in self._ALLOWED_READS:
            return getattr(self._fs, name)
        raise AttributeError(
            f"{type(self).__name__} does not forward {name!r} — "
            f"add it to _ALLOWED_READS after auditing whether it's a mutator."
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"Write blocked. '{file_path}' is read-only.")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path=file_path, content=content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=f"Edit blocked. '{file_path}' is read-only.")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)
