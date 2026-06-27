"""Wiki backlinks tool — derive inbound-link reports from ``[[wikilinks]]``.

Walks ``<home>/state/wiki/**/*.md``, extracts ``[[page-name]]``-style
wikilinks, and writes three derived reports:

- ``state/wiki/orphans.md`` — pages with zero inbound links, grouped by
  category (concepts/topics/entities). Actionable list for lint passes.
- ``state/wiki/dangling-links.md`` — ``[[targets]]`` referenced in
  pages but no matching page exists, grouped by source file with line
  numbers.
- ``state/wiki/backlinks-index.md`` — full inbound map, one section per
  page. Grep ``## <path>`` to find what links to a specific page.

All three are regenerated each run with a ``_Generated <ts>_`` header
— no partial updates, no diff noise from manual edits, no risk of
clobbering the agent's own page edits.

When the wiki has any orphans or dangling links, emits a
``wiki_backlinks_unhealthy`` event so the algedonic feedback block
surfaces "wiki health regressed" without needing the agent to run
lint explicitly. A clean wiki emits no event (no signal, no spam).

Wikilink resolution: ``[[stigmergy]]`` matches every ``stigmergy.md``
under ``state/wiki/`` regardless of category — so cross-category
collisions (``concepts/foo.md`` + ``topics/foo.md``) each receive
the inbound link rather than silently conflating. Each colliding
slug is also surfaced via ``wiki_slug_collision`` so the operator
can rename one of the pair. ``[[name|display]]`` strips the display
half. ``[[name#heading]]`` and ``[[name^block]]`` strip the
heading/block locator. Case is preserved (no fuzzy matching).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .event_logger import init_logger, log_event


# ``[[target]]`` — capture target only, strip optional |display, #heading,
# ^block locators. Tolerates whitespace inside the brackets.
_WIKILINK_RE = re.compile(
    r"\[\[\s*([^\]\|#^]+?)\s*(?:[#^][^\]\|]*)?(?:\|[^\]]*)?\]\]"
)

# Wiki meta files — exclude from the page set so they don't show up as
# orphans (they're not content; they're catalogs). The three derived
# output files are also excluded so they don't pollute their own next
# run.
_META_FILENAMES = frozenset({
    "AGENTS.md",
    "index.md",
    "log.md",
    "orphans.md",
    "dangling-links.md",
    "backlinks-index.md",
    "unwired.md",  # llm-wiki skill's priority list
})


def _category_of(rel_path: Path) -> str:
    """First path component (concepts / topics / entities) — or
    ``"_root"`` for pages directly under ``state/wiki/``."""
    parts = rel_path.parts
    return parts[0] if len(parts) > 1 else "_root"


def _posix(rel_path: Path) -> str:
    """Render a relative ``Path`` as a POSIX-separated string. The
    backlinks index uses path strings as stable map keys; mixing
    backslashes on Windows runs would split a single logical page
    into two entries."""
    return rel_path.as_posix()


def _title_from_markdown(text: str, fallback_slug: str) -> str:
    """Return the first H1 title from markdown, falling back to the slug.

    The dashboard-facing payload should be useful without rendering the
    full page body. Wiki pages conventionally start with ``# Title``; if
    they do not, keep the stable slug rather than guessing a prettified
    title that could obscure the underlying filename.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title
    return fallback_slug


def find_pages(wiki_dir: Path) -> dict[str, Path]:
    """Return ``path-str → relative-path`` for every content page
    under ``wiki_dir``.

    Path-keyed: each markdown file has its own unique entry, so
    cross-category same-stem files (``concepts/foo.md`` AND
    ``topics/foo.md``) are both tracked. Wikilink resolution lives
    in ``_build_slug_index`` instead, which is what actually has
    to handle stem collisions.
    """
    pages: dict[str, Path] = {}
    if not wiki_dir.is_dir():
        return pages
    for md in sorted(wiki_dir.rglob("*.md")):
        if md.name in _META_FILENAMES:
            continue
        rel = md.relative_to(wiki_dir)
        pages[_posix(rel)] = rel
    return pages


def _build_slug_index(
    pages_paths: dict[str, Path],
) -> dict[str, list[str]]:
    """Return ``slug → [path_strs]`` from a path-keyed page map. A
    slug with a single path is the common case; multi-path slugs are
    collisions that we resolve to all matches and surface separately."""
    by_slug: dict[str, list[str]] = {}
    for path_str, rel in pages_paths.items():
        by_slug.setdefault(rel.stem, []).append(path_str)
    return by_slug


def find_slug_collisions(wiki_dir: Path) -> dict[str, list[Path]]:
    """Return ``slug → [paths]`` for every slug that resolves to more
    than one file under ``wiki_dir``.

    Post-path-key refactor, ``build_graph`` resolves wikilinks
    correctly across collisions (both pages get the inbound link).
    The collision report still matters as a wiki-health signal —
    ``[[foo]]`` is ambiguous to a human reader when there are two
    ``foo.md`` pages, even if backlink accounting now handles it.
    """
    by_slug: dict[str, list[Path]] = {}
    if not wiki_dir.is_dir():
        return {}
    for md in sorted(wiki_dir.rglob("*.md")):
        if md.name in _META_FILENAMES:
            continue
        by_slug.setdefault(md.stem, []).append(md.relative_to(wiki_dir))
    return {slug: paths for slug, paths in by_slug.items() if len(paths) > 1}


#: Category prefixes documented in ``mimir/skills/wiki/SKILL.md``. A
#: wikilink that includes one — ``[[concepts/foo]]`` — refers to the
#: same page as the bare ``[[foo]]`` (resolution goes by slug, not
#: path; see ``BacklinksGraph._resolve``). Without normalization here,
#: the prefixed form gets recorded as a dangling link to the literal
#: ``"concepts/foo"`` slug while the actual ``"foo"`` page gets
#: falsely flagged as orphan — exactly the case muninn-mimir's
#: 2026-05-23 wiki-health report surfaced.
#:
#: Matching is case-sensitive — Obsidian wikilinks are case-sensitive,
#: so ``[[Concepts/foo]]`` won't be stripped. Acceptable: the wiki
#: convention is lowercase category dirs.
# Keep in sync with mimir/skills/wiki/SKILL.md §Layout — adding a new
# category subdir there (e.g. ``projects/``) needs a matching entry here.
_CATEGORY_PREFIXES: tuple[str, ...] = ("concepts/", "topics/", "entities/")


def extract_links(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, target_slug)`` for each wikilink in
    ``text``. Line numbers are 1-indexed.

    Normalizations applied to each target:

    - ``[[name.md]]`` → ``name`` (Obsidian explicit-extension form;
      see https://help.obsidian.md/Linking+notes).
    - ``[[concepts/name]]`` → ``name`` (and ``topics/``, ``entities/``).
      The wiki layout under ``mimir/skills/wiki/SKILL.md`` puts pages
      under category subdirs, but slug-based resolution treats them
      as equivalent to the bare form.
    """
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _WIKILINK_RE.finditer(line):
            target = m.group(1).strip()
            if target.endswith(".md"):
                target = target[:-3]
            for prefix in _CATEGORY_PREFIXES:
                if target.startswith(prefix):
                    target = target[len(prefix):]
                    break
            yield line_no, target


class BacklinksGraph:
    """Result of walking a wiki directory.

    ``pages`` maps path-string → ``{slug, path, outbound, inbound}``;
    ``orphans`` is the list of path-strings with empty inbound;
    ``dangling`` is the list of ``{target, source, line}`` for links
    that don't resolve to any page; ``collisions`` maps colliding
    slug → list of path-strings (``run()`` emits the algedonic
    ``wiki_slug_collision`` event from this). All four derive from
    one walk.

    Path-keyed (PR after #112): ``pages`` is keyed by relative path
    string (POSIX), not slug. ``outbound`` entries are target slugs
    (the wikilink form); ``inbound`` entries are source path-strings
    (unambiguous, no conflation under collision)."""

    def __init__(
        self,
        pages: dict[str, dict],
        orphans: list[str],
        dangling: list[dict],
        collisions: dict[str, list[Path]] | None = None,
    ) -> None:
        self.pages = pages
        self.orphans = orphans
        self.dangling = dangling
        self.collisions = collisions or {}


def build_graph(wiki_dir: Path) -> BacklinksGraph:
    """Walk ``wiki_dir``, return the path-keyed inbound + outbound +
    dangling + collisions structure described in the module docstring.

    **Path-keyed resolution.** ``[[foo]]`` resolves via a slug index
    to every ``foo.md`` page under ``wiki_dir``. Each resolved target
    (other than the source page itself) receives an inbound entry.
    Pre-refactor the slug-keyed map silently conflated collisions
    and a cross-category same-stem link looked like a self-link;
    now both pages see the link and the collision is also surfaced
    separately so the operator can rename.
    """
    pages_paths = find_pages(wiki_dir)
    slug_index = _build_slug_index(pages_paths)
    page_data: dict[str, dict] = {}
    inbound: defaultdict[str, set[str]] = defaultdict(set)
    dangling: list[dict] = []

    # Re-derive collisions from the slug index instead of a second
    # filesystem scan — same result, half the IO.
    collisions: dict[str, list[Path]] = {
        slug: [pages_paths[p] for p in paths]
        for slug, paths in slug_index.items()
        if len(paths) > 1
    }

    for source_path, rel_path in pages_paths.items():
        full_path = wiki_dir / rel_path
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        outbound: list[str] = []
        for line_no, target in extract_links(text):
            outbound.append(target)
            resolved = slug_index.get(target, [])
            if not resolved:
                dangling.append({
                    "target": target,
                    "source": source_path,
                    "line": line_no,
                })
                continue
            for target_path in resolved:
                # Self-link check is now path-based: a page linking to
                # its own path doesn't count, but ``concepts/foo.md``
                # linking ``[[foo]]`` correctly registers an inbound
                # on ``topics/foo.md`` (the slug-based check pre-fix
                # would have dropped this as a self-link).
                if target_path != source_path:
                    inbound[target_path].add(source_path)
        page_data[source_path] = {
            "slug": rel_path.stem,
            "path": source_path,
            "outbound": sorted(set(outbound)),
        }

    for path_str in page_data:
        page_data[path_str]["inbound"] = sorted(inbound.get(path_str, set()))

    orphans = sorted([p for p, d in page_data.items() if not d["inbound"]])

    return BacklinksGraph(
        pages=page_data, orphans=orphans, dangling=dangling,
        collisions=collisions,
    )


def build_wiki_payload(wiki_dir: Path) -> dict[str, Any]:
    """Return a JSON-friendly read-only wiki index + graph payload.

    This is the non-mutating dashboard/API path for the wiki viewer. It
    reuses ``build_graph`` for the canonical page discovery, wikilink
    resolution, generated-report exclusion, orphan detection, dangling
    links, and slug-collision semantics, but it does not write markdown
    reports and does not emit events. It is synchronous filesystem work
    by design: aiohttp route handlers should run it with
    ``asyncio.to_thread`` rather than calling it on the event loop.
    """
    graph = build_graph(wiki_dir)
    pages: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    collision_paths = {
        path_str
        for paths in graph.collisions.values()
        for path_str in (_posix(path) for path in paths)
    }

    for path_str in sorted(graph.pages):
        data = graph.pages[path_str]
        rel_path = Path(path_str)
        full_path = wiki_dir / rel_path
        try:
            stat = full_path.stat()
        except OSError:
            mtime = None
        else:
            mtime = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat(timespec="seconds")
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        title = _title_from_markdown(text, data["slug"])
        page = {
            "slug": data["slug"],
            "title": title,
            "category": _category_of(rel_path),
            "path": path_str,
            "mtime": mtime,
            "outbound": list(data["outbound"]),
            "inbound": list(data["inbound"]),
            "is_orphan": path_str in graph.orphans,
            "has_slug_collision": path_str in collision_paths,
        }
        pages.append(page)
        nodes.append({
            "id": path_str,
            "slug": page["slug"],
            "title": page["title"],
            "category": page["category"],
            "is_orphan": page["is_orphan"],
            "has_slug_collision": page["has_slug_collision"],
        })

    for target_path in sorted(graph.pages):
        target = graph.pages[target_path]
        for source_path in target["inbound"]:
            edges.append({
                "source": source_path,
                "target": target_path,
                "target_slug": target["slug"],
            })

    return {
        "page_count": len(pages),
        "pages": pages,
        "graph": {"nodes": nodes, "edges": edges},
        "orphans": list(graph.orphans),
        "dangling_links": [dict(item) for item in graph.dangling],
        "slug_collisions": {
            slug: [_posix(path) for path in paths]
            for slug, paths in sorted(graph.collisions.items())
        },
    }


# ─── Renderers ──────────────────────────────────────────────────────


_GENERATED_HEADER = (
    "_Generated {ts} by `mimir wiki backlinks` — regenerated on each "
    "run; don't hand-edit (your changes will be overwritten)._"
)


def render_orphans_md(graph: BacklinksGraph, generated_at: str) -> str:
    by_category: defaultdict[str, list[str]] = defaultdict(list)
    for path_str in graph.orphans:
        rel = Path(path_str)
        by_category[_category_of(rel)].append(path_str)

    lines = ["# Orphan Pages", "", _GENERATED_HEADER.format(ts=generated_at), ""]
    if not graph.orphans:
        lines.append(
            "(none — every page has at least one inbound `[[link]]`.)"
        )
        return "\n".join(lines) + "\n"

    lines.append(
        f"These {len(graph.orphans)} pages have no inbound `[[wikilinks]]`. "
        "Either link to them from related pages, or consider whether they "
        "should exist."
    )
    lines.append("")
    for category in sorted(by_category):
        paths = sorted(by_category[category])
        lines.append(f"## {category} ({len(paths)})")
        lines.append("")
        for path_str in paths:
            slug = graph.pages[path_str]["slug"]
            lines.append(f"- [[{slug}]] — `{path_str}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_dangling_md(graph: BacklinksGraph, generated_at: str) -> str:
    lines = [
        "# Dangling Wikilinks",
        "",
        _GENERATED_HEADER.format(ts=generated_at),
        "",
    ]
    if not graph.dangling:
        lines.append(
            "(none — every `[[link]]` resolves to an existing page.)"
        )
        return "\n".join(lines) + "\n"

    lines.append(
        f"These {len(graph.dangling)} `[[targets]]` are referenced but no "
        "matching page exists. Either create the page or fix the link."
    )
    lines.append("")

    by_source: defaultdict[str, list[dict]] = defaultdict(list)
    for d in graph.dangling:
        by_source[d["source"]].append(d)

    for source in sorted(by_source):
        entries = sorted(by_source[source], key=lambda e: (e["line"], e["target"]))
        lines.append(f"## `{source}`")
        lines.append("")
        for d in entries:
            lines.append(f"- `[[{d['target']}]]` (line {d['line']})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_backlinks_index_md(
    graph: BacklinksGraph, generated_at: str,
) -> str:
    lines = [
        "# Backlinks Index",
        "",
        _GENERATED_HEADER.format(ts=generated_at),
        "",
        "For each page, the list of pages that link to it via "
        "`[[wikilinks]]`. Grep `## <path>` to find what links to a "
        "specific page. Path-keyed so cross-category same-stem files "
        "appear as separate sections (no silent conflation).",
        "",
    ]
    for path_str in sorted(graph.pages):
        data = graph.pages[path_str]
        lines.append(f"## {path_str}")
        lines.append(f"_slug:_ `{data['slug']}`")
        lines.append("")
        if data["inbound"]:
            for source_path in data["inbound"]:
                source_slug = graph.pages[source_path]["slug"]
                lines.append(f"- [[{source_slug}]] — `{source_path}`")
        else:
            lines.append("_(orphan — no inbound links)_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ─── Driver ──────────────────────────────────────────────────────────


async def run(home: Path) -> dict:
    """Walk the wiki, write all three reports, emit the algedonic event
    if the wiki has any orphans or dangling links.

    Returns a summary dict with ``page_count``, ``orphan_count``,
    ``dangling_count`` — useful for the CLI's stdout summary."""
    wiki_dir = home / "state" / "wiki"
    if not wiki_dir.is_dir():
        raise FileNotFoundError(f"no wiki at {wiki_dir}")

    graph = build_graph(wiki_dir)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    (wiki_dir / "orphans.md").write_text(
        render_orphans_md(graph, generated_at), encoding="utf-8",
    )
    (wiki_dir / "dangling-links.md").write_text(
        render_dangling_md(graph, generated_at), encoding="utf-8",
    )
    (wiki_dir / "backlinks-index.md").write_text(
        render_backlinks_index_md(graph, generated_at), encoding="utf-8",
    )

    page_count = len(graph.pages)
    orphan_count = len(graph.orphans)
    dangling_count = len(graph.dangling)

    # Algedonic surfacing: only emit when the wiki has health issues.
    # A clean wiki emits no signal — the agent doesn't need a "still
    # clean" reminder every turn, and the firehose stays focused on
    # actionable signals.
    if orphan_count > 0 or dangling_count > 0:
        await log_event(
            "wiki_backlinks_unhealthy",
            page_count=page_count,
            orphan_count=orphan_count,
            dangling_count=dangling_count,
            generated_at=generated_at,
        )

    # PR #112 re-review fix: emit collision events through the
    # algedonic surface. Each slug-collision pair becomes a separate
    # ``wiki_slug_collision`` record so the operator can see exactly
    # which files clash without a one-shot summary message that the
    # algedonic feedback dedup would suppress on subsequent runs.
    # Post-path-key refactor backlink accounting handles collisions
    # correctly, but the ambiguity is still a wiki-health issue
    # (a human reader can't tell which ``foo.md`` ``[[foo]]`` means).
    for slug, paths in sorted(graph.collisions.items()):
        await log_event(
            "wiki_slug_collision",
            slug=slug,
            paths=[str(p) for p in paths],
            generated_at=generated_at,
        )

    return {
        "page_count": page_count,
        "orphan_count": orphan_count,
        "dangling_count": dangling_count,
        "generated_at": generated_at,
    }


# ─── CLI ─────────────────────────────────────────────────────────────


def add_argparse(p: argparse.ArgumentParser) -> None:
    """Wire flags onto ``p``. Used by the ``mimir wiki backlinks``
    subcommand."""
    p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )


def cmd_backlinks(args: argparse.Namespace) -> int:
    """``mimir wiki backlinks`` entry point. Always returns 0 — the
    algedonic event captures health regressions; non-zero exit codes
    would break operator pipelines that don't expect 'fail on
    orphans' semantics."""
    home = args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())
    home = home.resolve()
    os.environ["MIMIR_HOME"] = str(home)

    # log_event needs the global logger initialized; piggyback on the
    # bot's events.jsonl so the wiki-health signal lands in the same
    # firehose the agent's pre-message hook reads.
    from .config import Config as _Config
    cfg = _Config.from_env()
    init_logger(
        cfg.events_log, session_id="wiki-backlinks", agent_id=cfg.agent_id,
    )

    try:
        summary = asyncio.run(run(home))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"wiki backlinks: {summary['page_count']} pages, "
        f"{summary['orphan_count']} orphans, "
        f"{summary['dangling_count']} dangling"
    )
    print(f"  state/wiki/orphans.md          ← orphan pages by category")
    print(f"  state/wiki/dangling-links.md   ← references to missing pages")
    print(f"  state/wiki/backlinks-index.md  ← full inbound map")
    return 0
