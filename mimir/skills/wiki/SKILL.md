---
name: wiki
description: Maintain a structured wiki under state/wiki/ — ingest raw sources from state/raw/, synthesize cross-linked pages, and lint for health. Use this skill whenever you need to build durable, graph-shaped knowledge with cross-references (entities with relationships, topics that recur, concepts you trace across many sources).
allowed-tools:
  - Edit
  - Read
  - Write
  - file_search
  - saga_store
---

# Wiki

Based on Karpathy's LLM Wiki pattern (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). The wiki is your synthesis of raw sources — not just summaries. Pages are cross-linked. The graph is the value.

## Three layers

```
state/
├── raw/          # immutable source documents (transcripts, dumps, fetched articles)
└── wiki/
    ├── entities/    # named things: people, agents, organizations, products
    ├── concepts/    # abstract ideas, patterns, frameworks
    ├── topics/      # concrete subjects, projects, events
    ├── index.md     # catalog of all wiki pages
    ├── log.md       # chronological record of operations
    └── AGENTS.md    # schema reference (this file's conventions)
```

`raw/` is **immutable**. Once a source lands, never edit it. Wiki pages cite raw files by path.

`wiki/` is yours to maintain. Pages are markdown with frontmatter and `[[wikilinks]]` to other pages.

## Page conventions

### Frontmatter

Every wiki page starts with YAML frontmatter:

```yaml
---
title: Alice Smith
description: Eng team lead, advocate for async-first culture
type: entity        # entity | concept | topic
tags:
  - engineering
  - async
---
```

Frontmatter is for human readability and lightweight categorization — it's not parsed for control flow. Don't stress about getting it perfect; descriptive over rigid.

### Body structure

```markdown
# Page Title

## Overview
1-2 sentence summary.

## Core Content
Main material organized by headings. Quote sparingly; this is your synthesis.

## Connection to My Work
How this applies to the project, persona, or ongoing thread.

## Related
**Related:** [[other-page]], [[another-page]]

## Sources
- raw/2026-04-15-thread-import.md
- raw/2026-04-22-followup.md
- [External links if any]
```

A page can cite many sources — the **Sources** list grows as the page accumulates material. New material goes into the body; the source goes into the list.

### Wikilinks

Use `[[page-name]]` to link between wiki pages. Two places they go:

- **Inline in prose**, where natural: "Alice mentioned [[stigmergy]] as a coordination model."
- **In a "Related" section** at the bottom: `**Related:** [[bob]], [[stigmergy]], [[async-culture]]`

`page-name` must match the filename (without `.md`). Filenames are
lowercase-hyphenated: `entities/alice-smith.md` → `[[alice-smith]]`,
`concepts/async-culture.md` → `[[async-culture]]`. There's no resolver
that fixes case mismatches — `[[Alice]]` is a different string than
`[[alice]]` and won't traverse to the same page. Pick one canonical
form and stick to it.

Links are not optional — they make the wiki a graph, not a list. A
page with no links is an island. Before finishing a page, scan
`index.md` and ask: which existing pages relate? Add the links both
directions.

### Categories

- **`entities/`** — named things. People, agents, organizations, products, services. Each has a definite referent.
- **`concepts/`** — abstract ideas. Patterns, frameworks, theories. Could be discussed in many contexts.
- **`topics/`** — concrete subjects. Projects, events, technologies, ongoing threads.

If you're unsure between `concepts/` and `topics/`, ask: is this an *idea* (concept) or a *thing* (topic)? "Stigmergy" is a concept. "Project Hermes" is a topic. "Alice" is an entity.

## When content evolves across sessions

A wiki page on a long-running subject — a project, a team, a recurring
topic — accumulates state over many sessions. When a later turn asks
for "the latest" or "the final" or "this week's" version, the page has
to make clear which entry is current.

**Mark which entry is current, with a date.** Don't rely on
chronological order in the page body, recency-of-write, or "I'll
figure it out from context." Retrieval doesn't preserve write order,
and a date-less "Latest:" section becomes wrong the moment it's
updated.

## Operations

### Ingest: raw → wiki

This is the primary operation. Every raw file should land in the wiki —
either by creating a new page, by updating one or more existing pages,
or both. A single source can inform many pages; a single page can cite
many sources. The mapping is many-to-many.

1. Read the raw source file (`Read state/raw/<filename>.md`)
2. Identify what's in it — which entities, concepts, topics does it touch?
3. Scan `state/wiki/index.md` for existing pages on those subjects.
   (On the very first ingest the index is empty — that's fine; you're
   bootstrapping the graph. The cross-reference discipline kicks in once
   you have a few pages.)
4. For each subject:
   - If a page exists, **update it** with the new material and add the
     source to its **Sources** list.
   - If no page exists, **create one** following the conventions above:
     core content as your synthesis, "Connection to My Work" section,
     wikilinks to related pages both inline and in "Related".
5. Update `state/wiki/index.md` — add a one-line description for any new
   page, refresh existing entries if their focus shifted.
6. Append a log entry to `state/wiki/log.md` with date, source filename,
   and which page(s) were touched.
7. **Update existing pages that should link TO any new page.** The easy
   step to skip and the most important one for graph health — a page
   with no inbound links is invisible to traversal.

**Quality bar.** Each page should:
- Have a clear focus — don't try to cover everything in one page
- Link to related pages via wikilinks (not just mention them by name)
- Include "Connection to My Work" so the page isn't just a summary
- Avoid forced analogies that don't fit

### Query: search wiki first

Before doing new research or going to raw sources:

1. **Paraphrased query** — `mcp__mimir__file_search(query="...", scope="state")`
   does hybrid keyword + vector search over the whole wiki (and raw/).
   Best when you remember the gist but not the page name —
   "who's the engineer who keeps talking about async?" finds Alice
   even if her page never uses the exact phrase.
2. **Browse by category** — read `state/wiki/index.md`. Useful when
   you want to see what's in a category at a glance, or you know the
   page name and just need to navigate.
3. **Wikilink traversal** — once you've found one page, follow
   `[[name]]` references to related pages for additional context.
4. Only go to `state/raw/` if the wiki doesn't have it. Raw sources
   are also indexed (`scope="state"` covers both wiki and raw), so a
   broad query may surface raw content the wiki hasn't synthesized
   yet — that's a signal to ingest.

### Lint: periodic health check

Do a lint pass when wiring is caught up or when prompted:

- **Orphan pages.** Pages with no inbound links. Either add inbound links from related pages or merge the orphan into a parent page.
- **Missing cross-references.** Two pages discuss the same concept but don't link each other. Add the links.
- **Stale claims.** A new source contradicts an old wiki claim. Update the page; add a note in `log.md`.
- **Pages that should split or merge.** A page covers two distinct things (split). Two pages cover roughly the same thing (merge).
- **Index and log up to date.** `index.md` should list every page; `log.md` should show recent activity.

Lint passes are slower than ingest; do them deliberately, not constantly.

## When to use the wiki vs. plain memory/

- **Wiki** is for graph-shaped, cross-referenced knowledge that grows over time — entities you keep learning about, concepts that recur across sources, topics with their own arcs. The whole point is the link graph: who relates to whom, which concept underlies which topic.
- **`memory/core/`** is for always-in-context facts (persona, procedures, style). Always loaded; budget-tight; succinct.
- **`memory/<anywhere>/`** is for non-core notes that don't need a graph — channel-scoped notes, one-off facts, transient context. No frontmatter required, no cross-reference discipline.

If a note would benefit from being linked to other notes, it belongs in `wiki/`. If it's a one-off, `memory/` is fine.

## Mirroring to SAGA

Wiki pages are durable, but searching them depends on knowing the page
name (or following links from a page you already found). SAGA atoms add
fuzzy / paraphrased recall. For pages worth retrieving by paraphrase
("who's that engineer who keeps talking about async?"), mirror the
page's headline as a semantic atom:

```
mcp__mimir__saga_store(
    content="Alice Smith — eng team lead, async-first advocate. See state/wiki/entities/alice.md",
    stream="semantic",
)
```

The pre-message hook then surfaces the atom on relevant inbounds, and
the path in the content lets you jump straight to the wiki page for
the full story. Don't mirror every page — short-lived stub pages aren't
worth the atom; the rich pages with accumulated history are.

## Lightweight rules of thumb

- The wiki is **your synthesis**, not a transcript repository. Quote sparingly.
- Wikilinks must match filenames exactly (lowercase-hyphenated). No resolver fixes case or whitespace mismatches.
- `index.md` and `log.md` are append-only in normal operation. Edit them only during lint.
- A page that's just a stub (one line of description) is fine if you don't have content yet — it gives later pages something to link to.
