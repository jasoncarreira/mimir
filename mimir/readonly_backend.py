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

import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import (
    EditResult,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

log = logging.getLogger(__name__)

_DEFAULT_TRAVERSAL_EXCLUDES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    ".worktrees",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
})
_DEFAULT_MAX_GREP_MATCHES = 2_000
_DEFAULT_MAX_GLOB_MATCHES = 2_000
_DEFAULT_MAX_SCAN_FILES = 20_000
_DEFAULT_GREP_TIMEOUT_SECONDS = 10


def _log_truncation(op: str, reason: str) -> None:
    log.warning("%s truncated: %s; narrow the path or pattern", op, reason)


def _walk_files_bounded(root: Path, excludes: frozenset[str]) -> Iterator[Path]:
    """Yield files under ``root`` without descending into excluded directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excludes)
        base = Path(dirpath)
        for filename in sorted(filenames):
            yield base / filename


def _real_path_outside_root(key: str, cwd: Path) -> Path | None:
    """Return the resolved real path iff ``key`` names an EXISTING file/dir
    whose real location is outside ``cwd``.

    Distinguishes "the caller passed a literal absolute path that really exists
    elsewhere" (so silently remapping it under ``cwd`` would hide it — the
    false-not-found of chainlink #650) from a normal virtual path (which has no
    literal real-disk existence). Returns ``None`` when the path doesn't exist
    or already lives under ``cwd``."""
    if key == "/":
        return None
    try:
        root = Path(cwd).resolve()  # resolve both sides so a symlinked home
                                    # (e.g. macOS /var -> /private/var) can't
                                    # false-flag a path that is really under it
        virtual = (Path(cwd) / key.lstrip("/")).resolve()
        if virtual.exists():
            try:
                virtual.relative_to(root)
                return None
            except ValueError:
                pass

        literal = Path(key)
        if not literal.is_absolute():
            return None
        if not literal.exists():
            return None
        real = literal.resolve()
    except OSError:
        return None
    try:
        real.relative_to(root)
    except ValueError:
        return real
    return None


class _BoundedFilesystemBackend(FilesystemBackend):
    """FilesystemBackend with bounded recursive grep/glob traversal.

    DeepAgents' stock backend already runs async grep/glob through
    ``asyncio.to_thread`` and gives ripgrep a 30s timeout, but the synchronous
    call still walks every configured root recursively. Once mimir exposed
    operator-configured multi-GB roots (chainlink #650), a broad grep over a
    route such as ``/workspace/mimir`` could descend into ``.worktrees/`` or
    ``node_modules/`` for hundreds of seconds and starve the service. This
    subclass keeps read/write behavior intact while making recursive search
    safe by default: skip vendor/VCS/build trees and cap matches/files/time.

    This is defense in depth on top of PR #875's Docker-level ripgrep install:
    the ``rg`` path handles the normal case quickly, while the Python fallback
    remains bounded for drifted images or shells without ``rg``. Revisit this
    local fork if deepagents grows native traversal bounds or a non-error
    truncation signal.
    """

    def __init__(
        self,
        *args: Any,
        traversal_excludes: Iterable[str] = _DEFAULT_TRAVERSAL_EXCLUDES,
        max_grep_matches: int = _DEFAULT_MAX_GREP_MATCHES,
        max_glob_matches: int = _DEFAULT_MAX_GLOB_MATCHES,
        max_scan_files: int = _DEFAULT_MAX_SCAN_FILES,
        grep_timeout_seconds: int = _DEFAULT_GREP_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._traversal_excludes = frozenset(traversal_excludes)
        self._max_grep_matches = max_grep_matches
        self._max_glob_matches = max_glob_matches
        self._max_scan_files = max_scan_files
        self._grep_timeout_seconds = grep_timeout_seconds

    def _is_excluded(self, path: Path) -> bool:
        try:
            rel_parts = path.resolve().relative_to(self.cwd.resolve()).parts
        except (OSError, RuntimeError, ValueError):
            rel_parts = path.parts
        return any(part in self._traversal_excludes for part in rel_parts)

    def _ripgrep_search(
        self, pattern: str, base_full: Path, include_glob: str | None,
    ) -> tuple[dict[str, list[tuple[int, str]]], str | None] | None:
        """Search with ripgrep while excluding expensive dirs and bounding time."""
        cmd = ["rg", "--json", "-F"]
        for name in sorted(self._traversal_excludes):
            cmd.extend(["--glob", f"!{name}/**"])
        if include_glob:
            cmd.extend(["--glob", include_glob])
        cmd.extend(["--", pattern, str(base_full)])

        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=self._grep_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {}, f"ran longer than {self._grep_timeout_seconds}s"
        except (FileNotFoundError, PermissionError):
            return None

        results: dict[str, list[tuple[int, str]]] = {}
        match_count = 0
        truncated: str | None = None
        for line in proc.stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "match":
                continue
            pdata = data.get("data", {})
            ftext = pdata.get("path", {}).get("text")
            if not ftext:
                continue
            p = Path(ftext)
            if self._is_excluded(p):
                continue
            if self.virtual_mode:
                try:
                    virt = self._to_virtual_path(p)
                except ValueError:
                    log.debug("Skipping grep result outside root: %s", p)
                    continue
                except (OSError, RuntimeError):
                    log.warning("Could not resolve grep result path: %s", p, exc_info=True)
                    continue
            else:
                virt = str(p)
            ln = pdata.get("line_number")
            lt = pdata.get("lines", {}).get("text", "").rstrip("\n")
            if ln is None:
                continue
            results.setdefault(virt, []).append((int(ln), lt))
            match_count += 1
            if match_count >= self._max_grep_matches:
                truncated = f"matched more than {self._max_grep_matches} lines"
                break
        return results, truncated

    def _python_search(
        self, pattern: str, base_full: Path, include_glob: str | None,
    ) -> tuple[dict[str, list[tuple[int, str]]], str | None]:  # noqa: C901, PLR0912
        """Fallback search with excluded dirs and file/match caps."""
        regex = re.compile(pattern)
        results: dict[str, list[tuple[int, str]]] = {}
        root = base_full if base_full.is_dir() else base_full.parent
        candidates: Iterator[Path]
        if base_full.is_file():
            candidates = iter([base_full])
        else:
            candidates = _walk_files_bounded(root, self._traversal_excludes)
        scanned = 0
        matches = 0
        truncated: str | None = None
        for fp in candidates:
            try:
                if not fp.is_file():
                    continue
            except (PermissionError, OSError, RuntimeError):
                continue
            if include_glob and not fp.match(include_glob):
                continue
            scanned += 1
            if scanned > self._max_scan_files:
                truncated = f"scanned more than {self._max_scan_files} files"
                break
            try:
                if fp.stat().st_size > self.max_file_size_bytes:
                    continue
            except (OSError, RuntimeError):
                continue
            try:
                content = fp.read_text()
            except (UnicodeDecodeError, PermissionError, OSError, RuntimeError):
                continue
            for line_num, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    if self.virtual_mode:
                        try:
                            virt_path = self._to_virtual_path(fp)
                        except ValueError:
                            log.debug("Skipping grep result outside root: %s", fp)
                            continue
                        except (OSError, RuntimeError):
                            log.warning("Could not resolve grep result path: %s", fp, exc_info=True)
                            continue
                    else:
                        virt_path = str(fp)
                    results.setdefault(virt_path, []).append((line_num, line))
                    matches += 1
                    if matches >= self._max_grep_matches:
                        return results, f"matched more than {self._max_grep_matches} lines"
        return results, truncated

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search for literal text with bounded traversal and result caps."""
        try:
            base_full = self._resolve_path(path or ".")
        except ValueError:
            return GrepResult(matches=[])
        except (OSError, RuntimeError) as e:
            search_path = path or "."
            return GrepResult(error=f"Error searching path '{search_path}': {e}", matches=[])

        try:
            if not base_full.exists():
                return GrepResult(matches=[])
        except OSError as e:
            search_path = path or "."
            return GrepResult(error=f"Error searching path '{search_path}': {e}", matches=[])

        rg_results = self._ripgrep_search(pattern, base_full, glob)
        if rg_results is None:
            results, truncated = self._python_search(re.escape(pattern), base_full, glob)
        else:
            results, truncated = rg_results

        matches = []
        for fpath, items in results.items():
            for line_num, line_text in items:
                matches.append({"path": fpath, "line": int(line_num), "text": line_text})
        if truncated:
            _log_truncation("Grep", truncated)
        return GrepResult(matches=matches)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:  # noqa: C901, PLR0912
        """Find files matching a glob pattern without walking excluded trees forever."""
        if pattern.startswith("/"):
            pattern = pattern.lstrip("/")

        if self.virtual_mode and ".." in Path(pattern).parts:
            return GlobResult(error="Path traversal not allowed in glob pattern", matches=[])

        try:
            search_path = self.cwd if path == "/" else self._resolve_path(path)
            if not search_path.exists():
                return GlobResult(matches=[])
        except (OSError, RuntimeError) as e:
            return GlobResult(error=f"Error globbing path '{path}': {e}", matches=[])

        results: list[dict[str, Any]] = []
        scanned = 0
        truncated: str | None = None
        candidates: Iterator[Path]
        if search_path.is_file():
            candidates = iter([search_path])
        else:
            candidates = _walk_files_bounded(search_path, self._traversal_excludes)
        try:
            for candidate in candidates:
                scanned += 1
                if scanned > self._max_scan_files:
                    truncated = f"scanned more than {self._max_scan_files} files"
                    break
                if not candidate.match(pattern):
                    continue
                matched_path = candidate
                try:
                    is_file = matched_path.is_file()
                except (PermissionError, OSError, RuntimeError):
                    continue
                if not is_file:
                    continue
                if self.virtual_mode:
                    try:
                        matched_path.resolve().relative_to(self.cwd)
                    except (OSError, RuntimeError, ValueError):
                        continue
                    try:
                        out_path = self._to_virtual_path(matched_path)
                    except ValueError:
                        log.debug("Skipping glob result outside root: %s", matched_path)
                        continue
                    except (OSError, RuntimeError):
                        log.warning("Could not resolve glob result path: %s", matched_path, exc_info=True)
                        continue
                else:
                    out_path = str(matched_path)
                try:
                    st = matched_path.stat()
                    results.append({
                        "path": out_path,
                        "is_dir": False,
                        "size": int(st.st_size),
                        "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    })
                except OSError:
                    results.append({"path": out_path, "is_dir": False})
                if len(results) >= self._max_glob_matches:
                    truncated = f"matched more than {self._max_glob_matches} files"
                    break
        except (OSError, RuntimeError, ValueError) as e:
            msg = f"Glob of '{path}' aborted partway: {e}"
            log.warning("%s", msg, exc_info=True)
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(error=msg, matches=results)

        results.sort(key=lambda x: x.get("path", ""))
        if truncated:
            _log_truncation("Glob", truncated)
        return GlobResult(matches=results)

    def ls(self, path: str) -> LsResult:
        """List direct children, omitting excluded traversal roots by default."""
        result = super().ls(path)
        entries = result.entries or []
        filtered = [
            entry for entry in entries
            if Path(str(entry.get("path", "")).rstrip("/")).name not in self._traversal_excludes
        ]
        return LsResult(error=result.error, entries=filtered)


class _RootAwareFilesystemBackend(_BoundedFilesystemBackend):
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

    def __init__(self, *args: Any, guard_outside_root: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # When True, an absolute path naming a REAL file outside ``cwd`` raises a
        # clear error instead of being silently remapped under ``cwd`` (the
        # false-not-found of chainlink #650). Opt-in: only the home/default
        # backend sets it; CompositeBackend route backends receive
        # prefix-stripped keys, so the guard never misfires on them.
        self._guard_outside_root = guard_outside_root

    def _resolve_path(self, key: str) -> Path:
        if self.virtual_mode and key.startswith("/"):
            root_str = str(self.cwd).rstrip("/")
            if key == root_str:
                key = "/"
            elif key.startswith(root_str + "/"):
                key = "/" + key[len(root_str) + 1:]
        return super()._resolve_path(key)

    # --- out-of-root guard (chainlink #650) ---------------------------------
    # When ``guard_outside_root`` is set (the home/default backend), an absolute
    # path naming a REAL file/dir outside ``cwd`` would otherwise be silently
    # remapped under ``cwd`` and read back as "not found" (the 746-failure bug).
    # Return a clear, actionable error RESULT instead. We return rather than
    # raise: deepagents' middleware wraps only ``validate_path`` (not the backend
    # call) in try/except, so a raise from ``_resolve_path`` would propagate
    # uncaught. Route backends are plain ``_RootAwareFilesystemBackend`` instances
    # (no WriteGuard wrapper), so read/ls/write/edit all catch the ValueError that
    # ``_resolve_path`` raises when a ..-free symlink resolves outside ``cwd``.

    def _outside_root_msg(self, key: str) -> str:
        return (
            f"Path '{key}' is outside the file-tool root '{self.cwd}'. It exists "
            f"on disk but the file tools can't reach it — read or edit it with "
            f"shell_exec, or have the operator add its directory to "
            f"MIMIR_FILE_TOOL_ROOTS."
        )

    def _path_value_error_msg(self, key: str, exc: ValueError) -> str:
        if "outside root directory" in str(exc):
            return self._outside_root_msg(key)
        return str(exc)

    def _is_outside_root(self, key: str) -> bool:
        return self._guard_outside_root and _real_path_outside_root(key, self.cwd) is not None

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if self._is_outside_root(file_path):
            return ReadResult(error=self._outside_root_msg(file_path))
        try:
            result = super().read(file_path, offset, limit)
        except ValueError as e:
            return ReadResult(error=self._path_value_error_msg(file_path, e))
        if result.error is None:
            self._publish_read_provenance(file_path)
        return result

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if self._is_outside_root(file_path):
            return ReadResult(error=self._outside_root_msg(file_path))
        result = await super().aread(file_path, offset, limit)
        if result.error is None:
            self._publish_read_provenance(file_path)
        return result

    def _publish_read_provenance(self, file_path: str) -> None:
        """Publish the path only after backend resolution and a successful read."""
        self._publish_read_paths([file_path])

    def _publish_read_paths(self, file_paths: list[str]) -> None:
        """Publish every exact resource in a successful backend collection result."""
        from ._context import get_current_turn
        from .access_control import protected_result_source, publish_protected_result

        turn = get_current_turn()
        auth_context = getattr(turn, "auth_context", None)
        resolved_paths = []
        for file_path in dict.fromkeys(file_paths):
            try:
                resolved_paths.append(str(self._resolve_path(file_path).resolve(strict=True)))
            except (OSError, RuntimeError, ValueError):
                return
        publish_protected_result(tuple(
            protected_result_source(
                auth_context,
                principal="filesystem",
                domain="filesystem",
                resource_id=resolved,
                bridge_instance="filesystem",
            )
            for resolved in resolved_paths
        ))

    def ls(self, path: str) -> LsResult:
        if self._is_outside_root(path):
            return LsResult(error=self._outside_root_msg(path))
        try:
            result = super().ls(path)
        except ValueError as e:
            return LsResult(error=self._path_value_error_msg(path, e))
        if result.error is None:
            self._publish_read_paths([
                str(entry.get("path"))
                for entry in result.entries or ()
                if entry.get("path")
            ])
        return result

    async def als(self, path: str) -> LsResult:
        if self._is_outside_root(path):
            return LsResult(error=self._outside_root_msg(path))
        result = await super().als(path)
        if result.error is None:
            self._publish_read_paths([
                str(entry.get("path"))
                for entry in result.entries or ()
                if entry.get("path")
            ])
        return result

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        result = super().glob(pattern, path)
        if result.error is None:
            self._publish_read_paths([
                str(match.get("path"))
                for match in result.matches or ()
                if match.get("path")
            ])
        return result

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        result = await super().aglob(pattern, path)
        if result.error is None:
            self._publish_read_paths([
                str(match.get("path"))
                for match in result.matches or ()
                if match.get("path")
            ])
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        result = super().grep(pattern, path, glob)
        if result.error is None:
            self._publish_read_paths([
                str(match.get("path"))
                for match in result.matches or ()
                if match.get("path")
            ])
        return result

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        result = await super().agrep(pattern, path, glob)
        if result.error is None:
            self._publish_read_paths([
                str(match.get("path"))
                for match in result.matches or ()
                if match.get("path")
            ])
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            return super().write(file_path, content)
        except ValueError as e:
            return WriteResult(error=self._path_value_error_msg(file_path, e))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await super().awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        try:
            return super().edit(file_path, old_string, new_string, replace_all)
        except ValueError as e:
            return EditResult(error=self._path_value_error_msg(file_path, e))

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return await super().aedit(file_path, old_string, new_string, replace_all)


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
        guard_outside_root: bool = False,
    ) -> None:
        self._root = Path(root_dir).resolve()
        self._fs = _RootAwareFilesystemBackend(
            root_dir=root_dir, virtual_mode=True, guard_outside_root=guard_outside_root,
        )
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
        # Pre-resolved state/identities.yaml — the auth identity + role registry.
        # The agent must NEVER edit this via its file tools: a prompt-injected
        # chat user could otherwise talk the agent into adding an ``admin`` role
        # alias for themselves (privilege escalation, now that web RBAC gates
        # saga/memory/ops/admin on the admin role). Legitimate changes go through
        # the server-side admin Users UI (``issue_web_key``/``revoke_web_key``,
        # which write the file directly — not through this backend), so key
        # issuance is unaffected. Unconditional deny: there is no legitimate
        # agent-tool write to this file.
        self._identities_path: Path = (self._root / "state" / "identities.yaml").resolve()
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
        # Bound the buffer: denials with no turn_id (setup/non-turn
        # callables) and denials from turns that died before the post-turn
        # drain are never drained by the turn-scoped path — drop oldest so
        # they can't accumulate for process lifetime.
        if len(self._denials) > 512:
            del self._denials[: len(self._denials) - 512]

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

    def _is_identities_write_blocked(self, file_path: str) -> bool:
        """True iff this write targets ``state/identities.yaml`` — the auth
        identity + role registry. Blocked UNCONDITIONALLY for the agent's file
        tools (no turn-context exception, unlike core memory) to prevent
        prompt-injection privilege escalation: a chat user must not be able to
        talk the agent into editing the file that decides who is an admin. The
        admin Users UI mutates identities server-side (not through this
        backend), so legitimate web-key issuance/revocation is unaffected."""
        resolved = self._resolve_target(file_path)
        if resolved is None:
            return False
        return resolved == self._identities_path

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
        "operator reviews and merges the PR. For a non-diff suggestion, file a "
        "Chainlink issue or state/spec note."
    )

    _PROMPTS_DENY_REASON = (
        "Write blocked: prompts/ is operator-managed and not writable at "
        "runtime. To change a prompt template, open a proposal with "
        "open_proposal, edit it there, then submit_proposal — the operator "
        "reviews and merges the PR."
    )

    _IDENTITIES_DENY_REASON = (
        "Write blocked: state/identities.yaml is the auth identity + role "
        "registry (it governs who is an admin) and is not editable via file "
        "tools, by policy. Manage users and roles through the operator-only "
        "admin Users UI / web-key endpoints — not by editing this file."
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
        if self._is_identities_write_blocked(file_path):
            self._record_denial("write_identities_protected", file_path)
            return WriteResult(error=self._IDENTITIES_DENY_REASON)
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
        if self._is_identities_write_blocked(file_path):
            self._record_denial("edit_identities_protected", file_path)
            return EditResult(error=self._IDENTITIES_DENY_REASON)
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
        identities_blocked = {p for p, _ in files if self._is_identities_write_blocked(p)}
        if blocked_paths or core_blocked or identities_blocked:
            for p in blocked_paths:
                self._record_denial("upload", p)
            for p in core_blocked - blocked_paths:
                self._record_denial("upload_core_memory_readonly", p)
            for p in identities_blocked - blocked_paths - core_blocked:
                self._record_denial("upload_identities_protected", p)
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


def build_file_tool_routes(roots: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Build ``CompositeBackend`` routes from validated ``(abs_path, mode)`` pairs
    (chainlink #650).

    ``rw`` -> a real-FS backend rooted at the path (reads + writes hit real
    disk); ``ro`` -> the same wrapped read-only. ``CompositeBackend`` strips the
    route prefix before delegating, so each backend resolves the remainder under
    its own root. Route keys are the absolute path with a trailing ``/`` (the
    boundary form CompositeBackend matches on)."""
    routes: dict[str, Any] = {}
    for path, mode in roots:
        prefix = str(Path(path)).rstrip("/") + "/"
        if mode == "ro":
            routes[prefix] = ReadOnlyFilesystemBackend(Path(path))
        else:
            routes[prefix] = _RootAwareFilesystemBackend(root_dir=Path(path), virtual_mode=True)
    return routes


class FileToolRouter(CompositeBackend):
    """``CompositeBackend`` that also forwards mimir's ``drain_denials`` to the
    default (home) backend.

    The write-guard records permission denials on the home ``WriteGuardBackend``;
    ``Agent.run_turn`` drains them for the turn's audit trail. A bare
    ``CompositeBackend`` has no ``drain_denials``, so wrapping the home backend
    in routes would silently drop that trail — this preserves it (chainlink #650)."""

    def drain_denials(self, turn_id: str | None = None) -> list[dict[str, Any]]:
        drain = getattr(self.default, "drain_denials", None)
        if callable(drain):
            return drain(turn_id=turn_id)
        return []
