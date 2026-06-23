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

## Propose-only (operator reviews before it takes effect)

- ALL memory/core/ edits — cleanup, restructure, promote-to-core
  (including learned-behavior promotion), demote, persona blocks.
  Core memory is READ-ONLY at runtime; there is no autonomous core
  write. Promotions land via the protected-surface proposal PR flow
  (``open_proposal`` / ``submit_proposal``) the operator merges.
- Prompt edits (``prompts/*``) route through the same protected-surface
  proposal PR flow.
- Skill creation (skills/<name>/)
- Wiki page deletions
- Memory file deletions

If this file is missing or unparseable, fall back to propose-only
for everything — never auto-apply when in doubt.
