# Changelog

All notable changes will land here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with
[SemVer](https://semver.org/spec/v2.0.0.html) for versioning.

## [Unreleased]

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

[Unreleased]: https://github.com/jasoncarreira/mimir/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jasoncarreira/mimir/releases/tag/v0.1.0
