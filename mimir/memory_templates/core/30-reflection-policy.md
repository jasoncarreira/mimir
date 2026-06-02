<!-- desc: which reflection actions are autonomous vs propose-only -->
# Reflection Policy

Read by the reflection skill at the start of every weekly audit.
Edit this file to widen or tighten the autonomous boundary as
trust builds. Conservative defaults:

## Autonomous (the reflection turn may apply directly)

- SAGA atom decay calls
- SAGA triples linking (additive)
- Promote / drop entries in memory/learnings-pending.md
- Wiki orphan tagging (writes to state/wiki/index.md — flag, don't delete)

## Propose-only (write to state/proposed-changes.md, operator reviews)

- ALL memory/core/ edits — cleanup, restructure, promote-to-core
  (including learned-behavior promotion), demote, persona blocks.
  Core memory is READ-ONLY at runtime; there is no autonomous core
  write. Promotions land via the change-proposal PR flow
  (open_proposal) the operator merges.
- Skill creation (skills/<name>/)
- Wiki page deletions
- Memory file deletions

If this file is missing or unparseable, fall back to propose-only
for everything — never auto-apply when in doubt.
