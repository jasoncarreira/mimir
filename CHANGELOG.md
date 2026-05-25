# Changelog

All notable changes will land here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with
[SemVer](https://semver.org/spec/v2.0.0.html) for versioning.

## [Unreleased]

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

[Unreleased]: https://github.com/jasoncarreira/mimir/compare/v0.1.1...HEAD
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
