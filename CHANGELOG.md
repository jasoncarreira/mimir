# Changelog

All notable changes will land here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with
[SemVer](https://semver.org/spec/v2.0.0.html) for versioning.

## [Unreleased]

### Fixed

- **Worklink pushes the attempt branch from the checkout that owns it, not the
  parent repo** (chainlink #518). With the isolated-checkout shape (#517), the
  attempt branch and its commit live only inside `lease.path` (its own `.git`,
  with `origin` already pointed at the remote). The local-path PR step still
  pushed from `self.repo`, so once a run actually got through containment +
  evidence it failed at publish with `src refspec issue/<id>-a<attempt> does not
  match any` — the work was done safely but couldn't be published without manual
  salvage. The push now runs from `lease.path`, which is also correct for the
  legacy worktree shape (it shares the parent's refs). Surfaced by the first
  clean autonomous run on the #517 rail.
- **Worklink fails loud if codex runs on the controller without an isolated
  checkout** (chainlink #517). The codex CLI resolves the git project root from
  the filesystem, so when it executes on the controller (a shared-filesystem
  compute) it must be pointed at an *isolated* checkout with its own `.git`,
  never a parent-pointing worktree, or it edits the **repo root** (seen on
  #512/#513). `_create_backend_checkout` already routes codex + shared-filesystem
  to an isolated checkout; the orchestrator now also asserts that invariant after
  lease creation (`worklink_unsafe_codex_checkout` → `blocked`) as a backstop
  against the routing regressing. This is deliberately scoped to **controller**
  execution: remote computes (`docker_sibling`/`ecs`) report
  `shared_filesystem=false` because they run codex inside the worker's own clone,
  which is the safe, preferred isolated-dispatch path and is left untouched.
- **Worklink isolated checkout is relocated outside the parent repo** (chainlink
  #517). On the `codex` + `shared_filesystem` path the attempt checkout was a
  `git clone --local` *nested inside* the repo at `repo/.worklink/<id>-<attempt>`
  — a clone-into-self that, under concurrent load, was the leading suspect for
  the non-deterministic root leaks. The clone now lives at a sibling
  `<repo.parent>/.worklink/<repo.name>/<id>-<attempt>`, fully detached from the
  repo it was cloned from. A new cheap, deterministic **self-containment assert**
  (`git rev-parse --show-toplevel` / `--absolute-git-dir`) runs right after the
  clone and refuses (and cleans up) any checkout that resolves back to the
  parent, before a metadata-inspecting backend like codex is ever pointed at it.
- **Worklink quarantines leaked root edits recoverably** (chainlink #517). When
  the `backend_wrote_outside_worktree` detector fires, the leaked paths are now
  moved into a named, path-scoped `git stash` (`worklink-leak-<id>-a<attempt>`)
  so the repo root is left clean without manual salvage — and *recoverably*, so
  a containment regression can't silently destroy salvageable work. Pre-existing
  unrelated dirt in the root is untouched; the stash label is surfaced in the
  failure reason.

### Added

- **Per-turn model-iteration ceiling** (chainlink #511). A configurable, 3-tier
  cap (`MIMIR_MAX_TURN_ITERATIONS`, default **200**; 0 disables) on how many
  model iterations a turn runs, enforced by a new `IterationGateMiddleware`:
  **75%** a gentle wrap-up nudge (no event), **90%** an urgent nudge +
  `iteration_budget_warning`, **100%** a hard stop — it force-ends the turn
  (`jump_to: end`), logs `iteration_budget_reached`, and notifies the channel
  (the triggering channel for an interactive turn, or the `deliver:` channel
  for a poller/tick) so the truncation isn't silent. Belt-and-suspenders
  alongside the per-turn tool-call budget + the homeostat — it catches a
  low-tool loop that would spin under the tool cap. Default 200 is set above the
  observed ceiling of real turns (mimirbot p99≈91 / max 154 over ~4.5k turns)
  so it only fires on a genuine runaway.
- **Graceful-drain restart** (chainlink #510). On `SIGTERM`/`SIGINT` the
  dispatcher stops accepting new events and waits up to
  `MIMIR_DRAIN_TIMEOUT_SECONDS` (default 30) for in-flight turns to finish
  before exiting — so a deploy restart lets live turns complete instead of
  killing them, and deploys no longer need a manual idle-check. If the drain
  overruns, the remaining turns are cancelled and a `dispatcher_drain_timeout`
  event is logged (the exit stays deterministic). The scaffold `compose.yml`
  now sets `stop_grace_period: 45s` (Docker's 10s default would SIGKILL through
  the drain); systemd's `TimeoutStopSec` covers it by default. Runbook:
  `docs/graceful-restart.md`.
- **Optional `deliver:` channel on pollers + scheduled ticks** (chainlink #508).
  A poller (`pollers.json` / `pollers-overrides.yaml`) or scheduled tick
  (`scheduler.yaml`) can set an optional `deliver:` channel. It's injected into
  the triggered turn as an instruction — the agent **judges** whether anything
  is worth surfacing and delivers it via `send_message` (reusing the real send
  path; not an auto-dump). On a hard turn failure (the agent couldn't report),
  the framework posts a `⚠️ <job> failed: <error>` notice there — the only
  mechanical send. The literal **`OPERATOR_CHANNEL`** resolves to
  `MIMIR_OPERATOR_ALERT_CHANNEL`. `deliver` is distinct from the event's own
  `channel_id` (its queue) — it only routes OUTPUT. Unset = today's silent
  behavior; the no-reply guard is unaffected (the instruction supplies the
  explicit channel a non-interactive turn needs).
- **Liveness watchdog — out-of-process dead-man's-switch** (chainlink #507).
  The agent writes a liveness beat to `.mimir/liveness.json` every
  `MIMIR_LIVENESS_BEAT_SECONDS` (default 60; an event-loop task, so it also
  stops on a *wedge*), and a new `mimir watchdog` command — run as a compose
  sidecar or host cron, i.e. *outside* the agent process — alerts out-of-band
  when the beat goes stale. Closes the one gap that survives even with `ntfy`:
  a hard failure (OOM/SIGKILL/hung loop/dead container) can't self-report.
  Sinks are pluggable, not ntfy-locked: `NTFY_TOPIC` and/or a generic
  `MIMIR_WATCHDOG_WEBHOOK_URL` (Slack-incoming-webhook / PagerDuty / custom
  shape). Loop mode gates the first alarm on having seen the agent alive (no
  cold-start false alarm) and pushes a recovery notice. `--once` (cron) is
  **exit-code-only** — it returns `1` when down and posts nothing, leaving
  paging to the cron monitor (a fresh process per tick can't dedupe). Runbook:
  `docs/watchdog.md`.
- **Clean-shutdown marker → unclean-restart notice** (chainlink #507). A
  sidecar-free complement to the watchdog: the agent writes a `clean: false`
  marker to `.mimir/session.json` at startup and flips it to `clean: true` on a
  graceful (SIGTERM-initiated) shutdown. If the next boot still sees
  `clean: false`, the previous run was killed/crashed/OOM'd (or wedged then
  killed) — the agent logs a `liveness_unclean_restart` event and pushes an
  out-of-band notice on the same sinks as the watchdog. First boot and a clean
  prior stop both no-op.
- **`HEALTHCHECK` → `/health` in the container image.** The scaffold
  `Dockerfile` now defines a Docker healthcheck that polls the in-process
  `/health` endpoint (a timeout catches a *wedged* loop, not just a dead
  process). Note: `restart: unless-stopped` does **not** act on health status —
  pair with an autoheal sidecar or a host/Swarm probe to restart on
  `unhealthy`. See `docs/watchdog.md`.
- **systemd unit templates for non-Docker / local deployments** — `mimir run`
  under systemd with `Restart=on-failure` and an `OnFailure=` hook that runs
  the new `mimir notify-restart` command (a self-contained ntfy/webhook push,
  no live agent needed). Templates in `deploy/systemd/`; runbook in
  `docs/systemd.md` (incl. the macOS/launchd note).
- **In-container liveness watcher via s6-overlay.** The scaffold image now runs
  s6-overlay as PID 1 (superseding tini) supervising two services: the agent and
  `mimir watchdog --restart-on-stale`. On a stale beat the watcher alerts **and**
  kills the agent (SIGTERM→SIGKILL by its beat PID) so s6 restarts it — the
  recovery path for a wedge, which neither s6 nor Docker `restart:` catch on
  their own (the process is still alive). Same-container, no docker socket. New
  `--restart-on-stale` / `--restart-grace` flags on `mimir watchdog`; tune the
  threshold with `MIMIR_WATCHDOG_STALE_AFTER` (default 300s). s6 service defs in
  `deploy/s6-overlay/`. The unclean-restart notice is now coalesced within a
  120s window so a crash-loop doesn't page on every boot (the event is still
  logged each time).
- **`GET /` redirects to `/turns`** — the bare web root now sends browsers to
  the turn viewer (302) instead of 404ing.

## [0.4.1] — 2026-06-16

### Added

- **`mimir setup` seeds an onboarding `init` block on a fresh home** (#708) —
  a brand-new home gets `memory/core/01-init.md` pointing the agent at its
  onboarding skill, so onboarding reliably fires on first contact instead of
  depending on the model noticing it has no persona blocks. First-run only:
  the onboarding skill removes it (via the proposal that establishes the
  persona) and `setup` never re-seeds it.
- **First-contact DM-channel capture + a `list_channels` tool** (#710) — the
  first time a user messages on a bridge, the framework resolves their DM
  channel (`bridge.resolve_dm_channel` → Slack `conversations.open`, Discord
  `create_dm`; opens the conversation, sends nothing) and caches it under
  that person's `dm_channels:` map in `state/identities.yaml` (fill-blank,
  header-preserving). Surfaced in the per-turn "Known identities" block and
  via a new read-only `list_channels(platform=None)` tool, so the agent can
  DM a person by their `dm-…` channel id without operator setup. A DM channel
  id is the platform's *conversation* id, never the user id — docs corrected
  accordingly.

### Fixed

- **Containers run under `tini` (PID 1) to reap zombies** (#709) — the
  non-reaping PID 1 let orphaned `git` (and codex/hermes/openclaw) children
  accumulate as zombies. `tini` is added to the top-level Dockerfile + both
  scaffold modes and wraps the ENTRYPOINT. (The hand-maintained mimirbot /
  muninn operator Dockerfiles got the same change in their own repos, since
  they don't inherit the scaffold.)

## [0.4.0] — 2026-06-16

Headline: **Worklink slices 2–3** (the autonomous worker rail grows a
planner, pluggable compute backends, and an arbiter-gated ready-queue),
a **full adversarial-audit hardening pass** (the #476–#503 backlog —
high-severity through low, every finding verified against live code
before fixing, each with a regression test), and **non-Docker
portability** (mimir refuses to start without an explicit home and no
longer hard-codes container paths).

### Added

- **Worklink — planner + decomposition contract** (`docs/internal/WORKLINK.md`,
  chainlink #380): a planner decomposition contract (#647), portable
  worker entrypoint (#655), and the operator `run` vertical's slice-1
  runbook (#646).
- **Worklink — pluggable `ComputeBackend` (execution substrate) axis**,
  orthogonal to the `ToolBackend` (codex / claude_cli) axis:
  - Local compute-backend seam (#653) and a claude-cli tool backend (#657).
  - DockerSibling config surface (#672), broker client (#674), broker
    policy guard (#680), and a sibling smoke runbook (#687).
  - ECS `RunTask` remote compute backend (#673) with re-derived,
    **observed** remote compute evidence — backend self-reports are
    never trusted (#656).
- **Worklink slice 3 — autonomy** (#444): an arbiter-gated ready-queue
  poller with a concurrency cap, TTL reaper, and `worklink_run`; the
  compute-backend autonomy policy **refuses unsandboxed autonomous
  dispatch** (#460, #690), and autonomous claim safety is hardened
  against races (#691).
- **Worklink — tool-pin inventory + drift detection**: parse (#659),
  inventory (#661), seed (#666), and a drift poller (#664) for the
  configured coding-CLI pins.
- **Worklink — backend-blocked path** (#466): added (#670), then
  activated + hardened as a follow-up (#671); configurable base branch
  + feature-branch model (#467, #669).
- **Operator poller overrides** via `<home>/pollers-overrides.yaml`
  (#651) — tune or disable discovered pollers without editing skills.

### Changed

- **Non-Docker portability** (#701): `mimir run` refuses to start
  without an explicit `--home`/`MIMIR_HOME` (the silent cwd-as-home
  fallback was the root of a cluster of coworker bugs — files
  "not existing," wrong-channel replies); container paths
  (`/mimir-home`, `/workspace/mimir`) are no longer hard-coded — they
  read from env with safe fallbacks, and a misconfigured worklink
  tool-pins poller emits `worklink_tool_pins_misconfigured` instead of
  assuming `/mimir-home`.
- **Coding-CLI pins bumped** (#679) and the `claude-code` CLI install
  is gated in the scaffold Dockerfile (#650).
- **Docker images ship Node 22 LTS** (was 20) — both the scaffold
  generator and the top-level PyPI `Dockerfile` (#703).

### Fixed

- **Adversarial-audit hardening pass — the #476–#503 backlog**, every
  finding verified against live code before fixing, each with a
  regression test:
  - *High-severity (#482–#486, #692):* retry-path remote-sync now holds
    the home lock; the budget `snapshot()` staleness guard no longer
    trusts stale no-reset observations.
  - *Security cluster (#494, #495, #499, #500, #693):* child-spawn env
    allowlist; expanded secret-redaction patterns (AKIA/ASIA, AWS
    `*_key=`, JSON OAuth tokens); transactional skill install with
    escaping-symlink rejection.
  - *Server + scheduler (#487, #488, #694):* attachment/`extra` type
    checks on inbound events; the poller fire re-consults the arbiter
    after acquiring its slot.
  - *Budget / quota (#489, #490, #696):* quota-recovery emit is
    cross-process coordinated; a Codex cap reset is respected even when
    the remaining-percent is unparseable.
  - *Saga (#491, #492, #493, #695):* `agent_id IN (?, 'shared')`
    scoping; index-add under lock; embedding-provider provenance
    (`provider_name`/`model_id`) so `reindex` never stamps one
    provider's vectors as another's.
  - *Feedback (#496, #498, #697):* windowed scans complete under
    snapshot saturation; dedup-set completeness.
  - *Skills (#501, #502, #699):* skill-metadata conformance gate.
  - *Reflection + commitments (#497, #503, #700):* applied-audit section
    boundaries; commitment-snooze naive-ISO → UTC.
  - *Worklink low-severity (#481, #698):* autonomy edge cases.
  - Earlier correctness guards from the same sweep: stale quota evidence
    + mismatched-dim session embeddings (#667).
- **Robustness against bad input + scheduling races:**
  - Non-UTF-8 bytes in core/index reads (#470, #663) and scheduled-tick
    `prompt_file` reads (#470, #665) no longer crash the turn; broader
    `UnicodeDecodeError` turn-failure hardening (#662).
  - LLM-tick jobs no longer inherit the 1s misfire grace that silently
    skipped the heartbeat (#660); the scheduler surfaces event-loop lag
    (#675) and attributes it to the active turn hogging the loop (#702).
  - `SchedulerJob` is constructed before `Scheduler.add_job` (#682);
    mid-turn injection snapshots finalize atomically (#688); denied
    writes are scoped by turn (#686); an aborted spawn releases its
    exact rate slot (#685).
  - `send_message` ignores undelivered sends in the loop detector
    (#676); malformed action directive blocks are stripped (#677);
    forced extra-file removals are backed up (#678); gave-up signal
    counts stay distinct (#683); the defaults-upgrade ignores the
    vendor scratch worktree (#684).
  - Naive Anthropic reset headers are handled (#658); the ONNX default
    model is used in the API-key embedding fallback (#681).
- **Docs:** chainlink block argument order (#654); codex sandbox
  boundary clarified (#648); ComputeBackend execution-substrate axis +
  verified sandbox profile documented (#652); the web UI's turn viewer
  + ops pages are highlighted across the onboarding skill, README, and
  docker docs (#704); a non-Docker setup/troubleshooting guide
  (`docs/mimir-nondocker-guide.md`) was added (#701); the SPEC +
  `docs/internal/*` design docs were refreshed against the current code
  (#705); and the skill-creator skill now documents channel addressing
  for message-sending skills — prefix-qualified ids, no hardcoding (#707).

## [0.3.5] — 2026-06-11

### Added

- **Worklink slice 1 — the chainlink worker rail** (`mimir/worklink/`,
  spec: `docs/internal/WORKLINK.md`, chainlink #380 subissues #438–#441;
  built largely by mimir itself through the normal PR/review loop):
  - Race-safe chainlink claiming with attempt caps + TTL reaper (the
    slice-0 probe verified `chainlink locks` atomicity: 20/20 races,
    zero double-claims; `locks steal` is forceful, so the reaper
    enforces its own staleness evidence) (#637, #638).
  - Attempt-scoped worktree lifecycle and **observed** evidence: the
    executor itself diffs `base...HEAD` + untracked files and runs the
    test command anchored to the attempt worktree — backend
    self-reports are never trusted; `completed` with an empty diff
    demotes to `failed` (#638, #641).
  - Pluggable `ToolBackend` protocol (capability declarations incl.
    `tool_category` + `quota_pool`) with a codex adapter:
    process-group timeout kill, transcripts under
    `<home>/state/worklink/transcripts/`, config-driven selection via
    `worklink.yaml` (#639).
  - `mimir worklink run <issue> [--backend X] [--dry-run]` — the
    operator vertical: validate leaf → claim → worktree → backend →
    observed evidence → gated label transitions (failure with retries
    returns to `worklink:ready`) → push + PR → cleanup (#641).
- **github-poller: state-based reconciliation for own PRs stuck at
  CHANGES_REQUESTED** (`pr_changes_requested_stale`, chainlink #449):
  a turn that consumes a review event without pushing fixes gets one
  deduped reminder per `(pr, head_sha)` — validated in production the
  same day (#640).

### Fixed

- **Poller discovery no longer descends into hidden directories**
  (#642): `skill_install`'s `.pre-update-backup` snapshot includes
  `pollers.json`, and with first-wins duplicate-name dedupe the backup
  shadowed the freshly-updated live manifest — the scheduler ran the
  OLD poller from the backup dir every tick (found live on mimirbot).
- **social-cli pollers pass ATPROTO credentials via `pass_env`**
  (#643): the subprocess depended on a hand-seeded `STATE_DIR/.env`;
  an operator app-password rotation in `compose.env` never reached it,
  producing a 3-day Bluesky failed-login storm surfacing as
  rate-limiting. `compose.env` is now the source of truth.

## [0.3.4] — 2026-06-11

### Fixed

- **Adversarial-review hardening batch** — 17 confirmed findings across
  four PRs (#630–#633), from a six-reviewer adversarial sweep of the
  codebase (chainlinks #408–#425):
  - *Delivery/lifecycle (#630):* `send_message` `<react>` directives feed
    the same confirmed-delivery accounting as the react tool and emit
    `send_message_directive_failed` on failure; Discord emoji aliases
    actually resolve (`resolve_for_discord` had zero callers — the
    documented `thumbsup` ack 400'd); `run_turn`'s setup phase runs inside
    its cleanup `finally` (no more leaked injection entries / typing holds
    / turn contexts on setup-phase cancellation).
  - *Pollers (#632):* circuit breaker re-arms on every failure at/past the
    threshold (was exact-`==` — a hard-down poller stormed forever after
    one backoff); the post-drain `proc.wait()` is bounded (a child closing
    its pipes but living on pinned a global semaphore slot permanently);
    recovery watermark can't regress (`max`); invalid-cron reloads
    preserve the prior poller + emit `poller_reload_invalid_cron`;
    duplicate poller names are skipped loudly; the manifest `env` map is
    process-control-filtered (`LD_PRELOAD` et al.); recovery re-enqueue
    re-stamps event identity (forge-proof).
  - *Saga (#631):* skill-memory injection reads through a locked store
    seam (cross-thread sqlite access was segfault-class); `evidence_count`
    means relation count (refresh_trend stopped clobbering it); skill-
    memory dedup removes tombstoned FAISS vectors + `rebuild_if_needed`
    wired (was dead code); all embeddings precompute before
    `BEGIN IMMEDIATE` (a hung embed stalled every memory write).
  - *Quota (#633):* transient-429 backoff decay is end-anchored (the
    escalation ladder actually escalates — record-anchored it self-reset
    to the 60s floor forever); early-clear disarms the one-shot recovery
    wake (no double catch-up heartbeat); `resets_at=None` snapshots
    staleness-guarded by window length (a stale ≥wall reading could wedge
    severity TIGHT on Codex).

### Changed

- **Quota severity recalibrated: pace floor + higher wall (#629).** The M
  bands engage only when the projected end-of-window utilization exceeds
  **75%** — a window not even projected near its cap has no quota story to
  tell, no matter how small M is (observed live: muninn's heartbeats went
  dark over a projected-68% week). The raw-utilization wall moves
  **0.80 → 0.90**; the canonical time-left asymmetry is unchanged; direct
  and derived thresholds now coincide at 0.90; the early-recovery probe's
  hot-window veto follows the same threshold.
- **The forgot-to-send guard is channel-scoped (#634).** A confirmed
  delivery must reach the *triggering* channel — a cross-channel-only send
  (e.g. an ops alert) no longer suppresses the `no_reply` signal for the
  user who asked; the event carries `delivered_elsewhere`, and the prompt
  conventions teach "close the loop with whoever asked" (a react counts).
- **The `claude-code:` model route is deprecated (#634).** Its tools
  execute inside the Claude Code subprocess, bypassing the per-turn tool
  budget and prohibited-action screen. `_resolve_model` refuses it unless
  opted in via `MIMIR_ALLOW_CLAUDE_CODE=1` (env, or the `<home>/.env`
  scaffold line that `mimir setup --subscription` now writes for
  claude-code routes — informed consent at setup time, threaded through
  every Config-based resolution path).

### Added

- **WORKLINK design spec (#635)** — `docs/internal/WORKLINK.md`:
  chainlink worker orchestration (planner/executor split, observed
  evidence, pluggable toolchain backends). Spec only; implementation
  tracked as chainlink #380 subissues.

## [0.3.3] — 2026-06-10

### Added

- **Priority-banded suppression for ALL autonomous work — pollers included.**
  The homeostat previously made a binary fire/suppress call for heartbeats
  only; pollers bypassed it entirely, so under quota pressure heartbeats
  backed off while a `* * * * *` poller kept spawning turns (burning the
  window tail, or 429ing and refreshing the pause every minute). The arbiter
  now grades pressure into a severity ladder (CLEAR / ELEVATED / TIGHT /
  BLOCKED) and gates each unit of work by its declared priority: `low` sheds
  at ELEVATED, `normal` at TIGHT, `high` only at BLOCKED (recorded 429).
  Pollers declare `priority` in `pollers.json` (default `normal`); scheduled
  ticks declare it per scheduler.yaml entry (default `low` — heartbeats yield
  first). Shed poller fires skip the subprocess (cursor frozen — events
  delayed, not lost) and emit `poller_fire_suppressed`.
- **Pace-aware subscription severity (burst multiple M).** Subscription
  (quota) decisions are now based on projected usage with time-awareness:
  M = (1−util)/(pace×time_left) — how many times the established pace the
  agent would have to sustain for the rest of the window to hit 100%. The
  same projected 80% grades CLEAR with 1 day left of a 7d window (busting
  needs ~2.75× pace) but TIGHT with 5 days left (~1.35×). Band edges are
  scaled by an early-window confidence ramp (γ = elapsed/0.25, capped at 1)
  so a noisy first hour can't shed work. Raw utilization ≥ 0.80 is a
  TIGHT wall, demoted to ELEVATED when the same window's own pace shows
  coasting (M ≥ the ELEVATED edge) — near the cap but not going to hit it
  sheds low-priority work only; without pace evidence (pegged / derived /
  early windows) the wall stays TIGHT. API (pay-as-you-go) billing:
  cost-rate alert → TIGHT, within 80% of the hourly-limit or spike trip →
  ELEVATED.
- **429 early-recovery probe.** While a quota pause is active, an interval
  job (`MIMIR_QUOTA_RECHECK_SECONDS`, default 180s) checks whether the
  window cleared before the recorded reset: an authoritative cap, plus a
  rate-limit-store snapshot observed after the pause was recorded, with no
  current window at/over the wall → pause clears immediately,
  `quota_recovered` fires with `early=true`, and a catch-up heartbeat runs.
  The current severity (when above CLEAR) renders into the agent's
  Self-state block as an `autonomy throttle` line.

### Changed

- Internal API only — no operator-facing surface changes (patch
  release): `HomeostaticArbiter.should_fire_heartbeat()`
  is replaced by `should_fire(priority=...)` returning a `FireDecision`;
  `billing.evaluate_quota()` is replaced by `evaluate_quota_severity()`.
  The fixed on-pace suppress thresholds (0.90 5h / 0.95 7d) are superseded
  by the M bands. `scheduled_tick_suppressed` events now carry `priority`,
  `severity`, and (for pace decisions) `burst_multiple`.

## [0.3.2] — 2026-06-09

### Fixed

- **The forgot-to-send guard no longer flags react-only replies.** A `react`
  (an emoji acknowledgment) is a valid interactive response, but
  `interactive_turn_no_send_message` only counted `send_message`, so a turn
  that responded with just a reaction was falsely flagged as "no reply"
  (observed live on muninn). The `react` tool now bumps a `react_count` on the
  turn context (on a confirmed react), and the guard fires only when an
  interactive turn produced text and delivered **neither** a `send_message`
  **nor** a `react`.

## [0.3.1] — 2026-06-09

Follow-up to the 0.3.0 explicit-delivery switch.

### Fixed

- **Upgrade reconciliation now notifies the operator.** The version-triggered
  upgrade-reconciliation turn opens a propose-only PR (`submit_proposal`), but
  it ran on a non-interactive channel and never surfaced the PR — so the
  reconciliation could sit unreviewed (mimirbot's 0.3.0 reconciliation PR did
  exactly this). The upgrade prompt (`prompt_templates/upgrade.md` + the inline
  fallback) now instructs the agent to `send_message` the operator-alert
  channel (explicit `channel_id`, since the turn is non-interactive) with the
  PR URL after submitting.

## [0.3.0] — 2026-06-09

**BREAKING — explicit message delivery.** The agent's final turn text is no
longer auto-delivered to the channel. To say anything to a channel the agent
MUST call the `send_message` tool. This makes delivery an explicit,
inspectable action and removes the "the agent thought out loud and it got
shipped" failure mode; the unsent final text is captured as reasoning.

### Changed

- **No auto-dispatch.** The end-of-turn auto-send and the mid-turn streaming
  plan-flush are both removed (the `_streaming_dispatch` module is retired).
  The model's final text is captured as reasoning in the turn record (`output`
  + a reasoning event) and is NOT sent to the channel or recorded as a sent
  message. `send_message` is the sole delivery path and may be called multiple
  times per turn (multi-part replies, progress notes).
- **`send_message` channel defaulting is gated by interactivity.** A
  channel-less `send_message` defaults to the turn's channel only on an
  interactive turn (`user_message` / `shell_job_complete` on a registered
  bridge). On a non-interactive turn — heartbeat / `scheduled_tick`, poller,
  `saga_session_end` synthesis, `upgrade` maintenance — it errors and requires
  an explicit `channel_id` (e.g. the operator alert channel). Explicit-channel
  sends are unchanged, so heartbeat→operator-alert still works.
- **Typing indicator** fires at turn start on interactive turns and is
  released only at turn end, so it persists across multiple `send_message`
  calls instead of being tied to a per-send flush.
- Reply conventions (`prompts.py`) and `core/06-action-boundaries.md` rewritten
  to require `send_message`; `<actions>` directives (`<react>` / `<send-file>`)
  now ride inside the `send_message` text body.

### Added

- `interactive_turn_no_send_message` algedonic signal — an interactive turn
  that produced final text but never called `send_message` emits a negative
  feedback signal (the reply was stuck as reasoning, the user got nothing),
  surfaced in the next turn's feedback panel so the agent self-corrects.
- `channel_registry.is_interactive_turn` / `INTERACTIVE_TRIGGERS` — the shared
  interactivity gate used by both the `send_message` tool and the dispatcher.

### Migration

Agents reply via `send_message` now; the bundled prompt + core-memory defaults
teach this and the algedonic signal reinforces it. `send_message` remains
exempt from the per-turn tool-call budget, so the agent can always reply even
after other tools are capped.

## [0.2.18] — 2026-06-08

GEPA prompt-optimization lands as an opt-in capability (skill + extra + first
pilot), the skill-extras gap that pruned deps on restart is closed, plus two
saga/proposal correctness fixes and a core-memory default.

### Added

- **GEPA prompt optimization** — an opt-in framework for evaluator-backed
  optimization of bounded textual artifacts:
  - a bundled `gepa` skill (shipped as an **optional** skill) with explicit
    fit/anti-fit gates, an ASI-rich evaluator requirement, and a mandatory
    PR/proposal adoption gate — it never auto-replaces a production prompt
    (#611, moved to `optional-skills/` in #614);
  - an opt-in `gepa` extra plus `mimir.gepa_support`, which routes gepa's
    `reflection_lm` through mimir's already-configured ChatModel (codex-plus /
    minimax / anthropic) — no separate LiteLLM/OpenAI key (#614);
  - the first pilot harness (`evals/commitments_extraction/`, repo-root, not
    shipped in the wheel): reference-free self-containment metrics + ASI, a gepa
    adapter over the real extractor model path, and a `--baseline`/optimize
    runner that reads real in-home turns and never auto-applies a candidate
    (#616, #617).
- **Skill-declared `requires_extras`** + `mimir skills required-extras`:
  workspace-mode `start.sh` derives its `uv sync` extras from the installed
  skills, so an optional skill's dependency (e.g. `gepa`) isn't pruned on
  restart. CI validates that every declared extra is a real pyproject extra (#615).
- **Frame-checking non-goal** in the bundled core-memory defaults —
  `memory/core/05-non-goals.md` ("don't accept the source frame uncritically"),
  with a matching learned-behaviors procedure (#613).

### Fixed

- **Protected-surface proposals fail closed on leftover git conflict markers.**
  `finalize_proposal` refuses to submit `memory/core/` or `prompts/` content
  still containing a `<<<<<<<` / `=======` / `>>>>>>>` marker (the separator
  matched as a standalone 7-`=` line to avoid false-positives on setext rules),
  keeping the worktree open for re-resolution (#609).
- **Deterministic world-state lookup.** `saga.get_current_value` now orders by
  `valid_from DESC, rowid DESC LIMIT 1`, so a transient dual-`is_current` row
  resolves to the newest fact instead of an arbitrary one (#610).

### Changed

- **External tool pins bumped** — claude-code CLI `2.1.168`, codex CLI
  `0.137.0`, gogcli `v0.9.0` + Go `1.26.4`, chainlink `chainlink-1.6.0`
  (retiring the floating git HEAD), and the `langchain-claude-code` fork ref.
  Every pin verified to resolve upstream (#612).

## [0.2.17] — 2026-06-07

### Fixed

- **Five bundled skills were silently dropped** by a malformed-YAML frontmatter
  description. `try-harder`, `social-cli`, `gmail-poller`, `github-ci-watch`, and
  `github-poller` had an unquoted colon-space in their `description:` (e.g.
  `Opt-in: copy ...`, `verifiable diff: edit ...`); YAML reads that as a nested
  mapping ("mapping values are not allowed here"), so both the deepagents
  `SkillsMiddleware` and `skill_outcomes` failed to parse the frontmatter and
  skipped the skill entirely. Descriptions are now quoted. Added a strict-YAML
  conformance guard (`test_skill_frontmatter_is_valid_yaml`) over both
  `mimir/skills/` and `mimir/optional-skills/` — the existing test only scanned
  `skills/` and used the lenient parser, which is how this shipped. (#608)

## [0.2.16] — 2026-06-07

Mid-turn user-message injection (Claude Code-style continuous input) plus a
codebase-review batch of concurrency / correctness fixes.

### Added

- **Mid-turn user-message injection (#376).** On opted-in channels
  (`MIMIR_MIDTURN_INJECTION_CHANNELS` — comma-separated channel-id prefixes, or
  `*`; empty/default = off), a follow-up sent while a turn is running folds into
  that turn at the next reasoning-step boundary instead of queuing as a separate
  next turn. Durably recorded in `TurnRecord.injected_inputs`, the synthesis
  summary, chat history, and the turn viewer (woven into the chronological
  timeline at its fold time). (#591, #593, #594, #595, #596, #597)
- **`defer_injected_message` tool (#384).** Escape hatch so the agent can punt a
  folded message to its own later turn when it's a true topic switch / unrelated
  work (e.g. several people in one channel) rather than mixing it into the
  current answer. Loop-guarded via `force_new_turn`; the originating turn's
  `injected_inputs` entry is marked `deferred` for auditability. (#598)
- **Turn viewer: progressive (infinite-scroll) loading.** `/api/turns` now
  paginates (`?limit` / `?before` / `?after`); the viewer loads the newest page,
  fetches older pages on scroll, and polls only new turns — replacing the
  whole-file refetch every 5s that got slow as `turns.jsonl` grew. (#599)
- Pinned bundled npm CLI versions for reproducible images. (#585)
- Reflection / five-whys GEPA recommendation hooks (docs). (#587, #588)

### Fixed

Codebase-review batch — 1 critical, 2 high, 5 medium:

- **saga consolidate could segfault under concurrent load (critical).**
  Consolidate's shared-sqlite-connection reads now run under `_db_lock`, so a
  concurrent turn's write never touches the same connection object at the same
  time (sqlite3 + FTS5 hazard). (#386)
- **`bash_async` stuck-job leak (high).** A command that backgrounds a grandchild
  holding the pipe open no longer wedges the waiter forever (leaking the job +
  threads + FDs): bounded drainer-join, pipe close, and a live-job cap. (#387)
- **Synthesis crash on a malformed operator template (high).** A stray brace in
  an operator `saga_session_end.md` override no longer crashes every session-end
  turn — `_safe_format` falls back to the bundled default. (#388)
- **Post-turn work could wedge a channel (medium).** Finalize hooks and the
  end-of-turn `bridge.send` are now bounded by `MIMIR_POST_TURN_TIMEOUT_SECONDS`
  (default 180s); the model-loop timeout didn't cover post-loop awaits. (#389)
- **saga consolidate index / restructure correctness (medium).** Dedup-tombstoned
  atoms are removed from the FAISS index; a `_restructure` rollback tombstones the
  orphan observation instead of leaving it unbacked (and retry-duplicated).
  (#390, #391)
- **`bash_async` routed to no channel under MCP dispatch (medium).** Channel
  resolution uses `resolve_active_ctx` + the live ContextVar instead of a dead
  `_STATE` key — restoring the duplicate-spawn guard and the completion wake-up.
  (#392)
- **event_logger could lose a sync write during a trim (medium).** A shared
  threading lock serializes `log_sync` against the async `_trim_sync` rename.
  (#393)
- git-tracking no longer surfaces transient `attachments/` notes (reverts #356's
  attachments add). (#590)

## [0.2.15] — 2026-06-05

Reliability + observability: automatic GitHub Releases, poller `MIMIR_HOME`
injection, proposal-branch cleanup, richer skills-drift remediation, background-
task failure logging, and a world_state dual-current repair.

### Added

- **Automatic GitHub Releases.** `publish.yml` now creates a GitHub Release for
  each `v*` tag (after the PyPI publish gate), with notes from the matching
  CHANGELOG section — so tags appear on the repo's Releases page, not just PyPI.
  (#581)
- **`mimir skills update` shows a content diff.** A bounded, redacted unified diff
  (installed vs source) per drifted file, so the agent can tell intentional drift
  (→ `mimir skills accept`) from stale drift (→ `--apply`). Shown by default for a
  single named skill; `--diff` forces it under `--all`. (#579)
- **Resolved proposal branches are cleaned up.** Consolidation sweeps merged /
  closed `proposal/*` branches (and their worktrees), fail-closed on unknown PR
  status so it never deletes a branch with novel unmerged content. (#576)

### Fixed

- **Pollers always get `MIMIR_HOME`.** The poller runner injects `MIMIR_HOME` (from
  `Config.home`) alongside `STATE_DIR`/`POLLER_NAME`, so a poller resolving paths
  under the agent home no longer depends on per-entry `pass_env` — which accepted
  skill-drift could freeze out (the gmail-poller "MIMIR_HOME is unset" warning).
  (#580)
- **The skills-drift digest names the keep-vs-overwrite choice.** It now points to
  `mimir skills accept` (keep intentional local changes) alongside `--apply`
  (overwrite from source), instead of steering an agent with intentional drift into
  a clobber loop. (#577)
- **world_state dual-current rows are repaired.** Consolidation collapses any
  `(subject, predicate)` left with >1 `is_current` row by a transient write race —
  keeping the newest, end-dating the rest — so `get_current_value` can't stay
  ambiguous. Runs on every non-dry-run consolidate. (#582)
- **Background-task failures are logged.** Fire-and-forget tasks spawned via
  `spawn_background` emit a `background_task_failed` event (redacted, bounded) on a
  non-cancellation exception, instead of surfacing only via asyncio's default
  handler. (#575)

## [0.2.14] — 2026-06-04

Release hardening for scheduler recovery, skill drift, git tracking, prompt
refresh, event redaction, background tasks, and weather timeouts.

### Added

- **Poller recovery wiring is now covered by regression tests.** Added coverage
  for poller recovery wake-up registration and timeout process-group cleanup,
  plus shell-job output-drainer completion handling before jobs are marked done.
  (#568)
- **Deepagents graph rebuilds when prompt inputs change.** The agent now
  re-renders the system prompt on each build check, fingerprints the rendered
  prompt plus skill catalog, reuses the graph when bytes are unchanged, and
  rebuilds when core memory, memory index, operator-alert config, or bundled
  skill docs change without requiring a process restart. (#572)
- **Shared background-task helper.** Fire-and-forget bridge/server tasks now go
  through a shared strong-reference helper so asyncio tasks are retained until
  completion. (#573)

### Fixed

- **Version-bump skill-drift digest no longer lists orphaned skills.** The
  `mimir_update_digest` skills_drift list (#565) counted orphaned custom
  skills (no shipped source) as "drift" — but they aren't fixable via
  `mimir skills update --apply`, so they were noise. Now only skills with a
  source counterpart (real differs/added drift) are reported. Surfaced by the
  0.2.13 rollout, which flagged ~12 orphaned skills per agent. (#567)
- **Transient Codex Plus stream drops are retried.** The agent patches the
  langchain-codex-plus streaming path to retry transient stream failures instead
  of failing the whole turn immediately. (#569)
- **Fingerprint-accepted skill drift stays accepted.** Optional-skill drift
  acceptance now records and honors source fingerprints so operator-accepted
  drift does not keep resurfacing as actionable drift. (#570)
- **Release-polish fixes for git tracking, poller feedback, and OAuth usage.**
  Home git tracking is guarded on main, squash-sync/proposal bookkeeping is
  tightened, poller feedback rendering is cleaned up, and OAuth usage polling
  handles the release-polish edge cases. (#571)
- **Event logs redact token-shaped secrets at the sink.** `EventLogger._record()`
  now recursively redacts token-shaped strings before writing `events.jsonl`, and
  `git_bootstrap` reuses the shared redactor instead of carrying duplicate
  regexes. (#573)
- **Bridge/server fire-and-forget tasks retain strong references.** Discord and
  Slack bridge retry/logging tasks, Discord typing-trigger work, and startup
  index sweeps now keep tasks strongly referenced until they finish. (#573)
- **Weather skill network calls have explicit timeouts.** OpenWeather `urlopen()`
  calls now use a 10-second timeout so a stalled network request does not hang
  the skill path indefinitely. (#573)

## [0.2.13] — 2026-06-03

Memory by-id loads, SAGA read concurrency, tool-error observability, and a
version-bump skill-drift notice.

### Added

- **`memory_get` tool** — batch by-id atom load. The agent can fetch atoms
  whose ids it already knows (e.g. ids cited in an observation, or the
  session-boundary "atoms cited" list) in ONE call, instead of stuffing each
  id into the semantic `memory_query` tool and fanning out parallel calls.
  Pure read, no access events (a by-id load doesn't reinforce activation); runs
  on a per-call connection. The session-boundary synthesis prompt steers it to
  load cited atoms before judging their usefulness. (#564)
- **Tool call/error stats** — `tool_call`/`tool_error` events (tool name,
  ok/error, duration, denial), a per-tool failure-rate panel on the ops
  dashboard, and `mimir stats --tools`. `tool_error` is wired as a negative
  algedonic signal — closes the gap where tool *errors* (vs denials) went
  unaggregated. (#364, #566)
- **Version-bump skill-drift notice** — operator deploys (`pip install` /
  `git pull` + restart) now emit a `mimir_update_digest` when installed
  optional skills have drifted from the shipped source, with the
  `mimir skills update --apply` remediation, so skills don't go silently
  stale. (#363, #565)

### Fixed

- **Concurrent SAGA reads no longer segfault.** Read-heavy `SagaStore` methods
  open a short-lived per-call sqlite connection instead of sharing one across
  worker threads — FTS5 on a shared `check_same_thread=False` connection could
  segfault under concurrent `memory_query`. Writes stay serialized; FAISS index
  mutation/build is lock-guarded. (#365, #566)
- **Parallel `memory_query` no longer crashes with "cannot start a transaction
  within a transaction".** The retrieval access-event write is serialized and
  best-effort: a non-essential reinforcement write never fails the user-facing
  query, and never rolls back another caller's open transaction. (#564)

## [0.2.12] — 2026-06-03

Quota-pause hardening: short transient backoff + a recovery wake, and Codex
429 reset-header support.

### Fixed

- **Transient 429s no longer cause a multi-hour idle.** A header-less 429 (e.g.
  Codex's bare "Rate limit exceeded") now gets a short, escalating, decaying
  backoff (60s → 4m → 16m → …) instead of a blind 5-hour pause; a genuine cap
  still pauses to its real reset. An active authoritative cap is never shortened
  or seeded by a transient 429. (#559)
- **Recovery no longer waits for the next scheduled tick.** A one-shot wake is
  armed at the pause's reset (and re-armed on startup), so the agent retries the
  moment the window rolls over instead of idling until the next hourly
  heartbeat. (#559)
- **Codex 429s carry their reset.** With `langchain-codex-plus >= 0.0.3`
  surfacing the `x-codex-*` rate-limit headers on errors, a Codex cap pauses
  until the real window reset when the binding window is genuinely at cap; a
  low-utilization 429 is treated as transient. (#559)

### Changed

- Pin `langchain-codex-plus >= 0.0.3` (surfaces rate-limit headers on 429s). (#559)
- **Optional-skill pollers seed a transient-state `.gitignore`.** Each poller
  (github-poller, github-ci-watch, gmail-poller, and social-cli's notifications
  + feed pollers) now writes a per-`STATE_DIR` `.gitignore` (write-if-missing)
  so its high-churn cursor / dedup / working files aren't committed to the home
  repo, while durable session logs, ledgers, and operator config stay tracked.
  Removes the `git_ignored_note_skipped` noise seen when a home `.gitignore`
  blanket-blocked `state/pollers/`. (#561)

## [0.2.11] — 2026-06-02

Version-triggered defaults-upgrade proposals (epic #346) plus home-git
tracking hardening.

### Added

- **Version-triggered defaults-upgrade proposals** (epic #346): on a
  version bump, startup rewrites a local `mimir-defaults` vendor branch to
  the shipped `prompts/` + `memory/core/` defaults, runs git's native 3-way
  `merge-file` to reconcile them against the operator's home files in an
  `upgrade`-lane proposal, and fires an agent reconciliation turn — or
  auto-submits a conflict-free merge when
  `MIMIR_DEFAULTS_UPGRADE_AUTO_SUBMIT_CLEAN` is set. Core memory + prompts
  now seed from bundled templates and stay operator-editable; upgrades flow
  through an operator-gated PR. (#552, #554, #555)
- **Per-lane proposal worktrees** (chainlink #348): proposals are isolated
  per lane (`agent` vs `upgrade`) with independent one-open guards. (#553)

### Fixed

- **Multi-region conflict merges no longer abort the upgrade** — a file the
  operator customized in several spots that the new defaults also changed
  now opens a conflict proposal instead of failing with a fake error
  (`git merge-file` exit codes 2..127 are conflict-region counts, not
  process errors). (#556)
- **Home `.gitignore` tracks all of `state/**`** so agent state is not
  silently dropped, and re-blocks `.env` / `.env.*` to match the pre-commit
  hook's `NAME_PATTERNS` — preventing a wedged per-turn commit (or a
  cleartext leak if the hook is bypassed). (#548, #557)
- **Silently-ignored notes are surfaced** — prose written under a tracked
  root that the `.gitignore` would drop now emits a feedback signal instead
  of vanishing. (#551)
- **Pollers with unset required env are skipped at discovery** rather than
  scheduled to no-op every tick. (#549)
- **`rebuild_index` tool wired up** — `set_index_generator(indexes)` is now
  called in `build_app`; the tool was previously dead. (#547)

### Changed

- Dropped the over-broad `*token*` / `*credential*` gitignore + pre-commit
  filename guards (they false-blocked legitimate notes); the content scan
  remains the primary secret guard. (#550)
- introspection skill docs reference the correct runtime tool name and
  bundled-skill paths (`read_file`, `/mimir/skills`). (#541)

## [0.2.3] — 2026-05-31

The unified provider-registry refactor, Codex support, and the
post-update operator digest.

### Added

- **`spawn_codex` tool** (chainlink #293): a Codex analogue of
  `spawn_claude_code` — runs `codex exec <prompt>` once, async, reusing
  the shared spawn caps (per-hour / concurrency / recursion-depth).
  Registered only when the `codex` CLI is on PATH. (#505)
- **codex CLI in codex-subscription images** (chainlink #293):
  `mimir scaffold-docker` installs `@openai/codex` when the deployment
  uses the `codex-plus` extra, so `spawn_codex` + Codex Plus auth work in
  the container. (#506)
- **Post-update operator digest** (chainlink #284): after an approved
  update, a first-occurrence `mimir_update_digest` feedback event
  surfaces the scheduler-tick delta (new ticks to add), optional-skill
  drift, and missing required env vars. (#499)
- **`mimir setup` surfaces the model-adapter extra** — prints
  `pip install mimir-agent[<extra>]` for the chosen model, so operators
  install the adapter at setup rather than via a runtime ImportError.
  (#504)

### Changed

- **Unified LLM-provider registry** (chainlink #292): the provider
  taxonomy — name patterns, Anthropic-compat base URLs, quota pollers,
  pip extras, CLI dependencies — now lives once in `mimir/providers.py`
  as a `ProviderSpec` table. `detect_route` (routing) and
  `build_quota_providers` (quota) consult it; adding a provider is one
  table entry. Behavior-preserving. (#501, #504)
- **`spawn_claude_code` is gated on claude-code availability** — moved
  from the static tool list into a conditional registration on the
  `claude` CLI being present, mirroring `spawn_codex`. (#503)
- **social-cli pinned to a SHA** (chainlink #188): the scaffold fragment
  now checks out a fixed commit instead of a floating `--depth 1` clone
  of `main`, for reproducible image builds. (#500)

### Fixed

- **`mimir setup` codex-plus install hint** named the wrong distribution
  (`mimir` → `mimir-agent`). (#504)
- **`spawn_*` subprocesses no longer inherit stdin** (`DEVNULL`) —
  `codex exec` reads stdin and would otherwise block a headless spawn
  until timeout. (#505)

## [0.2.2] — 2026-05-30

Packaging fix. The published wheel was missing several runtime data files,
so a fresh `pip install mimir-agent` was broken at first run. Existing
deployments (with persistent saga DBs + already-seeded homes) were
unaffected — this only bit new installs.

### Fixed

- **Wheel now bundles all runtime data files** (chainlink #290). The
  `[tool.hatch.build]` include list is an allowlist that silently drops
  any file it doesn't match — and several files the installed package
  reads at runtime weren't matched:
  - `saga/schema.sql` — `SagaStore` `executescript()`s it on fresh-DB
    init, so a first-run install crashed with `FileNotFoundError` before
    saga could come up.
  - `prompt_templates/*.md` — the glob pointed at `mimir/prompts/` (a
    directory that doesn't exist; the templates live in
    `prompt_templates/`), so no default scheduler-tick prompts seeded.
  - `scheduler_template.yaml` — no default scheduler seeded on setup.
  - `credentials.yaml` — the mimir-core credential manifest never loaded.
  - `skills/**/*.sh` (tmux) and `skills/**/*.fragment` (chainlink) — skill
    support files outside the old `.md`/`.json`/`.py` allowlist.

  The skills entry is now the whole subtree (`mimir/skills/**/*`) so a new
  support-file type can't silently drop, and a stdlib regression test
  (`tests/test_wheel_package_data.py`) asserts the include config covers
  every runtime data file. Pre-existing since 0.2.0 and earlier.

## [0.2.1] — 2026-05-30

13 commits since v0.2.0 — dashboard polish, **Voyage as the default
embedding provider**, the backlog-audit scheduler ticks, and a batch of
container/boot bug fixes.

### Added

- **Backlog-audit scheduler ticks** (chainlink #283, #164): a monthly
  `issues-audit` tick triages the `memory/issues/` gotcha layer (retire
  resolved entries, file chainlink bugs for real code-level gotchas,
  escalate judgment calls) and a weekly `commitments-review` tick runs a
  validity pass over non-terminal durable commitments. Split into two jobs
  so neither turn exceeds the per-turn tool-call budget; both are
  budget-aware. (#489)
- **longmemeval category-skew warning** — `--limit N` now warns when the
  truncated sample is category-skewed, so partial bench runs aren't
  mistaken for representative. (#485)

### Changed

- **Voyage is now the default embedding provider** — the unused NVIDIA NIM
  embedding provider was removed. No-API-key deployments still fall back to
  on-device bge-small. (#492)
- **Removed the vestigial `allowed-tools` skill field** and the dead
  skill-as-subagent wiring (no skill used either; the general-purpose
  subagent path is unaffected). (chainlink #285, #494)
- **Dashboards** — unified headers and added cross-navigation links across
  all four pages; the memory page is now **state**. (#480, #482)

### Fixed

- **git_bootstrap** no longer re-appends `credential.helper` and
  `safe.directory` on every container boot — the git config stopped
  growing without bound. (chainlink #248, #477)
- **/saga dashboard** reads the live `.mimir/saga.db` instead of a stale
  `state/saga.db` path that never existed. (#481)
- **Dashboard API key** is now stored under one shared localStorage key
  across all four pages, so entering it once works everywhere. (chainlink
  #271, #486)
- **Index writes** use unique per-call temp names and sweep orphaned temp
  files, fixing a collision window under concurrent rebuilds. (chainlink
  #272, #484)
- **Pre-commit secret scan** tightened its `sk-` pattern so hyphenated
  slugs no longer trip a false positive. (#479)

## [0.2.0] — 2026-05-29

74 commits since v0.1.3 — a minor bump carrying the first **skill-memory
system**, a batch of **security hardening**, a 17-item code-review sweep,
and several structural refactors splitting large modules.

### Added

- **Skill-memory system** (chainlink #266): skills accumulate their own
  learnings — `failure-mode` / `input-quirk` / `perf-caveat` /
  `tip` / `success-pattern` atoms stored under a dedicated
  `skill_learning` source type, isolated from general recall. Recall is
  activation-ranked and injected at skill-load time for both poller and
  non-poller skills; the write path adds a `saga_record_skill_learning`
  tool plus synthesis-prompt guidance. Per-skill consolidation + dedup
  keep one skill's lessons from bleeding into another's. Feedback is
  agent-curated only (the per-turn auto-feedback ratchet was removed).
  (#447, #453, #454, #455, #456, #459, #461)
- **Per-skill refine/retire candidates** in the introspection report
  (chainlink #267): a `SkillHealth` view surfaces low-success-rate,
  negative-learning-heavy, and zero-usage skills as refine/retire
  candidates. (#462)

### Changed

- **Structural splits**: CLI subcommands extracted to
  `mimir/commands/` (#443, chainlink #240); the 2349-line feedback module
  split into a `mimir/feedback/` subpackage (#437, #241); saga migrations
  lifted to `mimir/saga/migrations.py` (#436, #242); shared bridge
  supervisor helpers to `_supervisor.py` (#435, #246); dashboard HTML/JS
  to sibling `.html` files (#438, #243).
- **Triples query path vectorized** — the `query()`-path cosine scan is
  now a single NumPy op (#464, chainlink #257).
- **git_bootstrap pre-push staleness gate** on `/workspace/mimir` (#434,
  chainlink #249).
- **skill-creator** gained a test-gate step in its authoring checklist
  (#468, chainlink #265).

### Fixed

- **Code-review sweep — 17 items** (chainlink #258 + #259): poller
  subprocess stdout/stderr now byte-capped (#466); consolidate/dedup
  candidate selection threads `reference_date` so historical-corpus bench
  replays measure the lookback window against the data, not wall-clock;
  `react()` resolves its default target from the history buffer and
  surfaces declined reactions instead of reporting "ok"; the JSONL
  snapshot tail-cap exposes `saturated` so time-windowed scans can detect
  truncation; and the scheduler now mutates the APScheduler jobstore on
  the event-loop thread (file IO stays in `to_thread`) rather than racing
  dispatch from a worker thread. Plus config int/float coercion, minimax
  host matching, ntfy timestamp normalization, and stderr redaction.
  (#467, #469, #470, #471, #472, #473, #474, #475)
- **history**: mimir's own outbound messages surface in Recent activity
  again (#465, chainlink #270).
- **triples**: expired triples are filtered by `valid_until` in both
  search paths (#463, chainlink #257).
- **oauth/saga**: 7-day confirm counter preserved across sub-bucket
  overlap gaps (#457); `search_sessions` recency falls back to
  `reflected_at`, undateable sessions rank last (#446) (chainlink #253).
- **dispatcher**: idle-worker retirement pops `_workers` (#452, #255);
  **shell_jobs** evict finished jobs after 1h to bound registry growth
  (#451, #256); **turn_logger** `_trim_sync` first-trim crash (#444);
  **quota_pause** clamps `extract_reset_at` to now + 7 days (#450).

### Security

- **SSRF gate on attachment downloads** — validate scheme + CDN host
  before fetching (#449, chainlink #251, HIGH); don't follow redirects on
  credentialed attachment downloads (#445, #252); tighten `.env` to
  `0o600` after writing secrets (#448, #252).
- **Destructive-action guardrail** wording clarified as an accident
  deterrent (not a security boundary) (#471, chainlink #259).

### Internal

- Dedicated test coverage for `config`, `registry`, `saga_ops`, `server`,
  and `templates` (chainlink #247: #439, #440, #441, #442); smoke test
  runs via `sys.executable`; longmemeval skip guard fixed (#458).
- `.mailmap`: final agent-identity remaps make the display layer
  (`%aE`/`%aN` + GitHub UI) free of anthropic/employer/hostname
  addresses (closes chainlink #176).

## [0.1.3] — 2026-05-27

25 commits since v0.1.2 — biggest single release in the agent-behavior
space yet. Two **CLI capabilities** for skill-catalog management
(`mimir skills update --apply`, `mimir skills install --configure`),
the **social-cli rabbit-hole fix** (Muninn's reported failure mode),
the **pre-merge `CHANGES_REQUESTED` gate** that closes the auto-merge
race, two new **dead-man alarms** (scheduler-wedge + cost-runaway),
and a new **`DESIGN.md` sibling-doc convention** for developer-facing
skill prose. Two pre-OSS-style refactors (`.modified` → `.differs`,
seam annotations split out of `SKILL.md`) and a batch of new algedonic
renderers round out the release.

### Added

- **`mimir skills update --apply` flag** (#396, chainlink #208): turns the
  drift-detection dry-run into an actual updater. Writes a backup of every
  `differs` file to `.pre-update-backup/<UTC-timestamp>/<rel>` before
  overwriting, so locally-edited files are recoverable. `--force` also
  removes extra files (local additions). Backup failure aborts the
  overwrite for that file rather than proceeding with no safety net.
  Exit code 1 when any file fails to copy or extras were skipped — safe
  for CI / automation use.
- **`mimir skills install --configure` flag** (#387, chainlink #127):
  interactive env-var setup at install time. The CLI walks the skill's
  declared required env vars (`env_required` in `pollers.json`) and
  prompts the operator to fill them in, writing the result to the
  skill's `state/.env`.
- **`mimir skills update` (dry-run drift detection)** (#390, chainlink #207):
  baseline of the apply flow — compares each installed optional skill
  against the source tree, prints 4-category drift output (`differs`,
  `added in source`, `extra in installed`, orphaned). Exits 1 on any
  drift (CI-friendly).
- **Social-cli thread depth surfacing + `thread.py` deeper-fetch script**
  (#395): poller events now include a rendered `threadContext` block
  showing the 5-ancestor parent chain Bluesky already provides via
  `getPostThread(parentHeight=5)`, with explicit `(you)` markers on the
  agent's own contributions and a count in the header
  (`thread (4 prior posts, 2 from you)`). Two new event extras:
  `thread_depth` and `agent_replies_in_thread`. The bundled `thread.py`
  helper fetches up to 100 ancestors + replies on demand using the
  operator's existing AT-proto creds — for cases past the 5-deep slice.
  Addresses the reported "rabbit-hole replies" failure mode.
- **Pre-merge `CHANGES_REQUESTED` gate** (#394, chainlink #217): bundled
  rule in `mimir/skills/github/SKILL.md` instructs the agent to fetch
  the current review state with one jq query before invoking
  `gh pr merge`, refuse if any reviewer is in `CHANGES_REQUESTED`, and
  post a `gh pr comment` explaining the block. Auto-surfaces on any
  `gh *` Bash invocation. Closes the auto-merge-on-first-approve race
  (chainlink #214); structured event emission for blocked merges is
  tracked in chainlink #218.
- **Scheduler-wedge dead-man alarm** (#382, chainlink #66): ntfy push
  when heartbeats are silent for `interval × safety_factor` —
  derived from the heartbeat schedule, so disabling heartbeats also
  disables the alarm (no false-positive when intentionally off).
- **Cost-runaway dead-man alarm** (#379, chainlink #66): ntfy push when
  hourly spend crosses a configurable threshold (default $50/hr).
  Paired with the wedge alarm; both share the `MIMIR_NTFY_*` env
  surface.
- **`pr_merge_blocked_by_changes_requested` algedonic event** (#391,
  chainlink #214): event-type registration + renderer in `feedback.py`
  for the merge-refusal case. Emission currently requires bash-callable
  `mimir feedback emit` CLI (tracked in chainlink #218); the renderer
  is in place so the observability lands when the emit path ships.
- **Phase 3 resolve CLI for reflection proposals** (#383, chainlink #205):
  `mimir reflection resolve <id> --accept|--reject` with audit log.
- **`list-pending` CLI + per-channel pending-proposals digest** (#380,
  #381, chainlinks #203 / #204): inspection + Phase-2 digest delivery
  to the operator channel.
- **`env_required` field on pollers** (#385, chainlink #108): pollers
  declare required env keys; missing keys fail loudly at registration
  rather than silently no-op'ing on first run.
- **`feedback mark-resolved` CLI** (#373, chainlinks #198/#199): mark
  incidents as resolved via the CLI, with robust timestamp comparison
  so silenced events stay silent across restarts.
- **Resolved-incident filter** (#372, chainlink #197): feedback block
  hides events from incidents the operator has marked resolved.
- **`SKILL.md` frontmatter parse-error algedonic** (#377, chainlink #201):
  malformed SKILL.md emits `skill_frontmatter_malformed` so the operator
  sees the parse failure on the next turn.

### Changed

- **`SkillDriftResult.modified` → `.differs`** (#393, chainlink #216):
  attribute + CLI output label renamed from `modified:` to
  `differs from source:`. The old label conflated "source changed
  upstream" with "operator hand-edited locally"; the neutral framing
  flags the ambiguity that `--apply` has to manage. Lands before #396
  so the safety story is consistent.
- **Memory-skill impl seams moved to `DESIGN.md` sibling** (#388):
  the 12 visibility tiers in `mimir/skills/memory/SKILL.md` no longer
  carry `_→ file.py:fn()_` impl-seam annotations in-line; those live
  in a new `mimir/skills/memory/DESIGN.md` developer-reference file.
  Agent reads SKILL.md without paying per-turn token cost for
  developer cross-refs; developers still get the same navigability via
  the sibling. Drift prevention is preserved via a structural test
  pinning the tier count on both files.
- **`DESIGN.md` convention documented** (#389, chainlink #215): a new
  section in `mimir/skills/skill-creator/SKILL.md` documents the
  sibling-doc pattern, when to use it, the pointer-blockquote shape
  for SKILL.md, and the recommended per-skill conformance test.
  Opt-in; bar is ≥100 lines of developer-facing prose.
- **`skill_resolver._strip_frontmatter` dedup** (#386, chainlink #212):
  removed the duplicate parser; both paths now go through `skill_md`.
  Resolves a longstanding "two parsers, two answers" hazard.
- **Memory-skill visibility tier annotations cross-linked** (#384,
  chainlink #110): each of the 12 tiers in `SKILL.md` now has an
  explicit code-path cross-reference; the test pins the count and
  the `_→` annotation per tier.
- **CHANGELOG blocks for schema version constants** (#374, chainlinks
  #195/#194/#103): every schema-version constant carries a leading
  CHANGELOG block documenting prior versions + the rationale for the
  current value.
- **`.mailmap` extended to remap sensitive git author emails** (#375,
  chainlink #176): prior commit authorship under work-domain emails
  re-mapped to a personal address for the OSS history.

### Fixed

- **`skill_outcomes` boundary-based attribution** (#378, chainlink #200):
  the success-criteria fallback path now uses Approach C
  (turn-boundary-based) attribution, matching the primary path's
  semantics. Closes a class of false-negative test failures where the
  outcome was attributed to the wrong tool call.
- **Weather skill `SKILL.md` invocation-form clarity** (#392): drops the
  misleading filesystem-path parenthetical that caused a downstream
  agent to try `python3 mimir/skills/weather/get_weather.py` (which
  only works from a source checkout) and report "not installed" when
  in fact the script was installed (just reachable only via
  `python3 -m mimir.skills.weather.get_weather`). Adds an explicit
  install-verification one-liner and documents the `--help` footgun
  (the script doesn't use argparse, so `--help` becomes a city query).
- **Poller circuit-breaker renderers** (#369, chainlink #196): more
  informative algedonic strings for the circuit events (already shipped
  in v0.1.2 but the renderer wording was refined here).

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

[Unreleased]: https://github.com/jasoncarreira/mimir/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/jasoncarreira/mimir/releases/tag/v0.2.3
[0.2.2]: https://github.com/jasoncarreira/mimir/releases/tag/v0.2.2
[0.2.1]: https://github.com/jasoncarreira/mimir/releases/tag/v0.2.1
[0.2.0]: https://github.com/jasoncarreira/mimir/releases/tag/v0.2.0
[0.1.3]: https://github.com/jasoncarreira/mimir/releases/tag/v0.1.3
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
