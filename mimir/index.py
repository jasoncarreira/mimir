"""Auto-generated INDEX.md files (SPEC §3.4).

Two indexes:
- ``memory/INDEX.md`` — non-core memory files (excludes ``memory/core/`` and self).
  This one renders into the system prompt every turn.
- ``state/INDEX.md`` — bulk state files. NOT in the prompt; the agent reads
  it on demand via ``read_file`` or finds files via ``file_search`` (Phase 3).

Both rebuild end-of-turn (debounced — N writes in one turn collapse to one
tree walk) plus a 60s sweep for out-of-band edits. Files without an explicit
``<!-- desc: -->`` comment render with an ``[auto]`` prefix so the agent
sees its own omissions in the next turn's prompt and can self-correct.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from .memory import describe_file

log = logging.getLogger(__name__)


@dataclass
class IndexEntry:
    rel_path: str
    description: str
    is_auto: bool


def _walk_tree(root: Path, exclude_names: set[str]) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        if path.name in exclude_names:
            continue
        # Exclude any path that has a segment in exclude_names (e.g. memory/core).
        if any(part in exclude_names for part in path.relative_to(root).parts):
            continue
        out.append(path)
    return sorted(out)


def _build_entries(root: Path, files: list[Path]) -> list[IndexEntry]:
    entries: list[IndexEntry] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        desc, is_auto = describe_file(text)
        rel = path.relative_to(root).as_posix()
        entries.append(IndexEntry(rel_path=rel, description=desc, is_auto=is_auto))
    return entries


def render_memory_index(entries: list[IndexEntry]) -> str:
    """Render the memory/INDEX.md body."""
    header = (
        "# Memory Index\n\n"
        "Files under memory/ that aren't in core/. Read directly with "
        "`read_file` if you know the path; use `file_search` to find by topic.\n\n"
    )
    if not entries:
        return header + "(none)\n"
    lines: list[str] = []
    for e in entries:
        prefix = "[auto] " if e.is_auto else ""
        body = e.description or "(no description)"
        lines.append(f"- {e.rel_path} — {prefix}{body}")
    return header + "\n".join(lines) + "\n"


def render_state_index(entries: list[IndexEntry]) -> str:
    """Render the state/INDEX.md body."""
    header = (
        "# State Index\n\n"
        "Verbatim bulk content. Read directly with `read_file` if you know "
        "the path; use `file_search` to find by topic.\n\n"
    )
    if not entries:
        return header + "(none)\n"
    lines: list[str] = []
    for e in entries:
        prefix = "[auto] " if e.is_auto else ""
        body = e.description or "(no description)"
        lines.append(f"- {e.rel_path} — {prefix}{body}")
    return header + "\n".join(lines) + "\n"


def build_memory_index(home: Path) -> str:
    memory_root = home / "memory"
    files = _walk_tree(memory_root, exclude_names={"core", "INDEX.md"})
    entries = _build_entries(memory_root, files)
    return render_memory_index(entries)


def build_state_index(home: Path) -> str:
    """Render state/INDEX.md. Mirrors search.py's exclusion rules so the
    listed files match what file_search can actually find — listing a
    file in INDEX.md while the indexer skips it is a misleading promise
    to the agent."""
    from .search import INDEX_SKIP_PATHS, INDEX_SKIP_PREFIXES

    state_root = home / "state"
    files = _walk_tree(state_root, exclude_names={"INDEX.md"})
    files = [
        p for p in files
        if (rel := f"state/{p.relative_to(state_root).as_posix()}")
        not in INDEX_SKIP_PATHS
        and not any(rel.startswith(prefix) for prefix in INDEX_SKIP_PREFIXES)
    ]
    entries = _build_entries(state_root, files)
    return render_state_index(entries)


class IndexGenerator:
    """End-of-turn debounced rebuilder. Call ``mark_dirty()`` from any write
    path; call ``flush()`` from the agent loop after a turn completes.
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._dirty_memory = False
        self._dirty_state = False
        self._lock = asyncio.Lock()

    def mark_dirty(self, scope: str = "all") -> None:
        if scope in ("memory", "all"):
            self._dirty_memory = True
        if scope in ("state", "all"):
            self._dirty_state = True

    async def flush(self) -> None:
        """Write any indexes marked dirty. Returns immediately if nothing dirty."""
        async with self._lock:
            if self._dirty_memory:
                await asyncio.to_thread(self._write_memory)
                self._dirty_memory = False
            if self._dirty_state:
                await asyncio.to_thread(self._write_state)
                self._dirty_state = False

    def _write_memory(self) -> None:
        path = self._home / "memory" / "INDEX.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_memory_index(self._home)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)

    def _write_state(self) -> None:
        path = self._home / "state" / "INDEX.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_state_index(self._home)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)

    def read_memory_index(self) -> str:
        """Return the current memory/INDEX.md body, generating in-memory if missing."""
        path = self._home / "memory" / "INDEX.md"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
        return build_memory_index(self._home)
