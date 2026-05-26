# Changelog

All notable changes will land here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with
[SemVer](https://semver.org/spec/v2.0.0.html) for versioning.

## [Unreleased]

## [0.1.2] — 2026-05-26

23 commits since v0.1.1, dominated by **agent-behavior + observability**
work — a wait-on-pending guard for `bash_async` (with wrapper-invariance
follow-up), a poller circuit-breaker, expanded algedonic event coverage,
plus a fix for the weather skill path-resolution and a turn-viewer scroll
bug. Also a structural cleanup pass (saga relocated under `benchmarks/`,
stale docs pruned, license attribution corrected).

### Added

- **Per-channel `bash_async` wait-on-pending guard** (#356, chainlink #189):
  refuses respawns when a same-intent job is already running on the same
  channel. Strips leading `export` and `VAR=val` tokens so env-export
  retry variants map to the same intent. Catches the failure mode where
  the agent spawns N parallel async jobs for the same operation when one
  was already in flight.
- **Wrapper-invariant `bash_async` guard** (#371, chainlink #192): extends
  the above to also strip `/bin/bash -c '…'` wrappers, `cd /path && …`
  chains, and absolute-path executables (`/usr/bin/node` → `node`). Adds
  a URI-target secondary key — same `at://X` / `https://X` in two
  commands triggers the guard even when executables differ. Closes the
  command-wrapper escalation sub-pattern.
- **Algedonic event on `bash_async` refusal** (#370, chainlink #193):
  emits `bash_async_refused_same_intent` when the guard fires, surfaced
  in `feedback.py`'s algedonic block. Closes the observability gap left
  by the original guard (refusals were tool-string-only, invisible to
  dashboards).
- **Poller circuit-breaker** (#365, chainlink #94): suspends a poller for
  5 minutes after 3 consecutive failures. Emits `poller_circuit_tripped`
  (once at transition) + `poller_circuit_open` (each suppressed run with
  `remaining_seconds`). Auto-resets on first clean run after backoff
  expires.
- **Circuit-breaker algedonic renderers** (#369, chainlink #196):
  informative `feedback.py` renderers for the new circuit events.
  `poller_circuit_open` deduplicates per-poller within a backoff window
  (omits `remaining_seconds` from the rendered string so 5 suppressed
  runs collapse to ONE entry with a count).
- **Per-channel memory injection** (#343, chainlink #187 — landed in
  v0.1.2 as the channel-memory load was wired post-v0.1.1 cut): turn
  prompts now auto-load `memory/channels/<channel_id>/*.md`.
- **`pollers.json` schema version field** (#360, chainlink #91):
  `POLLER_MANIFEST_SCHEMA_VERSION = 1`. Absent → v1 (backwards-compat);
  unknown values warn and parse best-effort.
- **`poller.env` deny-list warning** (#361, chainlink #95): emits
  `poller_env_secret_reintroduced` when a `poller.env` key matches
  `*_API_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `MIMIR_*`.
  Value is NOT logged in the event payload.
- **`recipient_name` extraction** (#363, chainlink #96): commitments
  extraction now surfaces a recipient identity. Threads through
  `dedupe_key` so "remind Alice about X" and "remind Bob about X" no
  longer collapse to the same commitment record.
- **GitHub poller commit subjects** (#362, chainlink #92): `pr_synchronize`
  events now include up to 3 commit subjects via the `/repos/.../compare`
  endpoint. Graceful degradation on API failure.
- **Skill-catalog schema marker** (#359, chainlink #103): generated
  `skills-catalog.md` starts with `<!-- catalog-schema: v1 -->`.
  Column-stability contract documented in `render_catalog()`.
- **Auto-regenerate `skills-catalog.md` on memory flush** (#366,
  chainlink #109): closes a drift gap — catalog feeds every-turn prompt,
  shouldn't lag SKILL.md edits.
- **Skill-catalog `--strict` exit code + stderr warnings** (#358,
  chainlink #105): malformed SKILL.md parse errors now emit a clear
  stderr warning + `--strict` exits non-zero for CI gating.
- **`<!-- desc: -->` conformance** (#357, chainlink #102): parametrized
  test enforces the first-body-line convention across all 27 bundled
  SKILL.md files.

### Changed

- **Saga relocated to `benchmarks/saga/`** (#352): the top-level `saga/`
  workspace package (LongMemEval bench harness, not the runtime memory
  backend) moved under `benchmarks/` to make the runtime-vs-bench split
  legible. Imports unchanged (package name is still `saga`); the bench
  shell's `config.py` path-resolution still works via the
  `parents[3]`-walk. Hand-edits to update path references in 9 files
  (FEEDBACK-LOOPS.md, SPEC.md, README.md, CONTRIBUTING.md, runner.py,
  score.py, etc.).
- **README + `saga/LICENSE` attribution corrected** (#350): the
  runtime/bench-shell conflation was untangled in the README; the
  dual-copyright (Jaden Schwab + Jason Carreira) was removed — the
  rewrite cleared all of Jaden's MSAM code from both the runtime and
  the bench shell, so the prior attribution was inaccurate.
- **`scaffold-docker` defaults `MIMIR_WEB_HOST=0.0.0.0`** (#348):
  generated `compose.yml` bakes the inside-container bind so the
  loopback default doesn't silently break the docker port-forward.
  Host exposure stays loopback-only via the `127.0.0.1:<port>` binding
  in `ports:`.
- **`docs/` pruning** (#351): 8 stale planning docs deleted (close-out
  of pre-OSS review backlog, v0.4/v0.5 historical roadmaps, abandoned
  spec docs). 4 KEEP specs got status banner refreshes from "filed /
  not started" → "shipped" where the feature actually landed.

### Fixed

- **Weather skill path resolution** (#368): `SKILL.md` now invokes via
  `python3 -m mimir.skills.weather.get_weather` instead of the broken
  relative path `python3 skills/weather/get_weather.py`. The latter
  didn't resolve from the shell-tool's cwd; agents had been guessing
  path variants for days with 0/7 successful invocations on muninn.
- **Turn-viewer inner-scroll preservation** (#367): the 5s poll's
  `innerHTML` replace destroys + rebuilds every `.event-bdy` scrollable
  box (reasoning, tool_call, tool_result, saga), resetting each
  `scrollTop` to 0. Long reasoning blocks were impossible to read past
  the 360px window. Captures + restores scroll positions on each poll.
- **`asyncio` strong-ref discipline at 3 fire-and-forget sites** (#349,
  chainlink #118): `loop.create_task(...)` without a retained reference
  can be GC'd before completion. `scheduler._on_job_missed`,
  `scheduler._dispatch_invalid_manifest_events`, and
  `budget_gate._emit_event_sync` now hold their tasks in module/instance
  sets with `task.add_done_callback(set.discard)` for cleanup.
- **Commitments `store.add` per-record exception isolation** (#353,
  chainlink #98): pinned with a regression test — when `store.add`
  raises for one record in a batch, the loop continues to attempt the
  remaining records.
- **Commitments large-store warning** (#364, chainlink #106):
  `current_state()` now warns when the JSONL replay count exceeds 500
  events. Per-instance flag prevents duplicate warnings across repeated
  calls in the same poller sweep.
- **Commitments `due_window_hint` ISO 8601 with non-UTC offset** (review
  follow-up via #346): test pin added in the v0.1.1 → v0.1.2 cycle for
  the non-UTC case (e.g. `-05:00` EST).
- **`commitments_due_check_error` + `saga_consolidate_error` tracebacks**
  (#346): both events now include `traceback=traceback.format_exc()` for
  operator diagnosis without trawling container logs.
- **Skill-md folded-scalar parser**: pre-existing in v0.1.1 (#347).
- **Integration test skips cleanly without claude CLI auth** (#354,
  chainlink #191): `_claude_sdk_can_invoke()` probes whether the CLI
  can complete an API call; if not, the integration test SKIPS with a
  clear message instead of failing with a misleading empty-events
  assertion.
- **Mock companion for the integration test** (#355): two new mock-based
  tests pin the hook-pairing contract even when the integration test
  skips (CI without OAuth keychain, fresh contributors).

[Unreleased]: https://github.com/jasoncarreira/mimir/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jasoncarreira/mimir/releases/tag/v0.1.2

## [0.1.1] — 2026-05-25

First post-release fix sweep — 13 PRs since v0.1.0, mostly addressing
notable code-review findings (chainlinks #97, #99, #104, #181-#187) plus
two feature additions for fresh-install ergonomics.

### Added

- **Channel memory injection** (#343, chainlink #187): per-turn prompts
  now auto-inject `memory/channels/<channel_id>/*.md` so the agent has
  operator / channel-specific context (preferences, names, patterns)
  without an explicit tool call. Synthetic channels (`scheduler:*`,
  `poller:*`) and channels with no memory files are graceful no-ops.
  8 KB cap with visible truncation note.
- **scaffold-docker `--mode=pypi`** (#336, closes #332): generates a
  Dockerfile that installs `mimir-agent` from PyPI at image-build time
  into a user-owned venv. No source clone, no `uv sync` at boot. Plays
  cleanly with the pending-update flow. Use `--mode=workspace` (default)
  for the legacy clone-on-boot shape.

### Changed

- **scaffold-docker compose.yml defaults `MIMIR_WEB_HOST=0.0.0.0`** (#348):
  inside-container bind must be 0.0.0.0 so docker's port-forward
  reaches the app. Host exposure stays loopback-only via the
  `127.0.0.1:<host_port>` binding in `ports:`; `MIMIR_API_KEY` gates
  the endpoint either way.
- **Sync mimir source defaults with deployed policy** (#337): pulls in
  refinements to `06-action-boundaries.md` and `60-filing-rules.md`
  from operator usage.

### Fixed

- **scheduler**: `commitments_due_check_error` and `saga_consolidate_error`
  events now include a `traceback` field so operators can diagnose
  failures without trawling container logs (#345, #346 follow-up).
- **commitments**: `due_window_hint` strings (ISO 8601, including
  non-UTC offsets) now parse into `due_window_start_unix` correctly
  rather than being dropped (#344, chainlink #97).
- **saga.forget**: `agent_id` and `min_retrievals` parameters now thread
  through `SagaStore.forget()` to `forget_by_criteria()` instead of
  being silently dropped (#342, chainlink #182). `contribution_threshold`
  and `contradiction_threshold` log a warning when set on the in-process
  path (NYI; HTTP path forwards server-side) (#346 follow-up).
- **saga**: `PRAGMA foreign_keys` toggle moved out of `executescript()`
  where it was silently a no-op due to SQLite parsing rules (#338,
  chainlink #186).
- **budget**: `quota_recovered` events no longer silently dropped when
  emitted from `asyncio.to_thread` callsites (#339, chainlink #184).
- **rate-limits**: `record_sync` now uses atomic write + `threading.Lock`
  to prevent file corruption under concurrent updates (#340, chainlink
  #181).
- **loop detector**: sliding-window detection now catches A,B,A,B
  alternation patterns (#341, chainlink #183).
- **skill_md parser**: unindented continuation lines inside `key: >` /
  `key: |` folded-scalar blocks now raise `ValueError` instead of
  silently swallowing subsequent keys (#347, chainlink #104).

[0.1.1]: https://github.com/jasoncarreira/mimir/releases/tag/v0.1.1

## [0.1.0] — 2026-05-24

Initial public release. `pip install mimir-agent`.

### Added

- Memory-centric agent harness built on deepagents / LangGraph.
- Saga in-process memory backend with embedding + triple retrieval.
- Skill registry under `mimir/skills/` (markdown-defined workflows).
- Per-channel cron scheduler with homeostat (plan-window + cost-rate
  suppression) under `mimir/scheduler.py`.
- Bridges for Discord, Slack, Bluesky, web chat, and benchmark stdout.
- Reflection + double-loop learning skill.
- Predictions and calibration tracking.
- PyPI version-check daily cron (`mimir_update_available` algedonic event).
- `mimir setup`, `mimir run`, `mimir update` CLI subcommands.

[0.1.0]: https://github.com/jasoncarreira/mimir/releases/tag/v0.1.0
