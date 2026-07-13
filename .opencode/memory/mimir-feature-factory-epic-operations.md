---
name: mimir-feature-factory-epic-operations
description: Current Mimir-to-opencode-feature-factory epic routing, safety gates, state paths, preflight, and operational boundaries.
type: project
---

Mimir's retired in-process/distributed epic runner must not be revived. Current routing keeps ordinary `worklink:ready` leaves on the Worklink backend and sends epics to the external `opencode-feature-factory` adapter in `mimir/worklink/backends/feature_factory.py`.

## Dispatch Contract

- An epic must be open, Chainlink-actionable, and carry both `worklink:ready` and `worklink:epic`. Children of an epic are excluded from ordinary leaf dispatch.
- Epic dispatch is opt-in. `MIMIR_FACTORY_EPICS_ENABLED=true` enables it; when unset, epics remain excluded from leaf dispatch but are not launched.
- The poller runs `mimir worklink run-epic <id> --home <home> --repo <repo> --autonomous` and writes its process log under `<home>/state/pollers/worklink-ready-queue/run-epic-<id>.log`.
- Autonomous execution also requires Worklink's compute policy to permit the selected substrate. The only built-in substrate is currently `local_subprocess`, which is unsandboxed and requires the explicit `defaults.allow_autonomous_local_subprocess: true` risk opt-in.
- The adapter creates a dedicated `.worklink/<issue>-<attempt>` checkout from the configured base branch, normally `main`. It does not use the caller's current branch as the implementation base.
- Factory run IDs are deterministic: `chainlink-<issue-id>`.
- The adapter launches once with `opencode factory start --autonomous --detached --repo <attempt-checkout> --ready --reviewer <name> <prompt>`, then polls the factory control plane instead of holding the launcher process. Default reviewer is `MIMIR_FACTORY_REVIEWER` or `mimir-carreira`.
- Autonomous mode self-drives story and brief gates when unambiguous and decides pre-PR through independent implementation-validator and security-reviewer evidence. It may run bounded remediation. It opens a review-ready PR by default and never auto-merges.

## State And Recovery

- Authoritative run state is `.worklink/<issue>-<attempt>/.opencode/factory/chainlink-<id>/run.json`, not the repository-root `.opencode/factory` directory.
- Terminal statuses are `completed`, `blocked`, `partial`, and `needs-human`; `run.json.terminal_result` is preferred when present. Success requires a PR URL.
- The adapter mirrors changed gate, slice, validator, security, and PR state into Chainlink comments. It resumes polling a live detached run and finalizes an already-terminal run without relaunching.
- A stale `heartbeat_at` triggers liveness probing; it is not by itself proof of failure. The adapter checks detached process and run-directory activity before declaring the run stuck.
- Only one non-terminal, non-stale factory session may run at a time across the repository root and `.worklink` attempt checkouts.
- Inspect with `feature-factory factory list`, `feature-factory factory status <run-id>`, `feature-factory factory validate <run-id>`, and `feature-factory factory cost-report <run-id>`. Do not mutate gate files or run state directly; use factory CLI control surfaces.

## Required Preflight

Before setting `MIMIR_FACTORY_EPICS_ENABLED` or adding dispatch labels:

1. Install the intended `opencode-feature-factory` version in the exact Mimir runtime/container that will launch epics.
2. Run `feature-factory doctor --profiles` there. Plugin registration, `/feature`, the primary agent, all subagents, non-interactive permissions, repo-local skill/schema, Git/GitHub auth, and ignored factory/worktree paths must all pass.
3. Run `feature-factory doctor --profiles --provider-smoke` before the first long autonomous run and after provider/auth changes. A model response alone does not prove the intended subscription/auth path.
4. Verify GitHub write identity and reviewer access. PRs target `main`, require review, and must remain unmerged until the operator/reviewer approves them.
5. Confirm no other live factory run, no conflicting PR for the epic, and no user-owned state in a checkout/worktree that cleanup could touch.

## Readiness Snapshot (2026-07-13, mimirbot `/workspace/mimir` runtime)

Point-in-time snapshot of a single runtime checkout. Regenerate it from the exact checkout/config before arming, capturing the `feature-factory doctor` command and its verbatim output — do not treat this dated snapshot as the live gate.

- npm registry latest is `opencode-feature-factory@0.2.1`.
- The runtime checkout has repo-local `.opencode/skills/feature/{SKILL.md,SCHEMA.md}`. The repository-root `.opencode/factory` directory is NOT empty: it holds a prior `chainlink-740` run (`run.json` plus `artifacts/`, `evidence/`, `gates/`, `plan/`, `reviews/`, dated 2026-07-05). Per "State And Recovery" above, repo-root is not where live runs execute (authoritative state is `.worklink/<issue>-<attempt>/.opencode/factory/chainlink-<id>/run.json`), so this is historical residue, not an active session.
- On this runtime, `feature-factory doctor --profiles --repo /workspace/mimir` reports non-interactive factory permissions OK. The outstanding readiness gaps are: the `opencode-feature-factory` plugin is not registered, doctor emits ignored-path warnings, provider smoke has not been run, and profiles resolve to the opencode default model/variant.
- Do not arm autonomous epic dispatch yet. Register the plugin and configure profiles in the actual runtime, run `feature-factory doctor --profiles` (and `--provider-smoke`) to green, then perform one bounded canary epic before enabling a broader queue.

Primary sources: `mimir/worklink/backends/feature_factory.py`, `mimir/worklink/orchestrator.py`, `mimir/optional-skills/chainlink-orchestrator/poller.py`, `mimir/worklink/backends/registry.py`, and `docs/internal/WORKLINK.md`.
