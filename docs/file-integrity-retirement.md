# File Integrity Ledger Retirement

The generic `$MIMIR_HOME/.mimir/file-integrity.json` ledger is obsolete. File
reads no longer consult its writer marks, inode-change-time epoch, or repair
audit. Ordinary writes no longer record per-file integrity. HOME reference
material instead relies on the unchanged protected write gate. This is not a
filesystem sandbox, a removal of live IFC checks, or a blanket trust grant to
everything readable or writable by the agent.

The acceptance-required exception is explicit admin-installed skill authority:
`$MIMIR_HOME/.mimir/skill-integrity.json`. This is a skills-only installation
record, not the generic ledger under a new name.

## Replacement Inventory

Names below identify the old consumers and their replacements, including
indirect callers whose source need not change when their shared helper changes.
Unless another file is named, symbols are in `mimir/access_control.py`.

| Old consumer or mechanism | Replacement or retained boundary |
| --- | --- |
| `_persisted_file_integrity` | Removed. `_filesystem_result_integrity` uses canonical HOME reference roots, the skills-only reader, and the unchanged source-repository, PR-lease, and fetch-cache rules described below. There is no generic lookup fallback. |
| `.mimir/file-integrity.json` | No runtime consumer, initialization, migration, read, or write. Existing files remain untouched and ignored. Test fixtures containing this filename exercise retirement, not continued support. |
| `_persisted_file_integrity_lock` | Removed; `_installed_skill_integrity_lock` serializes only explicit skill-installation records. Framework and ordinary model writes no longer take a generic ledger lock. |
| `_FILE_INTEGRITY_EPOCH_KEY`, `__ledger_epoch_ns__` | Removed. No creation-time/change-time cutoff, pre-epoch grandfathering, epoch validation, or epoch backfill remains. |
| `_FILE_INTEGRITY_DECLASSIFICATIONS_KEY`, `__declassifications__` | Removed with generic digest-bound repair. Unrelated live sink approvals and `DECLASSIFICATION_LIFETIME_SECONDS` remain; they are not file-ledger metadata. |
| `_FILE_INTEGRITY_RECORDED_ROOTS` | Removed. There is no ordinary-write recording set. `_SELF_AUTHORED_FILE_ROOTS` remains a read-classification set, not a list of paths to record. |
| `_FILE_INTEGRITY_EXCLUDED_SUBTREES` | Replaced by `_UNTRUSTED_REFERENCE_SUBTREES`, retaining exactly `state/pollers` as an untrusted reference subtree and an excluded scaffold destination. |
| `_configured_external_file_integrity_key` | Removed. Canonical absolute external-root keys no longer grant or retain trust, including keys for configured writable roots and PR checkout paths. `_configured_file_write_roots`, `_configured_pr_checkout_lease_root`, and path-resolution/access checks still serve their separate access-boundary roles. |
| `initialize_file_integrity_ledger` | Removed, without a compatibility stub or replacement initializer. |
| `mimir/runtime.py:create_agent_runtime` | No ledger startup dependency or `file integrity ledger could not be initialized` failure. Missing, malformed, or historical metadata is irrelevant to startup. |
| `mimir/core_blocks.py:_prompt_file_is_trusted` | No generic ledger lookup. Requires successful canonical resolution within the explicitly supplied HOME and trusted integrity from `_home_reference_integrity`, shared with filesystem reads. Missing/outside and untrusted targets are omitted and logged; containment alone grants no trust. Skill targets require installation authority from that supplied HOME, not the environment. |
| `mimir/core_blocks.py:load_core`, `load_channel_memory` | Continue using `_prompt_file_is_trusted` before reading prompt memory. Trusted reference memory is no longer omitted because of a historical mark or epoch; symlinks to untrusted HOME roots remain excluded. |
| `mimir/index.py:build_memory_index` | Still filters source paths through `_prompt_file_is_trusted`; the filter enforces canonical existence, containment, and the shared HOME reference policy rather than generic ledger status. |
| `mimir/index.py:IndexGenerator.read_memory_index` | Still checks the persisted index and falls back to rebuilding when unusable. Legacy metadata cannot reject a trusted reference index, but a symlink to an untrusted target cannot supply prompt content. |
| `record_framework_file_integrity` | Replaced by `publish_framework_files`: validate scaffold destinations, publish, then verify canonical paths and exact expected bytes. No invalidate/commit ledger transaction, epoch, or `prune_builtin` ledger pruning remains. Its return count is the number of files, not newly trusted entries. |
| `write_framework_file` | Retained as an atomic single-file publisher using exclusive temporary-file creation and replacement, followed by `publish_framework_files` verification. No integrity-record side effect. |
| `mimir/commands/setup.py:_write_if_missing` | HOME scaffold writes still call `write_framework_file`; existing files are still preserved. Non-HOME setup writes retain their existing path. |
| `mimir/doc_seed.py:_write_doc`, `seed_docs` | Documentation publication still uses `write_framework_file`. The separate docs seed manifest, restore behavior, and operator-deletion handling are not retired. |
| `mimir/memory_templates/__init__.py:seed_core_memory`, `seed_init_block` | Continue creating missing memory templates with `write_framework_file`, without recording integrity or overwriting existing custom content. |
| `mimir/prompt_templates/__init__.py:seed_prompts` | Continues creating missing prompt templates with `write_framework_file`, without a ledger dependency. |
| `mimir/index.py:IndexGenerator._write_generated_file` | Calls `publish_framework_files` instead of `record_framework_file_integrity`; retains its atomic publication mechanics. |
| `IndexGenerator._write_memory`, `_refresh_skills_catalog`, `_write_state`, `_write_wiki` | Their shared writer publishes `memory/INDEX.md`, `memory/skills-catalog.md`, `state/INDEX.md`, and `state/wiki/index.md` without generic records. The agent's post-turn index rebuild inherits this behavior. A catalog entry does not grant trust to an installed skill. |
| `mimir/skill_defs.py:migrate_builtin_skill_integrity` | Removed, including its package-byte-match backfill, migration log, and stale builtin-record pruning. No startup or refresh backfill replaces it. |
| `mimir/skill_defs.py:refresh_builtin_skills`, `seed_skills` | Package builtin refresh uses `publish_framework_files`; staged directory publication, byte verification, and escaping-symlink rejection remain. `migrate_legacy_skills_dir` is a separate directory-layout migration, not an import of legacy integrity records. |
| `record_file_write_integrity` | Removed, including virtual/physical path recording, least-trust accumulation, clean-write records, and the permanent-untrusted ratchet. Live write authorization remains. |
| `mimir/tools/budget_gate.py:BudgetGateMiddleware.wrap_tool_call` | Removes the synchronous `write_file`/`edit_file` recording hook and metadata-persistence refusal. Retains authorization, argument validation, resolved execution paths, budgets, result provenance, and handler execution checks. |
| `BudgetGateMiddleware.awrap_tool_call` | Removes the corresponding `asyncio.to_thread(record_file_write_integrity, ...)` hook and metadata-persistence refusal, with the same retained boundaries. |
| `record_admin_installed_skill_integrity` | Retained but narrowed to `.mimir/skill-integrity.json`. Accepts a canonical direct child `skills/<name>`, enumerates contained files, replaces that installation's record prefix, and atomically publishes explicit trusted records. It neither reads nor imports the old ledger. |
| `mimir/skill_install.py:install` | Continues explicitly invoking `record_admin_installed_skill_integrity` after publishing the installation. Recording failure remains an installation failure with rollback; merely copying files into `skills/` does not substitute for this step. |
| `repair_file_write_integrity` | Removed. No generic repair command, digest-based declassification record, automatic cleanup, or replacement generic repair API. Reviewed existing skills can instead be explicitly recorded offline with the existing skills-only installation helper. |

## Read Classification

The HOME enumeration is `.mimir_builtin_skills`, `docs`, `memory`, `prompts`,
`skills`, and `state`. After canonical resolution, the first four and `state`
are trusted/informational through the unchanged protected write boundary, with
the following explicit exceptions and limits:

- `state/pollers/**` remains untrusted/active-ingest. Poller subprocesses persist
  external cursor, recovery, and event data outside the protected tool boundary.
- `skills/**` has **no trusted location default**. Only explicitly recorded files
  under `skills/<name>/...` are trusted/informational. Missing, invalid, or
  unreadable skill metadata, missing entries, and newly dropped files remain
  untrusted/active-ingest.
- Other HOME locations are not made trusted by HOME membership. In particular,
  attachments and fetched content remain untrusted.
- `attachments/fetch-cache` sidecar handling is preserved, including URL-digest
  filename and recorded-path validation. Even a valid sidecar and an approved URL
  do not confer integrity: approval authorizes GET egress, not returned bytes.
- Canonical containment remains significant. A symlink escaping HOME does not
  acquire HOME trust, and an unresolved resource fails closed. Prompt readers
  apply the same reference-root policy to the resolved target: even an internal
  symlink to attachments, poller state, an unrecorded skill, or an unknown HOME
  root is omitted rather than admitted through its memory alias.

External feature-factory roots, benchmark roots, and any other configured
`MIMIR_FILE_TOOL_ROOTS` `:rw` root default to untrusted/active-ingest. Writable
access is not provenance. This includes ordinary clean writes that formerly
received a trusted absolute-path entry: their old entries are now ignored and
rewriting them does not create new authority. External `:ro` roots likewise do
not acquire trust from configuration.

Two existing exceptions remain unchanged, not generalized to arbitrary roots:

- `MIMIR_SOURCE_REPO` uses the existing canonical source-checkout rule, anchored
  in merge review rather than ledger state. HOME handling takes precedence when
  paths overlap; local modifications do not by themselves remove this exception.
- `protected_result_source` retains head-bound native PR checkout lease
  authority: active lease, matching scope/repository/PR/head, and the turn-local
  trusted-author verdict. A configured lease directory alone grants no trust.
  A matching trusted lease produces repository provenance; ordinary external
  files cannot claim it with an old ledger entry.

The unchanged gate is an authorization boundary, not an OS filesystem sandbox.
This retirement deliberately stops tracking per-file provenance for reference
roots; it does not detect arbitrary offline or subprocess modifications there.
Live IFC, shell restrictions, destination checks, and the separate admin-operator
boundary on skill file-tool writes remain in force.

## Skills-Only Exception

The new metadata represents an explicit installation decision. It is not an
epoch ledger, general write-history store, content digest database, or generic
declassification mechanism. `_installed_skill_integrity` recognizes only the
exact value `trusted` for an eligible relative skill-file key.

`record_admin_installed_skill_integrity(home, skill_root)` records files present
in one reviewed installation and rejects non-skill roots and escaping files.
It replaces records for that skill prefix while retaining eligible records for
other skills. It cannot authorize memory, prompts, state, fetched attachments,
external roots, or builtin framework files. The framework publisher explicitly
excludes `skills` so it cannot bypass installation authority.

`install` must successfully perform this explicit recording step. An ordinary
authorized `write_file` or `edit_file` does not create installation records.
Conversely, these records are path-based installation authority, not proof that
the current file bytes still match an installation digest.

## Upgrade Procedure

For a deployment whose HOME is `/mimir-home`, the old
`/mimir-home/.mimir/file-integrity.json` is **LEFT IN PLACE AND IGNORED**. There
is no migration and no runtime read or write of that file. Startup does not
parse, validate, repair, truncate, delete, or rename it. Historical untrusted
marks, trusted marks, epoch values, malformed data, and repair audit entries have
no effect on the new classification.

This document marks the old file obsolete. Operators may archive or remove it
offline according to their retention requirements; neither action is required
for startup or trust. Preserve it if its historical audit information is useful.
Do not rename it to `skill-integrity.json` or copy its entries into that file.

**Old skill records are not imported.** Existing installed skills without new
explicit records are untrusted until an operator deliberately reestablishes
installation authority. This can affect skill reads immediately after upgrade.

1. Review the existing skill directory and its provenance offline. Preserve
   local customizations and take an appropriate backup before changing it.
2. If retaining the reviewed installation, explicitly invoke the existing
   offline helper `record_admin_installed_skill_integrity(home, skill_root)` from
   the upgraded code with that HOME and its `skills/<name>` directory. Check its
   boolean result; failure does not establish trust. This records the existing
   files without replacing the skill directory and is not automatic migration.
3. Alternatively, explicitly reinstall from an operator-approved source using
   the existing administrative installer, which records the completed install.
   Reinstallation can overwrite custom files; do not use a forced reinstall as
   a routine cleanup step or merely to silence an untrusted classification.

Do not run `uv sync` or `uv run` against a live deployment checkout as part of
this procedure. Use the deployment's existing interpreter for offline operator
work, following the repository's deployment guidance. Neither the recorder nor
the removed generic repair helper is a model-facing trust-elevation tool.

## Preserved #1739 Boundary

The following are source-inspection findings, not claims of passing execution
or mutation tests. These paths are unchanged by the ledger retirement.

| Requirement | Source evidence | Existing regression evidence to execute |
| --- | --- | --- |
| Exactly five session-boundary capabilities | `mimir/access_control.py:633-636`: `memory_get`, `mimir_get_turn`, `saga_feedback`, `saga_end_session`, `write_file`. | `tests/test_saga_mutation_authorization.py::test_synthesis_capabilities_are_exactly_session_boundary_tools`; `tests/test_tool_registry.py::test_synthesis_principal_has_required_capabilities_for_session_end`. |
| Hard deny outside that set, including shadow mode | `ToolRegistry.authorize_tool`, `mimir/access_control.py:8570-8585`, returns `allowed=False`, `enforcement_enabled=True`, and `session_boundary_capability_denied` before general read/MCP authorization. | `tests/test_tool_registry.py::test_synthesis_denies_non_capabilities_even_in_shadow`; `tests/test_information_flow.py::test_session_synthesis_refuses_fetch_cache_and_can_write_own_memory`. |
| Retained `memory_get` is trusted-only | `mimir/tools/memory.py:174-215` requires every returned atom to have `integrity == "trusted"` before rendering any batch content or publishing provenance; unavailable/invalid requests refuse for synthesis. | `tests/test_information_flow.py::test_synthesis_retained_reads_require_trusted_content`, including mixed batches, missing, invalid, and untrusted integrity. |
| Retained `mimir_get_turn` is trusted-only | Shared turn reader in `mimir/tools/extra.py:223-280` refuses a matching record without exact trusted integrity before rendering or publishing provenance; absent records/logs also refuse. | `tests/test_information_flow.py::test_synthesis_retained_reads_require_trusted_content`; `test_synthesis_turn_read_unavailable_refuses_without_taint`. |
| Initial synthesis inputs remain filtered | `mimir/agent.py:_filter_session_turns:623-667` admits only trusted records for the matching session/channel; `Agent._build_synthesis_prompt:4331-4347` uses it. `mimir/runtime.py:dispatch_session_synthesis:36-67` uses the same filter and skips an empty trusted window. | `tests/test_turn_prompt_assembly.py::test_mixed_session_synthesis_uses_only_clean_turns`; `test_empty_trusted_session_does_not_dispatch_boundary_synthesis`; session/channel filter tests in the same file. |

These five are an allowlist, not five denied tools. Everything outside the set
is hard-denied for the session-boundary service, including general filesystem
reads and `edit_file`. Being on the list does not bypass the remaining sink and
destination checks: synthesis can write its own channel memory, not arbitrary
cross-channel memory. Removing file-write recording does not expand this set,
remove either retained-reader integrity check, or admit untrusted initial turns.

## Verification Status

Documentation was checked against the current production diff and the named
source paths. Test assertions in the working tree cover legacy-ledger ignorance,
HOME and external-root classification, skill-only installation authority,
framework publication, prompt/index reads, startup, and both budget middleware
paths. Relevant suites include `test_information_flow.py`,
`test_framework_file_integrity.py`, `test_prompts.py`, `test_index.py`,
`test_post_turn_hooks_wiring.py`, `test_runtime.py`, `test_budget_gate_and_alias.py`,
`test_readonly_backend.py`, `test_skill_install.py`, `test_skill_defs.py`,
`test_cli.py`, `test_doc_seed.py`, `test_memory_templates.py`, and
`test_prompt_templates.py`.

Prompt symlink review fix verified in this sandbox with this checkout's locked
dependencies (`uv run --locked --extra dev`):

- `pytest -q tests/test_prompts.py tests/test_index.py tests/test_information_flow.py`:
  843 passed, 18 warnings. Includes 56 parametrized symlink cases covering core,
  channel, persisted-index, and index-source readers, untrusted internal HOME
  targets, outside/missing targets, and trusted roots/recorded skill controls.
- `pytest -q`: 18,904 passed, 61 skipped, 106 warnings in 316.35 seconds. An
  initial full-suite attempt exceeded the 200-second command timeout; the rerun
  completed with a 600-second command budget and unchanged per-test timeouts.
- `git diff --check`: passed.

Other affected scoped runs and mutation results remain to be recorded separately
for the broader retirement. No mutation coverage is inferred from these runs.
