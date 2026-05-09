"""Wiki backlinks tool — derive inbound-link reports from ``[[wikilinks]]``.

Walks ``<home>/state/wiki/**/*.md``, extracts ``[[page-name]]``-style
wikilinks, and writes three derived reports:

- ``state/wiki/orphans.md`` — pages with zero inbound links, grouped by
  category (concepts/topics/entities). Actionable list for lint passes.
- ``state/wiki/dangling-links.md`` — ``[[targets]]`` referenced in
  pages but no matching page exists, grouped by source file with line
  numbers.
- ``state/wiki/backlinks-index.md`` — full inbound map, one section per
  page. Grep ``## <slug>`` to find what links to a specific page.

All three are regenerated each run with a ``_Generated <ts>_`` header
— no partial updates, no diff noise from manual edits, no risk of
clobbering the agent's own page edits.

When the wiki has any orphans or dangling links, emits a
``wiki_backlinks_unhealthy`` event so the algedonic feedback block
surfaces "wiki health regressed" without needing the agent to run
lint explicitly. A clean wiki emits no event (no signal, no spam).

Wikilink resolution: ``[[stigmergy]]`` matches any ``stigmergy.md``
under ``state/wiki/`` regardless of category. ``[[name|display]]``
strips the display half. ``[[name#heading]]`` and ``[[name^block]]``
strip the heading/block locator. Case is preserved (no fuzzy matching).
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
from typing import Iterable

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


def find_pages(wiki_dir: Path) -> dict[str, Path]:
    """Return ``slug → relative-path-under-wiki-dir`` for every content
    page under ``wiki_dir``.

    Slug is the filename sans ``.md``. Multiple files with the same
    slug across categories — last-wins; the dangling-link detection
    still works correctly because both files contribute their outbound
    links, but inbound-link grouping conflates them. (Genuine slug
    collisions are a wiki-health signal we'd want to surface
    eventually, but Phase 1 doesn't.)
    """
    pages: dict[str, Path] = {}
    if not wiki_dir.is_dir():
        return pages
    for md in sorted(wiki_dir.rglob("*.md")):
        if md.name in _META_FILENAMES:
            continue
        slug = md.stem
        pages[slug] = md.relative_to(wiki_dir)
    return pages


def extract_links(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, target_slug)`` for each wikilink in
    ``text``. Line numbers are 1-indexed.

    Normalizes ``[[name.md]]`` → ``name`` since per Obsidian convention
    both forms are equivalent (https://help.obsidian.md/Linking+notes).
    Without this, a page that uses the explicit-extension form would
    show up as a dangling link to the literal ``"name.md"`` slug while
    the real ``"name"`` page would still be flagged as orphan."""
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _WIKILINK_RE.finditer(line):
            target = m.group(1).strip()
            if target.endswith(".md"):
                target = target[:-3]
            yield line_no, target


class BacklinksGraph:
    """Result of walking a wiki directory.

    ``pages`` maps slug → ``{path, outbound, inbound}``; ``orphans``
    is the slug list with empty inbound; ``dangling`` is the list of
    ``{target, source, line}`` for links that don't resolve to any
    page. All three derive from one walk."""

    def __init__(
        self,
        pages: dict[str, dict],
        orphans: list[str],
        dangling: list[dict],
    ) -> None:
        self.pages = pages
        self.orphans = orphans
        self.dangling = dangling


def build_graph(wiki_dir: Path) -> BacklinksGraph:
    """Walk ``wiki_dir``, return the inbound + outbound + dangling
    structure described in the module docstring."""
    pages_paths = find_pages(wiki_dir)
    page_data: dict[str, dict] = {}
    inbound: defaultdict[str, set[str]] = defaultdict(set)
    dangling: list[dict] = []

    for slug, rel_path in pages_paths.items():
        full_path = wiki_dir / rel_path
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        outbound: list[str] = []
        for line_no, target in extract_links(text):
            outbound.append(target)
            if target in pages_paths:
                # Self-links don't count as inbound (a page linking to
                # itself isn't "supported by another page"). Pre-fix
                # wikis sometimes had these as a stylistic choice —
                # exclude either way.
                if target != slug:
                    inbound[target].add(slug)
            else:
                dangling.append({
                    "target": target,
                    "source": str(rel_path),
                    "line": line_no,
                })
        page_data[slug] = {
            "path": str(rel_path),
            "outbound": sorted(set(outbound)),
        }

    for slug in page_data:
        page_data[slug]["inbound"] = sorted(inbound.get(slug, set()))

    orphans = sorted([s for s, d in page_data.items() if not d["inbound"]])

    return BacklinksGraph(
        pages=page_data, orphans=orphans, dangling=dangling,
    )


# ─── Renderers ──────────────────────────────────────────────────────


_GENERATED_HEADER = (
    "_Generated {ts} by `mimir wiki backlinks` — regenerated on each "
    "run; don't hand-edit (your changes will be overwritten)._"
)


def render_orphans_md(graph: BacklinksGraph, generated_at: str) -> str:
    by_category: defaultdict[str, list[str]] = defaultdict(list)
    for slug in graph.orphans:
        rel = Path(graph.pages[slug]["path"])
        by_category[_category_of(rel)].append(slug)

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
        slugs = sorted(by_category[category])
        lines.append(f"## {category} ({len(slugs)})")
        lines.append("")
        for slug in slugs:
            lines.append(f"- [[{slug}]] — `{graph.pages[slug]['path']}`")
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
        "`[[wikilinks]]`. Grep `## <slug>` to find what links to a "
        "specific page.",
        "",
    ]
    for slug in sorted(graph.pages):
        data = graph.pages[slug]
        lines.append(f"## {slug}")
        lines.append(f"_path:_ `{data['path']}`")
        lines.append("")
        if data["inbound"]:
            for source in data["inbound"]:
                source_path = graph.pages[source]["path"]
                lines.append(f"- [[{source}]] — `{source_path}`")
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
    init_logger(cfg.events_log, session_id="wiki-backlinks")

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
