# Memory doctor

`mimir memory doctor` is a read-only diagnostic command for Mimir's file-memory and SAGA surfaces. It is meant to answer “is memory healthy enough to trust?” without requiring an operator to manually inspect `memory/INDEX.md`, wiki backlinks, SQLite tables, channel-memory byte counts, and stale indexes separately.

```bash
mimir memory doctor --home /mimir-home
mimir memory doctor --home /mimir-home --json
```

## What it checks

The doctor inspects these surfaces without mutating them:

- **Core memory**: missing or empty core blocks, missing leading `<!-- desc: ... -->` headers, suspiciously small core files, and total byte footprint.
- **Channel memory**: real channel directories vs synthetic `scheduler:*` / `poller:*` directories, channel-memory byte footprint, and real channels that exceed the prompt-injection cap.
- **Issue memory**: missing desc headers, oversized operational-gotcha notes, and obvious duplicate fingerprints.
- **`memory/learnings-pending.md`**: backlog size by bytes/lines.
- **Indexes**: whether `memory/INDEX.md`, `state/INDEX.md`, and `state/wiki/index.md` match freshly rendered in-memory versions. Drift is reported, not rewritten.
- **SAGA DB**: read-only SQLite integrity/foreign-key checks, atom counts, stream/type breakdowns, embedding coverage, orphan embeddings, triple counts, triple-embedding gaps, access-summary drift, and pending forget-candidate counts when available.
- **State/wiki**: unexpected top-level `state/*.md`, old open specs, wiki orphan pages, dangling wikilinks, and slug collisions.

## Status and exit semantics

The report status is derived from finding severity:

- `ok` — no warnings or errors.
- `warning` — at least one warning, no errors.
- `error` — at least one error.

The CLI exits nonzero only for `error`. Warnings are diagnostic: they should be visible to operators and automation, but they are not a hard failure gate by default.

## Output modes

Text mode is for humans:

```text
Memory doctor status: warning
Severity counts: error=0, warning=2, info=1
...
```

JSON mode is stable enough for dashboards, CI, and scheduled reports:

```bash
mimir memory doctor --home /mimir-home --json
```

The JSON shape has:

- `status`
- `severity_counts`
- `sections[]` with section metrics
- `findings[]` with `layer`, `check`, `severity`, `path`, `message`, and `suggestion`

## Read-only boundary

`mimir memory doctor` does not fix anything. It does not forget atoms, compact files, delete pages, rebuild indexes, create embeddings, edit memory, or open proposal PRs. It is a doctor in the diagnostic sense: it reports findings and suggestions so a human, agent turn, or future workflow can decide what treatment is appropriate.

## Automation

The scheduled introspection report includes a compact **Memory Health** section sourced from the same doctor internals:

- overall `ok` / `warning` / `error` status
- severity counts
- compact section metrics
- the top actionable findings, ordered by severity

Keep memory-doctor automation diagnostic-only. Alert-channel surfacing should be reserved for `error` status or clearly worsening warnings; routine warnings belong in the introspection report rather than a separate noisy scheduled job.
