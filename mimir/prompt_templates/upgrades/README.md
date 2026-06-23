# Version-specific upgrade prompts

One-shot migration nudges the agent runs **once** when its home crosses a
given mimir version during a defaults upgrade. The mechanism lives in
`mimir/defaults_upgrade.py` (chainlink #645); this file is documentation, not
a prompt, and is skipped by the loader.

## Authoring

- **Filename = target version:** `upgrades/<version>.md` (e.g. `0.6.5.md`).
  The stem is the mimir version this prompt runs *on arriving at*. It must be
  a PEP 440 version matching the release that ships it; non-version names are
  ignored.
- **Frontmatter:** include `---\nname: ...\ndescription: ...\n---`, like the
  other bundled prompts.
- **Placeholders** substituted at dispatch: `{from}` (version upgraded from),
  `{to}` (the new installed version), `{version}` (this prompt's target).

## Dispatch semantics

- **Cumulative:** upgrading `0.6.4 -> 0.6.7` runs every `upgrades/<v>.md` with
  `0.6.4 < v <= 0.6.7`, oldest target first.
- **Once per bump:** dispatched only when the upgrade check advanced
  `.mimir/upgrade-defaults/last-synced-version` to the new version, so a
  restart/retry doesn't re-run it.
- Each prompt is one turn on the synthetic `upgrade-prompt:<version>` channel
  (`trigger=upgrade`, `source=system`). A missing prompt is a clean no-op.
- These are **NOT** seeded into `<home>/prompts` — they're framework-owned and
  read from package data at dispatch time.

## Boundaries

Upgrade prompts get no special powers: follow normal action boundaries —
flag/escalate destructive work, route `memory/core/*` and `prompts/*` changes
through `open_proposal` / `submit_proposal`, and don't mutate protected
surfaces directly.
