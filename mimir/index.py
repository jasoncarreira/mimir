"""Auto-generated INDEX.md files (SPEC §3.4).

Three indexes:
- ``memory/INDEX.md`` — non-core memory files (excludes ``memory/core/`` and self).
  This one renders into the system prompt every turn.
- ``state/INDEX.md`` — bulk state files. NOT in the prompt; the agent reads
  it on demand via ``read_file`` or finds files via ``file_search`` (Phase 3).
- ``state/wiki/index.md`` — section-grouped catalog of wiki pages
  (entities/, concepts/, topics/). NOT in the prompt; surfaces the wiki
  layout for browse, with each entry's desc-comment as its description.

All three rebuild end-of-turn (debounced — N writes in one turn collapse
to one tree walk per dirty scope). Files without an explicit
``<!-- desc: -->`` comment render with an ``[auto]`` prefix so the agent
sees its own omissions in the next turn's prompt and can self-correct.
The wiki regen replaces the previously hand-curated wiki/index.md
(chainlink #31 #38) — hand-edits are overwritten same way the other two
indexes already worked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from .core_blocks import describe_file

log = logging.getLogger(__name__)


@dataclass
class IndexEntry:
    rel_path: str
    description: str
    is_auto: bool
    is_core: bool = False


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
        is_core = rel.startswith("core/")
        entries.append(IndexEntry(
            rel_path=rel, description=desc, is_auto=is_auto, is_core=is_core,
        ))
    return entries


def render_memory_index(entries: list[IndexEntry]) -> str:
    """Render the memory/INDEX.md body.

    Core blocks are listed with a ``[core]`` tag. They're already
    inlined in every system prompt under ``## Core memory``, but the
    index needs to know they exist so the agent can edit them when
    it wants to (the index is the agent's "where do I edit X?" map)."""
    header = (
        "# Memory Index\n\n"
        "All files under memory/. Core blocks are tagged ``[core]`` — "
        "they're inlined in every system prompt under ``## Core memory`` "
        "but you can ``read_file`` and edit them like any other memory "
        "file. Read directly with ``read_file`` if you know the path; "
        "use ``file_search`` to find non-core files by topic (file_search "
        "skips core because it's already in the prompt).\n\n"
    )
    if not entries:
        return header + "(none)\n"
    # Core first (numeric prefix order), then non-core. Within each
    # group preserve the lexicographic order from the walker.
    core_entries = [e for e in entries if e.is_core]
    other_entries = [e for e in entries if not e.is_core]
    lines: list[str] = []
    for e in core_entries:
        prefix = "[auto] " if e.is_auto else ""
        body = e.description or "(no description)"
        lines.append(f"- {e.rel_path} `[core]` — {prefix}{body}")
    if core_entries and other_entries:
        lines.append("")
    for e in other_entries:
        prefix = "[auto] " if e.is_auto else ""
        body = e.description or "(no description)"
        lines.append(f"- {e.rel_path} — {prefix}{body}")
    return header + "\n".join(lines) + "\n"


# Section-grouped wiki layout. Order matters — Entities first (the
# named referents), then Concepts (ideas), then Topics (long-form
# writeups). Mirrors the existing hand-curated wiki/index.md shape so
# the auto-regen is a drop-in replacement.
_WIKI_SECTIONS: tuple[tuple[str, str], ...] = (
    ("entities", "Entities"),
    ("concepts", "Concepts"),
    ("topics", "Topics"),
)


def render_wiki_index(entries_by_section: dict[str, list[IndexEntry]]) -> str:
    r"""Render state/wiki/index.md as section-grouped catalog
    (chainlink #31 #38). Each section lists pages from its subdir
    in lexicographic order; per-page format is the wiki-style
    ``[[link]] — \`<rel>\` — <desc>`` shape.

    Sections with no pages are omitted from output (no empty
    ``## Concepts`` headers cluttering the file when a fresh agent
    has no concept pages yet)."""
    header = (
        "<!-- desc: catalog of wiki pages — directory placement implies type -->\n"
        "# Wiki Index\n\n"
        "Catalog of wiki pages. Directory placement is the source of truth "
        "for type (`entities/`, `concepts/`, `topics/`); this file groups "
        "them for quick browse. Auto-regenerated end-of-turn from each "
        "page's `<!-- desc: -->` first-line comment — hand-edits are "
        "overwritten.\n"
    )
    sections_rendered: list[str] = []
    for subdir, label in _WIKI_SECTIONS:
        section_entries = entries_by_section.get(subdir, [])
        if not section_entries:
            continue
        lines = [f"\n## {label}\n"]
        for e in section_entries:
            # rel_path is wiki-relative, e.g. "concepts/foo.md"
            stem = Path(e.rel_path).stem
            prefix = "[auto] " if e.is_auto else ""
            body = e.description or "(no description)"
            lines.append(f"- [[{stem}]] — `{e.rel_path}` — {prefix}{body}")
        sections_rendered.append("\n".join(lines) + "\n")

    if not sections_rendered:
        return header + "\n(no pages yet)\n"
    return header + "".join(sections_rendered)


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
    # Core files ARE included in the memory index now (so the agent
    # knows where to edit them). They're still skipped by ``file_search``
    # because they're already inlined in the system prompt.
    files = _walk_tree(memory_root, exclude_names={"INDEX.md"})
    entries = _build_entries(memory_root, files)
    return render_memory_index(entries)


def build_wiki_index(home: Path) -> str:
    """Render state/wiki/index.md (chainlink #31 #38). Walks each
    section subdir under state/wiki/ and groups pages by section.
    Skips meta files at the wiki root (AGENTS.md, index.md, log.md)
    — those are documentation about the wiki, not catalog entries."""
    wiki_root = home / "state" / "wiki"
    by_section: dict[str, list[IndexEntry]] = {}
    for subdir, _label in _WIKI_SECTIONS:
        section_root = wiki_root / subdir
        files = _walk_tree(section_root, exclude_names={"INDEX.md", "index.md"})
        # Build entries with section-relative rel_path (e.g.
        # "concepts/foo.md"), so the rendered link is wiki-relative
        # rather than section-relative.
        entries: list[IndexEntry] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            desc, is_auto = describe_file(text)
            rel = f"{subdir}/{path.relative_to(section_root).as_posix()}"
            entries.append(IndexEntry(
                rel_path=rel, description=desc, is_auto=is_auto, is_core=False,
            ))
        if entries:
            by_section[subdir] = entries
    return render_wiki_index(by_section)


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

    ``skills_root`` is the directory containing one sub-directory per skill,
    each with a ``SKILL.md`` file.  When ``None`` (default) the bundled
    ``mimir/skills/`` directory is used (via ``skill_catalog.DEFAULT_SKILLS_ROOT``).
    The memory flush path regenerates ``memory/skills-catalog.md`` if its
    content drifts from a fresh generation — so any SKILL.md edit is picked
    up at the next memory rebuild rather than only when the operator manually
    runs ``mimir skills catalog`` (chainlink #109).
    """

    def __init__(self, home: Path, skills_root: Path | None = None) -> None:
        self._home = home
        self._skills_root = skills_root  # None → defer to DEFAULT_SKILLS_ROOT
        self._dirty_memory = False
        self._dirty_state = False
        self._dirty_wiki = False
        self._lock = asyncio.Lock()

    def mark_dirty(self, scope: str = "all") -> None:
        if scope in ("memory", "all"):
            self._dirty_memory = True
        if scope in ("state", "all"):
            self._dirty_state = True
        if scope in ("wiki", "all"):
            self._dirty_wiki = True

    async def flush(self) -> None:
        """Write any indexes marked dirty. Returns immediately if nothing dirty."""
        async with self._lock:
            if self._dirty_memory:
                await asyncio.to_thread(self._write_memory)
                self._dirty_memory = False
            if self._dirty_state:
                await asyncio.to_thread(self._write_state)
                self._dirty_state = False
            if self._dirty_wiki:
                await asyncio.to_thread(self._write_wiki)
                self._dirty_wiki = False

    def _write_memory(self) -> None:
        path = self._home / "memory" / "INDEX.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_memory_index(self._home)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        # Regenerate skills-catalog.md if drift detected (chainlink #109).
        # A diff check prevents unnecessary writes when nothing changed.
        self._refresh_skills_catalog()

    def _refresh_skills_catalog(self) -> None:
        """Write ``memory/skills-catalog.md`` if it differs from a fresh
        generation.  Silent on errors — catalog drift is recoverable; a
        write failure here must not crash the index flush."""
        catalog_path = self._home / "memory" / "skills-catalog.md"
        if not catalog_path.parent.is_dir():
            return
        from .skill_catalog import generate as _gen_catalog  # local import: keeps cli callers thin
        try:
            fresh = _gen_catalog(self._skills_root)
        except Exception:  # noqa: BLE001 — defensive; catalog regen is best-effort
            log.warning("skills-catalog.md regeneration failed", exc_info=True)
            return
        existing: str = ""
        if catalog_path.is_file():
            try:
                existing = catalog_path.read_text(encoding="utf-8")
            except OSError:
                pass  # treat missing/unreadable as empty → always write
        if fresh == existing:
            return  # already current; skip the write
        tmp = catalog_path.with_suffix(".md.tmp")
        try:
            tmp.write_text(fresh, encoding="utf-8")
            tmp.replace(catalog_path)
        except OSError:
            log.warning("failed to write skills-catalog.md", exc_info=True)

    def _write_state(self) -> None:
        path = self._home / "state" / "INDEX.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_state_index(self._home)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)

    def _write_wiki(self) -> None:
        # Lowercase index.md (not INDEX.md) — the wiki convention pre-dates
        # the auto-regen and the existing hand-curated file uses lowercase.
        # Keeping the filename matches the wiki/AGENTS.md doc + every page's
        # link reference.
        path = self._home / "state" / "wiki" / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = build_wiki_index(self._home)
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
