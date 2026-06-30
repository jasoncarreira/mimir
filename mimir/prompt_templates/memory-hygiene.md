---
name: memory-hygiene
description: Scheduled deterministic scan for memory filing drift. Run when a turn fires with trigger=scheduled_tick on channel scheduler:memory-hygiene. Separate from weekly reflection: this pass surfaces bounded candidates for channel-memory bloat, stale/resolved/misfiled memory/issues files, missing descriptions, and wiki/memory hygiene drift without turning reflection into a grab-bag audit runner. Output should be a concise digest plus Chainlink/spec-decision follow-ups, not broad reflective reasoning.
---

# Memory hygiene

A scheduled deterministic scan for memory filing drift. This is the
sibling job to weekly reflection, not a reflection sub-pass: reflection
stays focused on behavior, learnings promotion, and core-memory review;
this job does cheap structural checks and queues cleanup work.

Default posture: **flag, don't delete**. Deletes under `/mimir-home` are
escalate-first unless a narrower prompt explicitly grants autonomy for a
clear-cut case. When in doubt, file a Chainlink issue or write a digest
entry for operator review.

## Step 1 — Gather deterministic candidates

Run cheap scans first; avoid reading the whole tree unless a candidate
actually needs inspection.

Suggested shape:

```bash
python - <<'PY'
import os
from pathlib import Path
home = Path(os.environ["MIMIR_HOME"])
for root in [home / "memory" / "channels", home / "memory" / "issues", home / "state" / "wiki"]:
    print(f"## {root}")
    for p in sorted(root.rglob("*")) if root.exists() else []:
        if p.is_file():
            stat = p.stat()
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline().rstrip("\n")
            desc = first if first.startswith("<!-- desc:") else "NO_DESC"
            print(stat.st_size, int(stat.st_mtime), p, desc)
PY
```

Candidate buckets:

- `memory/channels/*` directories whose total size approaches or exceeds
  the channel-memory injection cap (~8 KB), or synthetic-channel notes
  that look more like runbooks / wiki material than channel facts.
- `memory/issues/*.md` files that say `RESOLVED`, `FIXED`, name merged
  PRs, lack fingerprint/runbook shape, or look like concept synthesis
  that belongs in `state/wiki/concepts/`.
- memory/wiki markdown content files missing a first-line `<!-- desc: ... -->`, because
  INDEX descriptions become less useful without it. Exclude generated wiki reports
  (`state/wiki/{orphans,dangling-links,backlinks-index}.md`) and hidden/editor
  artifacts (`.DS_Store`, `.obsidian/`) from this bucket; those should be fixed
  by generator/indexer/ignore rules rather than hand-written descriptions.
- obvious filing-rule mismatches from `memory/core/60-filing-rules.md`:
  session-scoped notes in core, operational gotchas in wiki, concept
  synthesis in `memory/issues/`, free-form top-level `state/*.md` files.

Keep the candidate list bounded: surface the top 10–20 by risk/size/
certainty, not every possible cleanup.

## Step 2 — Inspect only the top candidates

For each candidate, read just enough to classify it:

- **keep** — correctly filed, still useful, or not enough evidence.
- **chainlink** — real cleanup/fix work that can be done later.
- **proposal** — protected-surface or operator-policy change needed.
- **operator decision** — ambiguous deletion/migration decision.

Do not convert this into a full content audit. If a candidate needs deep
semantic review, file a Chainlink issue with the file path and why it
looked suspicious.

## Step 3 — Record follow-ups

Use the smallest durable artifact that matches the action:

- Chainlink issue for concrete cleanup work.
- Chainlink issue or `state/spec/<topic>-decision.md` for operator decisions.
- Protected-surface proposal PR only when the needed change is already
  clear and touches `memory/core/*` or `prompts/*`.
- `rebuild_index(scope="memory" | "state" | "all")` if you made edits
  that should be visible immediately.

## Step 4 — Report a concise digest

End with a tight result:

- files scanned / candidates inspected;
- follow-ups created;
- anything that needs operator attention;
- what was deliberately deferred.

If there is no operator-actionable result, the scheduled turn can stay
silent after writing durable follow-ups.
