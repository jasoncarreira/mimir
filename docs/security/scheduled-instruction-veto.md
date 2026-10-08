# Scheduled instruction write veto (#1893)

After a turn ingests an untrusted active source, schedule mutations
(`add_schedule`, `set_schedule_priority`, `remove_schedule`,
`set_poller_overrides`) are refused regardless of IFC shadow/enforcement mode.
The same veto applies to file-tool writes, edits, replacements and uploads to
the live home's `scheduler.yaml`, `pollers-overrides.yaml`, `prompts/*.md`,
`memory/core/*.md`, `memory/INDEX.md`, and a skill's `SKILL.md`, `pollers.json`
or `scripts/**`. Targets are compared after resolving traversal and symlinks.

The veto is scoped to the live home. Proposal worktrees under
`scratch/proposals/` and PR-lease checkouts remain writable under their existing
authorization rules. Use `open_proposal` and `submit_proposal` for a change
from a tainted turn; the operator merges the PR. `reload_pollers` reapplies
on-disk configuration without writing it and remains available. Since the
schedule and its autonomous instruction surfaces can only be changed by clean
turns or reviewed merges, `list_schedules` emits trusted `schedule_metadata`
provenance and does not taint a trusted operator turn.
