"""Skill catalog generator (chainlink #81 / G5 in cluster A of chainlink #29).

Walks ``mimir/skills/<name>/SKILL.md`` (or any skills root passed in)
and produces a single markdown page that surfaces every skill's name,
description, allowed-tools, and an auto-derived trigger phrase. The
intent is a RESOLVER.md-style dispatcher — operators (and the agent
itself) get a single map of what skills exist and how to invoke them.

Wired into the ``mimir`` CLI as ``mimir skills catalog`` (writes to
the path passed in, defaulting to stdout). The catalog file lives at
``memory/skills-catalog.md`` by convention so the indexer surfaces
its ``<!-- desc: -->`` line in the every-turn ``memory/INDEX.md`` block
(tier 7 in memory/SKILL.md's visibility hierarchy) — the catalog is
high-signal-per-byte enough to earn that prompt cost as a description.

Auto-extracted trigger phrase rule:

* If the ``description:`` field contains a sentence starting with
  ``"Use when"`` or ``"Use for"`` or ``"Use this"`` etc., use that
  sentence as the trigger phrase.
* Otherwise, fall back to the first sentence of the description.

The trigger phrase is a discovery-only hint; the description itself
is what find-skills' semantic ranker keys off.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from mimir.skill_md import extract_list_field, parse_frontmatter

# Default bundled skills root, used when nothing is passed in.
DEFAULT_SKILLS_ROOT = Path(__file__).parent / "skills"

# Trigger-phrase extraction: a sentence starting with one of these
# phrases is preferred over the first sentence of the description.
_TRIGGER_PHRASES = (
    "Use when",
    "Use for",
    "Use this when",
    "Use to",
    "Use only",
    "Run when",
)

# Sentence-split heuristic: `.` / `!` / `?` followed by whitespace and a
# capital letter. Tolerant; documented failure modes (none currently
# tripped by the 29 bundled skills, verified by smoke):
#
# * Abbreviations followed by a capitalized word — ``U.S. Department``
#   would split between ``U.S.`` and ``Department``; ``e.g. When X``
#   would split between ``e.g.`` and ``When``. Safe shapes the regex
#   handles correctly: ``e.g. when X happens`` (lowercase next word),
#   ``8.5 million`` (digit next, not a letter), and end-of-sentence
#   followed by start of next (the intended split).
# * Sentences ending in ``."`` / ``.)`` (period-then-closer) — the
#   regex doesn't account for trailing quote/paren, so a sentence
#   like ``He said "Hi." Then left.`` won't split at the ``.``
#   inside the quote (acceptable — that one's an actual sentence).
#
# If a future skill's description hits a failure mode, prefer
# rewriting the description to avoid the abbreviation rather than
# growing the regex into a state machine — the catalog trigger is a
# discovery hint, not a load-bearing parse target.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


@dataclass(frozen=True)
class SkillEntry:
    """One row in the catalog."""

    name: str
    description: str
    allowed_tools: list[str]
    trigger: str


def _extract_trigger(description: str) -> str:
    """Pick a one-line trigger phrase from the description.

    Prefers a sentence starting with one of :data:`_TRIGGER_PHRASES`;
    falls back to the first sentence.
    """
    sentences = _SENTENCE_SPLIT.split(description.strip())
    for sentence in sentences:
        s = sentence.strip()
        if any(s.startswith(p) for p in _TRIGGER_PHRASES):
            return _trim_trailing_punct(s)
    if sentences:
        return _trim_trailing_punct(sentences[0].strip())
    return ""


def _trim_trailing_punct(text: str) -> str:
    """Strip a trailing period from a single-sentence trigger so the
    catalog table doesn't have inconsistent trailing punctuation."""
    text = text.strip()
    if text.endswith("."):
        return text[:-1]
    return text


def load_skill(skill_dir: Path) -> SkillEntry | None:
    """Parse one ``<name>/SKILL.md`` into a :class:`SkillEntry`.

    Returns ``None`` if the directory is missing a SKILL.md or the
    frontmatter is malformed.  On a malformed SKILL.md, emits a
    ``WARNING:`` line to *stderr* so operators can diagnose silently-
    omitted skills (the conformance test is the place to *fail*, but
    stderr lets operators catch drift in non-CI environments too).
    Use ``mimir skills catalog --strict`` to make the CLI exit non-zero
    on any parse error.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text()
        fm = parse_frontmatter(text)
    except (OSError, ValueError) as exc:
        print(f"WARNING: {skill_md}: skipped (parse error: {exc})", file=sys.stderr)
        return None
    name = fm.get("name", "").strip() or skill_dir.name
    description = fm.get("description", "").strip()
    allowed = extract_list_field(text, "allowed-tools") or []
    trigger = _extract_trigger(description)
    return SkillEntry(
        name=name,
        description=description,
        allowed_tools=list(allowed),
        trigger=trigger,
    )


def _load_catalog_inner(
    skills_root: Path,
) -> tuple[list[SkillEntry], int]:
    """Internal: walk ``skills_root`` and return ``(entries, parse_error_count)``.

    ``parse_error_count`` counts directories that *have* a SKILL.md but
    couldn't be parsed (i.e. dirs without a SKILL.md are not counted as
    errors).  Each parse failure also emits a ``WARNING:`` line to
    *stderr* via :func:`load_skill`.
    """
    if not skills_root.is_dir():
        return [], 0
    entries: list[SkillEntry] = []
    error_count = 0
    for entry in sorted(skills_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue  # no SKILL.md — not a skill dir, not an error
        loaded = load_skill(entry)  # emits stderr on parse failure
        if loaded is not None:
            entries.append(loaded)
        else:
            error_count += 1  # SKILL.md present but unparseable
    return entries, error_count


def load_catalog(skills_root: Path) -> list[SkillEntry]:
    """Walk ``skills_root``, return one :class:`SkillEntry` per skill
    directory that has a parseable SKILL.md, sorted alphabetically.

    Directories missing a SKILL.md are silently skipped.  Directories
    whose SKILL.md cannot be parsed emit a ``WARNING:`` line to *stderr*
    (via :func:`load_skill`) and are excluded from the result.  Use
    ``mimir skills catalog --strict`` to make the CLI exit non-zero on
    any parse error.
    """
    entries, _ = _load_catalog_inner(skills_root)
    return entries


def render_catalog(entries: list[SkillEntry]) -> str:
    """Render a list of :class:`SkillEntry` as the catalog markdown.

    Column-stability contract (catalog-schema: v1):
    - The table always has exactly three columns: ``Skill``, ``Trigger``,
      ``Allowed tools``, in that order.
    - Column names and order are stable across minor releases; a schema
      version bump (``catalog-schema: v2``) is required to change them.
    - Cell values may change as skills are added, renamed, or updated.
    - Downstream parsers should key on the ``<!-- catalog-schema: vN -->``
      comment in the first two lines, not on column indices alone.

    Catalog schema version history:

    v1 (2026-05-26, chainlink #103): initial schema.  Three columns
      (``Skill``, ``Trigger``, ``Allowed tools``) with stable names and
      order.  Breaking changes require a ``catalog-schema: v2`` bump.
    """
    lines: list[str] = []
    lines.append("<!-- desc: All bundled mimir skills, one row per skill (name, trigger phrase, allowed tools). Use to find which skill applies to a problem. Regen via `mimir skills catalog`. -->")
    lines.append("<!-- catalog-schema: v1 -->")
    lines.append("# Skills Catalog")
    lines.append("")
    lines.append(
        "Auto-generated dispatcher for the bundled mimir skills. Each "
        "row maps a one-line trigger phrase (extracted from the skill's "
        "`description:` frontmatter) to the skill's invocation surface. "
        "Source: `mimir/skills/<name>/SKILL.md` frontmatter, regenerated "
        "via `mimir skills catalog`. Do not hand-edit — changes are "
        "overwritten on next regen."
    )
    lines.append("")
    lines.append(f"_{len(entries)} skills indexed._")
    lines.append("")
    lines.append("| Skill | Trigger | Allowed tools |")
    lines.append("|-------|---------|---------------|")
    for entry in entries:
        # ``entry.trigger`` is already passed through ``_trim_trailing_punct``
        # by ``_extract_trigger``; it is empty only when ``description`` is
        # empty, in which case ``_trim_trailing_punct(entry.description)``
        # is also empty — so the old ``or _trim_trailing_punct(...)``
        # fallback was always-empty dead code (PR #131 review). Use the
        # em-dash sentinel for consistency with the empty-tools cell.
        trigger = entry.trigger or "—"
        tools = ", ".join(f"`{t}`" for t in entry.allowed_tools) or "—"
        # Escape pipes inside cells so the table layout doesn't break.
        trigger_cell = trigger.replace("|", r"\|")
        name_cell = entry.name.replace("|", r"\|")
        lines.append(f"| `{name_cell}` | {trigger_cell} | {tools} |")
    lines.append("")
    lines.append("## Per-skill descriptions")
    lines.append("")
    for entry in entries:
        lines.append(f"### `{entry.name}`")
        lines.append("")
        if entry.description:
            lines.append(entry.description)
        else:
            lines.append("_(no description)_")
        lines.append("")
    return "\n".join(lines) + "\n"


def generate(skills_root: Path | None = None) -> str:
    """Convenience: load entries from ``skills_root`` (or default) and
    render the catalog markdown."""
    return render_catalog(load_catalog(skills_root or DEFAULT_SKILLS_ROOT))


# ----- CLI ----------------------------------------------------------


def add_argparse(parser) -> None:
    """Wire ``mimir skills catalog`` subcommand into the CLI."""
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file path (default: stdout). "
             "Recommended target: memory/skills-catalog.md under "
             "MIMIR_HOME — landing there surfaces the catalog's "
             "<!-- desc: --> line in the every-turn memory/INDEX.md.",
    )
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=None,
        help="Skills root to walk (default: the bundled "
             "mimir/skills/ directory).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Exit non-zero (status 1) if any SKILL.md could not be "
             "parsed.  Each failure is always reported on stderr "
             "regardless of this flag; --strict makes the catalog "
             "command itself signal CI failure.",
    )
    parser.set_defaults(skill_catalog_cmd=cmd)


def cmd(args) -> int:
    """Entry point for ``mimir skills catalog``."""
    root = args.skills_root or DEFAULT_SKILLS_ROOT
    entries, error_count = _load_catalog_inner(root)
    catalog = render_catalog(entries)
    if args.out is None:
        print(catalog, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(catalog)
        print(f"wrote {args.out} ({len(catalog)} bytes)")
    if getattr(args, "strict", False) and error_count:
        return 1
    return 0
