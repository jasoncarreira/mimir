# WORKLINK — Chainlink worker orchestration (chainlink #380)

Mimir-native, toolchain-agnostic orchestration for durable work
decomposition and execution. Chainlink is the coordination surface and
source of truth; mimir plans; pluggable coding/maintenance CLIs build;
deterministic machinery connects them.

Status: **Live and autonomous** (as of 2026-07-08; the original #380 slices
have all shipped, and the #832 substrate cleanup has retired the
`docker-sibling` broker and `ecs-runtask` compute paths). The production model
is **per-leaf execution**: Worklink claims a `worklink:ready` leaf, builds it
in an attempt branch via the sole coding backend, opencode (#830), observes
evidence, and opens one PR per leaf. **Integrated-epic mode
was removed (#830)** — the in-mimir distributed epic runner (decompose →
per-slice review → one integration branch → one draft PR) is gone. Epics are
now built by the external **opencode feature-factory**; `worklink:epic`
remains only as a marker the poller EXCLUDES from leaf dispatch (reserved for
the feature-factory), never a run path in mimir.

The **planner/decomposer contract** (the leaf template in §2.5) is enforced: a
leaf missing the required sections is auto-demoted to `worklink:blocked` with a
`WORKLINK_BLOCKED` reason before dispatch (re-plan → re-add `worklink:ready`).
Execution uses self-contained per-leaf attempt checkouts via the configured
compute backend (§5). Poller-dispatched OpenCode builds run as the distinct
`worklink` uid in the existing `/workspace/.worklink` checkout. The checkout is
group-writable and setgid, but there is deliberately no per-run pathname barrier:
this protects the source repository checkout from build writes, not one build
from another. It is also **not credential isolation** because the deployment's
virtiofs home mount ignores guest ownership; that separate prerequisite is
tracked in #1435. **The only Worklink compute substrate is
`local_subprocess`** (chainlink
#832 — `docker_sibling` and `ecs_runtask` were retired); autonomous dispatch
needs `defaults.allow_autonomous_local_subprocess: true` in `worklink.yaml` to
opt into the unsandboxed blast radius. The current mimirbot deployment runs
the local substrate with the opencode backend by operator config. A failed
attempt is retried up to the configured retry count and then marks the leaf
blocked. A leaf PR is never auto-merged into base — operator/reviewer approval
of each PR remains the merge boundary. The slice markers below are historical
rollout notes — the poller, the `worklink_run` tool path, and the leaf
template contract are all live now. Owner issue: chainlink #380; leaf issues
are subissues of #380.

## Operator quickstart (TL;DR)

You normally don't run anything by hand. You **file a leaf** that satisfies the
strict leaf template (§2.5) and label it `worklink:ready`; the poller claims it,
builds it via the coding backend, and opens one PR for you to review and merge.
For a whole feature/epic, use the external **opencode feature-factory** rather
than filing a `worklink:epic` in mimir (the in-mimir epic runner was removed,
#830). Everything below this section is the design/internals.

### 0. Epics: use the opencode feature-factory (chainlink #833)

The in-mimir integrated-epic runner — brief → `work-decomposer` → `decompose-reviewer`
→ per-slice adversarial review → serial `--no-ff` integration branch →
`integration-validator` → one final draft PR — **was removed in #830** after the
epic #783 arc concluded (every failure was distribution tax in that layer).

Epics are built by `feature-factory@0.8.3` through the lockstep
`opencode-feature-factory@0.8.3` adapter. OpenCode's `/feature` workflow owns
factory transitions. Worklink owns the outer Chainlink claim, isolated attempt
checkout, OpenCode process, restart record, status observation, repository tests,
and final PR identity verification.

- **Poller routing** (opt-in via `MIMIR_FACTORY_EPICS_ENABLED`, default off):
  when the flag is set, the ready-queue poller dispatches `worklink:epic` issues
  (those with both `worklink:ready` and `worklink:epic` labels) via
  `mimir worklink run-epic` instead of `mimir worklink run`, and the adapter
  starts or resumes the opencode feature-factory in the target repo. Until the
  flag is set, epics are only excluded from leaf dispatch and are never
  dispatched (no dispatch-then-refuse churn).
- **Launch**: Worklink supervises exactly `opencode run --log-level DEBUG
  --print-logs --dir <checkout> --command feature " --autonomous --max-retries
  5 <issue>"`.
  Cancellation is verified process-group cancellation through `mimir worklink
  stop`; feature-factory has no cancel transition.
- **Retry and staging**: `MIMIR_FACTORY_MAX_RETRIES` defaults to `5`, accepts
  exactly ASCII `[0-9]+` in range `1..9007199254740991`, and falls back to `5`
  for absent or invalid values. feature-factory 0.7.5 stages the workflow inside
  the run directory; exact token `--auto` is never passed. Worklink's base
  selects the checkout start point and PR target; it is not factory `--base`,
  which is never passed.
- **First-dispatch identity**: Before first factory dispatch only, after checkout
  creation and before process launch, Worklink reads the effective checkout `git
  config --get user.name` and `git config --get user.email`. Both must be
  nonblank. The child receives `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
  `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL`; Worklink never writes sandbox
  Git identity configuration.
- **Publication credential**: Worklink reads the nonblank `publishing_identity`
  from `MIMIR_FACTORY_PUBLISHING_IDENTITY` when that variable is set, otherwise
  from the trusted controller checkout's `.factory.json`. A set but blank or
  non-string override fails instead of falling back. For GitHub
  publication Worklink verifies the credential this process is already bound to,
  `GITHUB_TOKEN`, rather than selecting among candidates: `GH_TOKEN` is a
  child-only alias for `gh`, and verifying a second credential in a process that
  already verified one is refused by the forge identity memo before `/user` is
  ever reached. That token's owner is compared against the selected identity
  before dispatch, then both child aliases are normalized to it. `GH_TOKEN` and
  `GITHUB_TOKEN` set to different values fail dispatch as an operator ambiguity
  rather than one being preferred, and a missing `GITHUB_TOKEN` fails naming that
  variable - in both cases without disclosing values.
- **Controls**: Every control is `node <absolute feature-factory/bin/factory.js>`.
  Worklink admits the launcher only after package/adapter 0.8.3 verification and
  all 16 nonmutating structural command probes. Status is read with `status
  <run-id> --repo <sandbox> --json`; resume and heartbeat reuse the retained
  session; lock actions use `lock <run-id> <claim|steal|release> --session
  <session> --repo <sandbox>`.
- **Status contract**: Factory status responses are bounded whole JSON payloads
  with required, typed known fields; additive top-level fields are ignored.
  `issue_key`, `pr_base`, `lock_session`, and `pr_url` are string-or-null;
  `park_snapshot` is string-or-null and may be absent in an older status, while
  `validator` and `terminal_result` are object-or-null. Binding a status to a
  Worklink record uses the run ID as identity; the issue key is optional display enrichment. A null PR base is
  allowed during recovery and ordinary running, parked, blocked, or partial
  observation, but a populated base must always match the record. Completed
  publication verification requires a non-null matching PR base before evidence
  collection or GitHub API calls.
- **State**: Worklink records live under
  `<MIMIR_HOME>/state/worklink/factory-runs/<numeric-run-id>.json`. They preserve
  the exact launcher, sandbox, session, process handle, strict latest status,
  observation time, and controller phase. Old repository `run.json` files are
  not read as Worklink authority. Cost is unknown and no cost schema is exposed.
- **Autonomy** (second, hard opt-in): autonomous dispatch is gated by the
  capability-based `autonomous_compute_allowed` policy. The factory runs as a
  local subprocess (shared filesystem, no network isolation), so an
  autonomously-dispatched epic is *refused* unless the deployment sets, in
  `<MIMIR_HOME>/worklink.yaml`:

  ```yaml
  defaults:
    allow_autonomous_local_subprocess: true
  ```

  This knob is fail-closed — only `true` / `1` / `yes` / `on` enable it; a typo
  or unrecognized value stays OFF. The operator CLI `mimir worklink run-epic
  <id>`, invoked by hand, is unaffected (it never hits this gate). So an
  **autonomous** factory epic needs *both* knobs — `MIMIR_FACTORY_EPICS_ENABLED`
  (poller dispatches it) and `defaults.allow_autonomous_local_subprocess: true`
  (run-epic accepts the local-subprocess compute); with either missing the epic
  is safely not built.
- **Concurrency and liveness**: Same-issue exclusivity comes only from the atomic
  Worklink claim. `MIMIR_FACTORY_MAX_CONCURRENT` is a separate factory cap with
  default/fallback `1`; leaf concurrency remains `2`. The 12-hour timeout is a
  process liveness backstop. The 900-second stale threshold emits diagnostics
  only and never dispatches, steals, cancels, or deletes work.
- **Recovery**: A parked or interrupted run retains its sandbox. The parked report
  includes the `park_snapshot` path when the factory published a complete current
  diagnostic control-plane snapshot at `<operator root>/.factory/.parked/<run-id>`;
  Worklink never removes it. A null snapshot means the sandbox manifest is the only copy.
  The snapshot is not resumable: recovery uses the retained sandbox manifest. Recovery binds
  the retained identities, including resolving a factory clone through its local
  lease checkout, reads status, obtains a run-ID-first lock only when justified,
  rereads owner/history, and resumes and relaunches OpenCode with one session
  identity. `terminal_result` is opaque context; its `reason` never selects an
  action. `needs-human` and lock-refusal exits are parked and resumable, not
  terminal. A relaunch never archives, replaces, or removes a retained run.
- **Completion**: `completed`, `blocked`, and `partial` are terminal, but only
  `completed` can ship. Before `worklink:review`, Worklink runs the configured
  repository test command, requires observed non-skipped exit-zero evidence and
  a clean stable HEAD, then verifies the canonical open/non-draft GitHub PR's
  repository, base ref, head repository/ref, and head SHA. Worklink does not
  push, ready, review, remediate, or merge the PR.

Per-leaf worklink (the rest of this document) is unaffected: file a
`worklink:ready` leaf that satisfies the strict template (§2.5) and the poller
builds it via the coding backend into one PR.

## 1. Roles

The open-strix `chainlink-worker` pattern is split into two roles so a
vague parent issue can never be handed directly to a coding agent:

| Role | Who | Intelligence | Output |
|---|---|---|---|
| **Planner / decomposer** | mimir, in LLM turns | mimir's model | Ready *leaf* issues: acceptance criteria, dependency edges, review criteria, labels |
| **Executor** | `mimir/worklink/` — deterministic machinery (no model calls of its own) | opencode's own agent loop | A claimed issue → isolated checkout → backend run → **observed** evidence bundle → state transition |

The executor is a process supervisor. It claims, prepares, spawns,
observes, and reports. All building intelligence lives inside the
spawned backend subprocess, on that tool's own model account and
session mechanics. Up to three model contexts touch one leaf issue —
planner turn (decompose), backend loop (build), reviewer (PR review) —
with the executor as the auditable connective tissue.

Why the split is load-bearing:

- **State transitions never depend on a model following instructions.**
  Claim/evidence/transition is plain Python; the model can only affect
  the checkout contents.
- **Evidence is observed, not self-reported.** The executor runs
  `git diff` and the test command itself after the backend exits. A
  backend that claims success over a failing suite produces evidence
  with the real exit code.
- **Planner/design flaws are routed as blocked, not retried blindly.** If
  the backend discovers contradictory acceptance criteria, missing
  prerequisites, or another issue that needs planner/human rework, it
  emits a line `WORKLINK_BLOCKED: <reason>` as the **final line** of its
  output (the work-order prompt instructs this). The adapter maps that to
  evidence status `blocked`, preserves the reason, and the executor labels
  the leaf `worklink:blocked` (reason posted under `WORKLINK_BLOCKED`)
  instead of opening a PR or burning attempts as a generic failure. The
  marker counts only as the **final non-empty line** of stdout/stderr, so a
  backend that echoes the prompt's instruction earlier and then completes
  normally is not mislabeled blocked.
- **Mimir's turn loop stays free.** A 20-minute backend run is a
  side subprocess (like a poller), not a turn holding a channel slot.

## 2. Chainlink as the control plane

All coordination state lives in chainlink — no parallel store. The CLI
already provides the primitives:

| Need | Chainlink surface |
|---|---|
| Parent/child decomposition | `chainlink issue subissue <parent> …` |
| Dependency edges | `chainlink issue block <id> <blocker>` |
| Ready-leaf discovery | `chainlink issue ready` (open, no open blockers) + `worklink:ready` label |
| Race-safe claiming | `chainlink locks claim <issue>` / `release` / `steal` (+ `chainlink agent` identity) |
| Evidence + audit trail | `chainlink issue comment` (evidence JSON + human summary) |
| Review loop | labels + approval-shaped comment / PR review, reconciled by a poller |

**Slice 0 verified lock atomicity empirically** with
`scripts/probe_chainlink_locks.py` (chainlink #438): 20 independent
remote-backed clone races against `chainlink locks claim <issue>` on
chainlink `0.2.0+9909d7e-dirty` produced exactly one successful claim
per trial, zero double-claims, and one loser failing in git rebase on
the shared `chainlink/locks` branch. Decision: use chainlink locks as
the primary claim mechanism; do **not** add a parallel `O_CREAT|O_EXCL`
claim-file fallback unless a future chainlink-version probe shows a
double-claim or non-deterministic winner. Operational caveat: `locks
steal` is forceful — in this version a freshly-created lock is reported
as "STALE (no recent heartbeat)" and can be stolen immediately, so the
worklink reaper must enforce its own TTL / heartbeat evidence before
calling `steal`; it must not trust `steal` to reject live claims.

### State machine

The ready-queue poller treats the Worklink label vocabulary as follows. Removing
`worklink:ready` is the common way to park either a leaf or an epic; no other
label admits dispatch.

| Label | Leaf dispatch | Factory-epic dispatch |
|---|---|---|
| `worklink:ready` | Required | Required, together with `worklink:epic` |
| `worklink:epic` | Excludes the issue and children whose parent carries it | Required to select the `run-epic` path |
| `worklink:in-progress` | Not consulted directly; the active claim lock excludes redispatch and consumes a leaf slot | Not consulted directly; the active claim lock excludes redispatch and consumes a factory slot |
| `worklink:review` | Not consulted; normal transitions remove `worklink:ready`, so review work is parked | Not consulted; normal transitions remove `worklink:ready`, so review work is parked |
| `worklink:blocked` | Always excludes dispatch, even if stale `worklink:ready` remains | Always excludes dispatch, even if stale `worklink:ready` and `worklink:epic` remain |

Dependency actionability remains a separate, additional requirement from
`chainlink issue ready`. Attempt-budget exhaustion is enforced authoritatively
by the executor after its race-safe claim checks. That first refusal adds
`worklink:blocked`; the poller then excludes the issue, so exhaustion is
reported once rather than launching another refusal every poll cycle.

Labels on the issue (chainlink `status` stays open/closed):

```
needs-decomposition ──planner──▶ worklink:ready
worklink:ready ──executor claim (lock)──▶ worklink:in-progress
worklink:in-progress ──evidence ok──▶ worklink:review     (+ PR opened)
worklink:in-progress ──backend emits WORKLINK_BLOCKED / blocked_reason──▶ worklink:blocked  (human unblocks → ready)
worklink:in-progress ──failure──▶ worklink:ready           (attempts < 3)
                                  worklink:blocked          (attempts == 3, reason=attempts_exhausted)
worklink:review ──approval-shaped event (PR approved/merged or approval comment)──▶ closed
```

Rules:

- Close requires an approval-shaped event. The reconciler poller is
  state-based (reads PR state / issue comments), so missed ticks can't
  lose a transition — same pattern as the github poller's
  `requested_reviewers` reconciliation.
- A failed attempt re-enters `worklink:ready` with an attempt counter
  in the claim record; 3 attempts → `worklink:blocked`, mirroring
  poller-recovery's wedge guard.
- Stale claims (TTL, default 2× the work-order timeout) are released by
  a reaper (`locks steal` + issue back to ready + comment naming the
  stale agent).


## 2.5 Planner contract (slice 2)

The planner/decomposer is an LLM turn, but its output is still constrained to
Chainlink mutations. Use the opt-in `chainlink-orchestrator` skill (install with
`mimir skills install chainlink-orchestrator`) and the
operator-tunable `mimir/prompt_templates/decompose.md` prompt to turn a parent issue into leaf
subissues. The canonical leaf-description template is
`mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN`; the planner prompt and skill
render that exact text from the constant, and executor tests assert they stay in sync.

A leaf may receive `worklink:ready` only when it includes:

- `Acceptance criteria:` with checklist items,
- `Review criteria:`,
- `Worklink notes:` containing `Scope`, `Out of scope`, and
  `Suggested test command`.

`mimir worklink run` refuses leaves missing that template before claiming, so a
vague parent or half-planned subissue cannot reach a backend agent. Backcompat:
issues created before slice 2's template hardening are advisory-warned and still
allowed, but newly created issues are strict. Existing queued Worklink leaves
#445 and #446 were migrated in the tracker to include the new sections;
non-Worklink candidates #447 and #448 remain pre-contract advisory-warned if
routed without migration, and a planner should add the template before marking
them ready. When a planner needs ordering, it records it with
`chainlink issue block <ID-that-is-blocked> <BLOCKER>`; the blocked issue id
comes first, then the blocker. Readiness comes from Chainlink's ready queue plus
`worklink:ready`, not from prose alone.

## 3. Executor anatomy (`mimir/worklink/`)

```
mimir/worklink/
  __init__.py
  orchestrator.py   # claim → checkout → backend → evidence → transition
  claims.py         # lock protocol + TTL reaper (chainlink locks or O_EXCL fallback)
  checkout.py       # per-issue isolated-clone checkout lifecycle
  evidence.py       # schema, validation, observation (diff/tests run by US)
  backends/
    __init__.py     # registry + capability resolution
    base.py         # ToolBackend protocol, Caps, WorkOrder, RawResult
    opencode.py     # sole coding backend
    feature_factory.py # #833 worklink:epic adapter
  compute.py       # ComputeBackend protocol, WorkSpec, LaunchHandle, LocalSubprocessComputeBackend
```

**Packaging (decided 2026-06-12).** The executor above (`mimir/worklink/`) and
the `mimir worklink run` CLI stay in **core** — it is the security-critical,
integration-coupled engine (it imports `event_logger`; slice-3 dispatch consults
`HomeostaticArbiter.should_fire()`), and the safety claim that *state transitions
never depend on a model following instructions* is strongest with claim/evidence/
transition as core Python behind a hard CLI/tool boundary. The two model-touching
surfaces ship as **one opt-in skill** (`chainlink-orchestrator`): the planner
(slice 2) and the ready-queue poller (slice 3). The skill's poller **invokes
`mimir worklink run` as a subprocess and never reimplements the state machine**;
SKILL.md tells the agent to dispatch via the CLI/tool, not to hand-run claim/
evidence/transition. This buys opt-in per-home enablement (mimirbot yes, muninn
no) and an auto-registering poller via `skill_install.py` without model-mediating
the deterministic core. The slice-3 `worklink_run` *tool* stays core regardless
(tools have no skill-contributed mechanism).

**Root executor provenance (decided 2026-08-25).** Deployment builds must never
install the root-owned executor from a local directory or named context backed
by a checkout. The executor source is an immutable released distribution or a
Git reference pinned by full SHA. The SHA path is the supported way to deploy an
unreleased branch commit: the build resolves and checks out that commit, refuses
unless it exactly matches the controller commit, and records it in the image's
OCI revision label and `/opt/mimir-worklink/executor-source-commit`. The executor
identity operation exposes the recorded SHA and `/health` reports it as
`worklink_executor_source_commit`. Dirty-tree executor builds are unsupported;
working-directory edits cannot enter this root-owned artifact. Deployment
compose files must pass a fully qualified `MIMIR_GIT_REF` plus the same resolved
full SHA for `MIMIR_CONTROLLER_COMMIT` and `MIMIR_EXECUTOR_COMMIT`. The build
fetches exactly that ref and refuses unless `FETCH_HEAD` equals the pinned SHA.
This permits CI to name `refs/pull/<number>/merge` while keeping the SHA
authoritative; “point it at whichever checkout you want” is not a valid
deployment contract. All three provenance arguments are required, so plain
`docker build .` intentionally fails. Deploying a new executor requires rebuilding
and recreating the image; pulling a controller checkout is insufficient.

Entry points:

1. `mimir worklink run <issue-id> [--backend X] [--dry-run]` — CLI,
   operator-invoked (slice 1; matches #380's failure-path guidance to
   start manual).
2. `worklink_run` tool — the agent dispatches work from a turn
   (slice 3, after the dry-run proves the rail).
3. Ready-queue poller — discovers `worklink:ready` leaves and enqueues
   dispatch events (slice 3).

Per-issue flow (one `run`):

1. **Validate the leaf.** Refuse any issue missing the planner template
   (acceptance-criteria checklist, review block) — the structural
   guarantee that vague parents can't reach a coding agent.
2. **Claim** (`claims.py`). Lock + claim comment (agent id, ts,
   attempt N).
3. **Checkout.** Create a self-contained sibling checkout with `git clone
    --local`, then check out `issue/<issue>-a<attempt>` from the **configured
    base branch** in the dedicated repository named by `WORKLINK_REPO`
   (`defaults.base_branch` in `worklink.yaml`, default `main`; a `mimir
   worklink run --base <branch>` flag overrides per run). The same base is the
   diff floor (`base...head`) and the PR target (`gh pr create --base
   <base>`), so pointing it at a long-running integration/feature branch
   stacks every leaf PR there instead of straight onto main (the
   **feature-branch model**) — without any other change. The base is a single
   configured branch for the whole run; **dependency-aware bases** (auto-cutting
   a dependent leaf from its blocker's branch, resolved from the Chainlink
   `block` graph) are a deliberate follow-up — they need blocker-branch
   existence/merge ordering and rebase-on-blocker-merge handling. Names are
   **attempt-scoped**, because failed/blocked checkouts are retained for autopsy
   (§3 step 8): with issue-scoped names the second attempt would
   collide with the first attempt's kept checkout AND its branch, and
   `attempts < 3` could never actually run more than once. Each retry
   gets a fresh path + branch; autopsy artifacts from prior attempts
    stay untouched until the reaper prunes them. Never a long-lived
    per-worker branch. `WORKLINK_REPO` is never inferred from the process cwd,
    installed package, or module location. Immediately before fetching and branching, Worklink runs
   one porcelain status check as the account that owns the base repository and
   refuses a dirty index or working tree. The refusal event and error name
   bounded samples of staged, unstaged, and non-ignored untracked paths; ignored
   local directories such as `.venv/` and `.worktrees/` do not count. Worklink
   never resets or cleans the shared base automatically.
4. **Render the prompt** from issue fields: description, acceptance
   criteria, review criteria, parent context, repo conventions pointer.
   Template lives at `mimir/prompt_templates/worklink-order.md` (operator-tunable). The
   planner-side decomposition prompt lives at `mimir/prompt_templates/decompose.md`; its
   `{leaf_template}` slot is rendered from the single
   `mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN` contract, which the
   `chainlink-orchestrator` skill also embeds, so the executor validates the
   same template the planner emits.
5. **Spawn the backend** (adapter `run(WorkOrder)`) under a timeout.
6. **Observe evidence** (`evidence.py`): for shared-filesystem compute,
   the executor itself runs `git -C <checkout> diff --stat`/`status`, runs
   the declared test command, and captures the transcript pointer; for
   non-shared/remote compute, the executor fetches `origin/<base>` and
   `origin/<attempt-branch>`, checks out the fetched attempt ref, then
   re-derives diff/test evidence locally. Worker-reported evidence is a
   hint/transcript, not the transition gate.
7. **Validate + transition.** Evidence gates the label move (§4).
   Local shared-filesystem runs push the committed branch before PR creation;
   remote runs expect the worker to have pushed the branch and the executor
   opens the PR only after fetched-ref evidence passes. Native PR creation can
   become another backend cap later, but it must not bypass executor evidence.
8. **Cleanup.** Remove the attempt's checkout on success; keep on
   `failed`/`blocked` for autopsy (the reaper prunes attempt checkouts
   and their `issue/<issue>-a<n>` branches after N days, and on issue
   close). Release the lock.

## 4. Evidence schema (backend-independent)

Written to `<home>/state/worklink/evidence/<issue>-<attempt>.json` and
attached as a chainlink comment (JSON in a fenced block + 3-line human
summary):

```json
{
  "issue": 412,
  "attempt": 1,
  "backend": "opencode",
  "branch": "issue/412-a1",
  "checkout": "/workspace/.worklink/mimir/412-1",
  "started_at": "...", "finished_at": "...",
  "files_changed": ["mimir/saga/triples.py", "tests/test_memory_triples.py"],
  "diff_stat": "2 files changed, 31 insertions(+), 4 deletions(-)",
  "commands": [{"cmd": "...", "exit_code": 0}],
  "tests": {"cmd": "env -u MIMIR_MODEL_SPEC uv run pytest -q", "exit_code": 0,
             "summary": "4386 passed, 7 skipped"},
  "pr_url": "https://github.com/...",
  "status": "completed | blocked | failed",
  "blocked_reason": null,
  "transcript": "state/worklink/transcripts/412-1.jsonl"
}
```

New evidence writes only `checkout`. Continuation recovery still reads the
historical `worktree` key when `checkout` is absent, so existing evidence files
remain associated and do not need rewriting.

Validation (orchestrator-side, after the backend exits — never
self-reported):

- `worklink:review` requires `status=completed` AND non-empty diff AND
  a tests entry with exit 0 (or an explicit `tests.skipped_reason` the
  reviewer sees rendered).
- `status=completed` with an empty diff is demoted to `failed`
  ("readiness ≠ agent replied").
- `blocked` requires a non-empty `blocked_reason`; it is a first-class
  outcome, not a failure.

## 5. Pluggable backends

### Protocol

```python
@dataclass(frozen=True)
class Caps:
    tool_category: str        # "coding-cli" | "renderer" | "tracker" | "helper"
    persistent_sessions: bool # can resume a session across invocations
    json_output: bool         # machine-readable result stream
    native_pr_creation: bool  # can open the PR itself
    quota_pool: str | None    # e.g. "opencode" — see §6

@dataclass(frozen=True)
class WorkOrder:
    issue_id: int
    checkout: Path
    prompt: str               # rendered from mimir/prompt_templates/worklink-order.md
    rules: str | None         # backend-appropriate rules/system content
    timeout_s: int
    env: dict[str, str]       # explicit allowlist, assembled like poller env
    transcript_root: Path | None

@dataclass(frozen=True)
class RawResult:
    exit_code: int
    transcript_path: Path | None
    backend_status: str       # backend-specific; adapter maps to common terms
    error: str | None

class ToolBackend(Protocol):
    name: str
    def capabilities(self) -> Caps: ...
    def work_spec(self, order: WorkOrder, *, attempt: int, repo_url: str,
                  base_ref: str, branch: str, test_command: str) -> WorkSpec: ...
    async def interpret(self, order: WorkOrder, result: ComputeResult) -> RawResult: ...
```

Adapters own **only** CLI session mechanics: how to turn a
`WorkOrder` into a portable git-handoff `WorkSpec`, pass prompt/rules,
and map backend-specific output/errors into the common status terms. Everything else — claiming, checkouts, compute launch/wait/cancel/cleanup, evidence, transitions —
is orchestrator code shared by every backend. Adding a backend is one
file implementing the protocol plus a registry entry; it must never
require touching the orchestrator.

### Registered backends

| Adapter | Invocation sketch | Notes |
|---|---|---|
| `opencode` | `opencode run --dir <checkout> -- <prompt>` | Sole coding backend for leaf issues; provider and model are selected by opencode configuration/arguments. |
| `feature_factory` | `opencode run ... --command feature " --autonomous --max-retries 5 <issue>"` | Epic adapter with Worklink-supervised OpenCode and absolute 0.8.3 controls. |

Selection is config, not code (§7): per repo / label / issue-type, with
a per-category default. The executor consults `Caps` rather than
assuming (e.g. it opens the PR itself unless the backend both declares
and is configured for `native_pr_creation`).

### Execution substrate (the orthogonal `ComputeBackend` axis)

`ToolBackend` answers **what** builds (`opencode` for leaves or
`feature_factory` for epics). The
orthogonal `ComputeBackend` axis answers **where** it runs. **After the #832
substrate cleanup, the only Worklink compute substrate is
`local_subprocess`** (chainlink #832 retired `docker_sibling` and
`ecs_runtask`; the design-level home of the "containerized worker" idea and the
portable git-handoff `WorkSpec` remain in the code as #454, but no built-in
compute substrate other than `local_subprocess` ships today).

```python
@dataclass(frozen=True)
class WorkSpec:
    issue_id: int; attempt: int
    repo_url: str; base_ref: str; branch: str   # git handoff, not a local path
    prompt: str; rules: str | None
    test_command: str; backend: str; timeout_s: int
    creds_ref: dict[str, str]                    # substrate-resolved, value-blind
    env: dict[str, str]                          # explicit allowlist
    backend_config: dict[str, Any]               # tool-specific settings
    local_checkout: Path | None                  # local-subprocess compatibility only
    local_argv: tuple[str, ...] | None            # local-subprocess compatibility only

@dataclass(frozen=True)
class LaunchHandle:
    substrate: str
    identifier: str                              # pid (local_subprocess only today)

@dataclass(frozen=True)
class ComputeResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    launch_error: str | None = None
    handle: LaunchHandle | None = None
    command: tuple[str, ...] = ()                # local-subprocess audit trail

class ComputeBackend(Protocol):
    name: str
    def capabilities(self) -> ComputeCaps: ...
    async def launch(self, spec: WorkSpec) -> LaunchHandle: ...
    async def wait(self, h: LaunchHandle, timeout_s: int) -> ComputeResult: ...
    async def logs(self, h: LaunchHandle) -> str: ...
    async def cancel(self, h: LaunchHandle) -> None: ...
    async def cleanup(self, h: LaunchHandle) -> None: ...
```

The `WorkSpec` still carries git coordinates + the handle-based protocol
required by #454, so adding a future isolated compute substrate (a new
docker-sibling broker, ECS, k8s) is one builder function + one registry entry;
nothing in the orchestrator or the workorder render path depends on the
specific substrate.

**Operator posture (post-#832):** `local_subprocess` runs the backend as a
local subprocess with full container filesystem access. That is the **only**
built-in choice today and is gated by `defaults.allow_autonomous_local_subprocess`
for autonomous dispatch — there is no isolated alternative to fall back to.
The operator CLI (`mimir worklink run`, no `--autonomous`) is never gated.

**Historical substrate-design notes (#454, pre-#832):** the prior design
distinguished "Spawn a container is not a portable primitive" (the Docker
family exposes a daemon socket; ECS Fargate and k8s expose job APIs
`RunTask` / k8s `Job`); "Git handoff is the portable common denominator" (the
worker cloned the repo, ran the backend, ran tests, pushed the branch, emitted
evidence, and the orchestrator re-derived diff/tests from the pushed commit);
and "Observe-don't-trust survives" (the worker evidence was a hint, the
orchestrator's re-derivation was the gate). The `WorkSpec` carries every one of
those git-shaped fields today so a future isolated compute substrate can be
added without an orchestrator change.

### Backend trust model

The executor always isolates **git state** by running each backend in a
per-issue isolated checkout, pushing only an issue/attempt branch, and requiring
normal PR review before merge. It also uses an explicit environment
allowlist, so backend subprocesses do not inherit arbitrary ambient
secrets by default. Those are the durable safety rails.

That is **not the same as filesystem sandboxing**. A Worklink run on the sole
`local_subprocess` compute substrate has full container filesystem access from
the backend process. The checkout cwd limits where opencode is asked to
work, but it does not prevent reads or writes elsewhere in `/workspace`
or `/mimir-home` if the backend agent/tool chooses to do them. Treat
checkout isolation as an audit/review boundary, not a security boundary.

The configured base repository has one role: it is the independently provisioned
source from which attempts branch. It must not be the running controller's source
checkout. Checkout creation compares `WORKLINK_REPO` with the explicitly declared
`MIMIR_SOURCE_DIR` and refuses equal, ancestor, or descendant paths. This check does
not inspect `__file__`, the installed package root, or the process cwd. An editable
deployment must therefore set `MIMIR_SOURCE_DIR` to its controller checkout; omitting
that declaration makes separation conventional rather than enforced. A PyPI install
has no controller source checkout and leaves `MIMIR_SOURCE_DIR` unset, but it still
must configure a separately fetched or operator-provisioned `WORKLINK_REPO`.

The dedicated base remains mutable only by controller Git maintenance. Immediately
before every build Worklink refuses a dirty index/worktree, fetches the configured
base branch from `origin`, verifies the fetched tip, and branches from that tip.
Thus deployment upgrades do not move build inputs; the intended project and branch
relationship is expressed by `repositories.yaml` (`slug`, exact `origin`, and
`base_branch`) plus `worklink.yaml.repository`, and startup/fetch checks enforce it.
Updating the base through its origin does not alter installed controller code.

Local clone hardlinks remain available between this dedicated base and ordinary
attempt checkouts, so their object sharing cannot reach the controller source.
Worker-eligible uid-dropped checkouts already use `--no-hardlinks`, costing the
measured 179 MB per Mimir clone instead of approximately 0 MB. Foreign-owner
detection and hardlink-failure fallback remain unchanged.

This is acceptable for **operator-invoked** slice-1 runs on bounded issues
because the output still lands as a review PR and the executor observes
diff/tests itself. It is not acceptable as a silent autonomous-dispatch
boundary without a fresh risk decision. The pre-#832 options were:

1. **In-container sandbox (verified).** A one-line seccomp profile permitting
   user namespaces (`docs/internal/seccomp-userns.json`) was verified against
   the retired Codex path (#452). It is historical context, not a shipping
   Worklink backend or current opencode confinement guarantee.
2. **Out-of-container worker** via the `ComputeBackend` axis above
   (`docker-sibling` through a broker, or `ecs-runtask`) — chainlink #454. The
   portable path; the only isolation that survives a move to AWS.
3. **Accept-the-risk** `local-subprocess` (today's behavior) as an explicit,
   documented operator policy decision — recorded 2026-06-12 as a permitted
   fallback, not the default.

After #832 options 1 and 2 are no longer shipping compute substrates. The
seccomp/userns profile (1) is still a valid operator hardening for operators
who want to evaluate analogous confinement. Options 2 and 3 collapse: with no
isolated substrate shipped, the only shipping posture is the
`local_subprocess` accept-the-risk fallback (3), gated by
`defaults.allow_autonomous_local_subprocess` for autonomous dispatch. The
`ComputeBackend` plumbing (git-handoff `WorkSpec`, handle-based protocol) stays
in place so a future isolated substrate can be added without an orchestrator
change.


Planner-supplied `Suggested test command` text in Chainlink issue descriptions
is advisory only. It is shown to the backend as part of the issue body, but the
controller MUST NOT promote that planner-authored text into executable shell
input. Controller-side test execution uses only the explicit CLI override or the
operator-configured `worklink.yaml` default test command, which live in the same
operator/agent-private trust boundary as poller commands.

After the #832 substrate cleanup the only Worklink compute substrate is
`local_subprocess` (shared_filesystem=True), so the orchestrator always runs
the test command on the controller in the attempt checkout. The pre-#832
non-shared-filesystem evidence path (orchestrator fetches refs and runs `git
diff`, but must not check out the worker branch and run `test_command` on the
controller) is documented in #454 but is no longer reached by any shipping
code path.

Prompt content originates from chainlink issues (operator/planner-authored),
not arbitrary external text; the planner must not paste untrusted web/PR
content verbatim into acceptance criteria.

## 6. Mimir integrations

- **Quota arbitration.** A backend whose `quota_pool` matches the
  agent's own provider shares real quota. Autonomous dispatch (poller/tool) consults
  `HomeostaticArbiter.should_fire(priority=<worklink priority>)` —
  default `normal`, configurable — so worker launches shed under TIGHT
  with everything else. Operator-invoked `mimir worklink run` always
  proceeds.
- **Events.** Leaf and epic runs share `worklink_autonomous_refused`,
  `worklink_attempts_exhausted`, `worklink_claim_failed`, `worklink_claimed`,
  `worklink_transition`, and `worklink_run_failed`. Epic-only admission failures
  (a non-epic issue or an unavailable base branch) use
  `worklink_epic_refused`. Leaf runs additionally emit `worklink_evidence` after
  their controller evidence pass; epic runs deliberately do not duplicate that
  event because feature-factory owns its staged evidence lifecycle and the epic
  controller emits the terminal transition after completion verification. All
  transition events carry `status`, `review_ready`, and `pr_url`. These events
  land in events.jsonl with feedback rules (attempts-exhausted negative), so the
  agent sees its delegated work's health in the algedonic block. Existing event
  consumers match event names and read only their required fields; the optional
  `reason` added to epic refusal and transition records is additive.
- **Tool-pin inventory (external-toolchain drift).** `worklink.yaml`
  may carry deployment-specific `tool_pins:`, but the source-controlled
  seed inventory lives in `mimir.worklink.tool_pins.DEFAULT_TOOL_PINS`.
  The seed covers pinned external executables that affect agent/worklink
  operation: chainlink, mermaid-cli, osv-scanner, gmail-poller's gogcli
  helper, and the OpenCode CLI and plugins. Each entry records category,
  upstream lookup source, current pin, install surface, smoke command, and risk
  notes; guard tests compare those pins against Dockerfile/scaffold/
  skill-fragment literals so the inventory cannot silently drift from
  shipped installs. A low-priority maintenance poller inventories pins
  vs upstream and **files chainlink bump issues** with changelog/risk
  notes; those flow through this same pipeline (planner refines →
  executor bumps in an isolated checkout → the smoke command is the evidence).
  Version-drift checks are a tool-class concern, not a coding-CLI
  special case.

  Initial seed inventory:

  | Tool | Category | Source lookup | Current pin | Install surface | Smoke command | Risk surface |
  |------|----------|---------------|-------------|-----------------|---------------|--------------|
  | chainlink | `issue-cli` | GitHub release/tag `dollspace-gay/chainlink` | `chainlink-1.6.0` | bundled Chainlink skill `dockerfile.fragment` | `chainlink --version && chainlink issue ready` | High: Worklink coordination depends on issue, lock, comment, and dependency semantics. |
  | mermaid-cli | `renderer` | npm package `@mermaid-js/mermaid-cli` | `11.16.0` | scaffold Dockerfiles | `mmdc --version` | Low: renderer/Chromium drift affects diagram generation, not coding execution. |
  | gogcli | `integration-cli` | GitHub release/tag `steipete/gogcli` | `v0.9.0` | gmail-poller optional-skill `dockerfile.fragment` | `gog --version && gog gmail messages search 'in:inbox newer_than:1d' --account "$GOG_ACCOUNT" --max 1 --json --no-input` | High: pre-1.0 Gmail/Calendar helper drift can break polling subcommands on Muninn; large version jumps need an authenticated smoke before merge. |

## 6.5 Compute-backend autonomy policy (#460, #832)

Worklink composes on two orthogonal axes: the **ToolBackend** (*what* builds —
`opencode` or `feature_factory`; chosen by `backends:` +
`routes[].backend`) and the **ComputeBackend** (*where* it runs). After the #832
substrate cleanup the only Worklink compute substrate is `local_subprocess`
(chainlink #832 retired `docker_sibling` and `ecs_runtask`; the
`ComputeBackend` plumbing stays in place so a future isolated substrate can be
added without an orchestrator change). Both registered tool backends run on
local subprocess compute; a route matches first, otherwise the defaults apply.

`local_subprocess` is still **not a sandbox**. Poller-dispatched OpenCode is
uid-dropped to `worklink`, which prevents writes to the agent-owned source
checkout, but shared Worklink checkouts and the virtiofs-mounted credential home
remain reachable. Feature-factory retains the agent uid. This is therefore still
an **explicit accept-the-risk fallback** rather than an isolated substrate. With
no built-in isolated substrate to fall back to after #832, the only escape from
the autonomous refusal is the `allow_autonomous_local_subprocess: true` opt-in.

**Policy (enforced in the core executor, `WorklinkConfig.autonomous_compute_allowed`):**

- **Autonomous dispatch** — the ready-queue poller (which invokes `mimir
  worklink run --autonomous`) and the in-turn `worklink_run` tool (which passes
  `autonomous=True`) — **refuses** an unsandboxed substrate. With
  `defaults.compute: local_subprocess` and
  `defaults.allow_autonomous_local_subprocess: false` (the default), the run
  returns `refused` *before claiming* and the issue is left untouched.
  Autonomous use **requires** the opt-in (the pre-#832 "route to an isolated
  substrate" alternative is no longer available — there is no built-in
  isolated substrate to route to).
- **Operator-invoked `mimir worklink run`** (no `--autonomous`) is **never
  gated** — it always proceeds. Enabled OpenCode receives the same repo-checkout
  uid protection, but not credential or cross-run isolation; other backends keep
  the agent uid. Reserve manual runs for issues you've scoped and trust. The gate
  lives in core Python ahead of the claim, so no model-facing caller can bypass
  it.

## 7. Operator runbook

Slice 1 is intentionally operator-invoked. The executor is deterministic
plumbing around an external coding CLI; use it for small, well-scoped ready
leaf issues with explicit acceptance criteria and review criteria.

### Prerequisites

- Provision a dedicated clone for `WORKLINK_REPO`; do not use the checkout from
  which an editable Mimir controller runs. `mimir worklink run` requires either
  `WORKLINK_REPO` or an explicit `--repo` and never falls back to cwd. Point
  `--home` at the agent home that owns Chainlink and Worklink state
  (`/mimir-home` in production).
- The Chainlink tracker must have an agent identity for the executor. If lock
  claims fail with a missing-agent/identity error, initialize it once from the
  Chainlink repo: `cd /mimir-home && chainlink agent init mimir-worklink`.
- The issue should be an unblocked leaf, not a vague parent. Worklink expects
  concrete acceptance criteria plus review criteria; the prompt renderer passes
  those to the backend, and the evidence gate only checks observed mechanics.
- Configure opencode arguments and permissions in `<home>/worklink.yaml`. The
  backend is **not process-confined** on `local_subprocess`; see §5's trust
  model before using Worklink on sensitive issues or enabling autonomous dispatch.

Minimal current config shape:

```yaml
defaults:
  backend: opencode
  compute_backend: local_subprocess  # the only built-in compute substrate (#832)
  timeout_s: 1800
  priority: normal
  test_command: "uv run pytest -q"
  base_branch: main          # checkouts cut from + PRs target this branch

backends:
  opencode:
    bin: opencode
    # Replaces the built-in Mimir-repo list: ["git *", "uv *"].
    # OpenCode evaluates the last matching pattern. Worklink always installs a
    # leading "*": deny rule, then these grants; "*" itself is rejected.
    bash_allowlist: ["git *", "uv *"]
    args: []                 # Worklink injects the configured model and --dir
  feature_factory:
    entrypoint: /opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js
```

The `compute_backends:` stanza is not needed in normal configs — it is only
required to add a future isolated substrate (a docker-sibling broker, ECS
RunTask, k8s Job) without changing the orchestrator. Unknown compute-backend
names fail clean at config-load time; `local_subprocess` is the only built-in.

### Dry run

Before a first real run on a new issue shape, render the work order without
claiming or spawning the backend:

```bash
mimir worklink run <issue-id> --home /mimir-home --dry-run
```

A dry run is a prompt/config validation step only. It does not create a claim,
checkout, evidence bundle, branch, or PR.

### Real run

```bash
mimir worklink run <issue-id> \
  --home /mimir-home \
  --test-command 'uv run pytest -q <focused-tests> --tb=short'
```

Expected success output is shaped like:

```text
worklink #433 attempt 1: completed review-ready PR https://github.com/.../pull/645
evidence: /mimir-home/state/worklink/evidence/433-1.json
```

On success Worklink should leave the Chainlink issue labeled
`worklink:review`, write an evidence bundle under
`<home>/state/worklink/evidence/`, write a backend transcript under
`<home>/state/worklink/transcripts/`, push `issue/<id>-a<attempt>`, and open a
GitHub PR. The normal review/merge path still applies; Worklink does not make
its own PR trustworthy.

### Evidence inspection

Evidence is executor-observed, not backend self-report. Inspect it directly:

```bash
jq . /mimir-home/state/worklink/evidence/<issue>-<attempt>.json
```

The load-bearing fields are:

- `status`: `completed`, `failed`, or `blocked`.
- `files_changed` and `diff_stat`: collected with git by the executor.
- `commands`: observed command summaries, including diff/status checks.
- `tests`: the exact test command, exit code, and captured summary.
- `transcript`: path to the backend JSON transcript outside the checkout.
- `pr_url`: review PR if the evidence gate passed.

Do not treat a backend's prose as evidence when these fields disagree. Empty
diff with a nominal backend success is demoted to failure.

### Recovery and cleanup

For the current mimirbot migration, first stop new dispatch and let in-flight
claims finish (or stop them through the normal verified Worklink stop path). Clone
the configured Mimir origin as the controller account into a persistent path such
as `/var/lib/mimir-worklink/base/mimir`, configure that path as the inventory root,
set `WORKLINK_REPO` to it, and keep `MIMIR_SOURCE_DIR=/workspace/mimir` while the
controller remains an editable checkout. Restart only after startup validates the
new root and origin. Existing attempt checkouts retain their own Git directories
and recorded base path; do not move them mid-claim. Retained completed/failed
checkouts may be pruned after the switch by the old-path cleanup procedure. A host
that has never had a base must be provisioned with this clone before startup;
Worklink does not guess a checkout or clone destination on first use and fails
closed when `WORKLINK_REPO` is absent.

Current slice-1 recovery is manual:

- **PR remediation checkout lease:** never rebase inside a lease whose destination
  publication policy does not permit a force-with-lease push. Use `repo_merge` to
  merge `main` into the PR branch instead; this preserves a fast-forward push and
  lets a later turn reacquire committed or uncommitted lease work. Force-push
  approval remains turn-scoped, so any permitted rewritten push must be approved
  and published by the same turn that holds the lease.
- **Stranded rebased PR lease:** do not reset or delete the checkout. Preserve its
  HEAD as a verified Git bundle under the lease root's `.recovery/` directory
  (and archive dirty worktree files), then have the operator inspect and publish
  the recovered commits with an explicit force-with-lease operation. Remove the
  stranded checkout only after publication is independently verified.
- **Parked factory epic:** when factory ownership is the cause, run the factory's
  `amend-paths` recovery first, then relaunch the epic normally. Worklink verifies
  the retained sandbox and resumes it in place with the retained session. A
  verification mismatch applies `worklink:blocked` and reports the sandbox; it
  does not claim a fresh attempt. Discarding the record requires the explicit
  `mimir worklink archive-factory-run <issue-id>` operator action.
- **Factory sandbox cleanup:** treat `.factory/<run-id>`, its plan, artifacts, and
  reviews like unpublished commits. Worklink does not sweep a parked, completed,
  failed, or unresumed factory sandbox. Removal is permitted only after the
  factory's own handoff terminalizes the run, fetches permitted local refs,
  archives and verifies the control plane outside the sandbox, and removes the
  guarded sandbox. If any handoff phase fails, leave the sandbox in place.

- **Backend/sandbox failure before useful diff:** read the transcript path in
  the evidence bundle or `<home>/state/worklink/transcripts/`, adjust
  `<home>/worklink.yaml`, and rerun only if the Chainlink comments/attempt
  state indicate the next attempt will use a fresh attempt number.
- **Claim lock left behind:** list capacity-consuming ids with `cd /mimir-home &&
  chainlink locks list --json`, inspect one with `chainlink locks check
  <issue-id>`, and release the executor's own known-stale lock with `chainlink
  locks release <issue-id>`. The Worklink TTL reaper also discovers lock-only
  claims and releases them after their structured claim heartbeat is stale. Use
  `locks steal` manually only after independent TTL/heartbeat evidence;
  Chainlink can report a fresh lock as stale when no heartbeat has been written
  yet.
- **Claims leaked before the lifecycle telemetry fix:** inspect
  `<home>/logs/events.jsonl` for `worklink_reattach_dispatch_failed`,
  `worklink_shutdown_claim_release_failed`, and `worklink_claim_reap_skipped`.
  Compare their issue ids with `chainlink locks list --json` and current
  `worklink:in-progress` issues. A `worklink_claims_reaped` event with
  `examined=0` means no stale claim was found; nonzero `skipped` counts identify
  why stale candidates remained among records discovered in that pass. Skip
  issue ids are bounded samples, so use the lock table as the complete inventory
  before manually releasing anything.
- **Concurrency-cap refusal:** the current refusal reports only active and cap,
  not the blocking lock ids. It should include a bounded id sample in a separate
  observability change; this fix leaves cap behavior and payloads unchanged.
- **Retained failed checkout/branch:** failed or blocked attempts are retained
  for autopsy under `.worklink/<issue>-<attempt>` with branch
  `issue/<issue>-a<attempt>`. Remove them only after evidence has been copied
  or is no longer needed. Successful attempts are cleaned up automatically.
- **Attempt/branch collision:** if reruns reuse `attempt=1` and collide with
  `issue/<id>-a1`, do not keep retrying. The current parser likely missed prior
  structured claim comments; file/fix the parser gap before trusting retries.
- **Stale async wake-up:** shell completion logs can report an earlier failed
  run after a later completed run. Compare Chainlink labels, the newest evidence
  bundle, PR state, and current branch state before rerunning.

### Closeout checklist for a Worklink leaf

1. Chainlink target issue has a `WORKLINK_EVIDENCE` comment and
   `worklink:review` label.
2. Evidence JSON validates the changed files, diff stat, test command, and exit
   code.
3. GitHub PR is open, mergeable, and green.
4. Normal review/merge has happened; only then close the original leaf issue.
5. Any manual intervention or rail gap found during the run is captured either
   in the slice postmortem or as a follow-up Chainlink issue.

## 8. Configuration (`<home>/worklink.yaml`)

### Deploying the 0.7 migration to an existing home

`backends.feature_factory` changed shape. The retired `bin`, `args`, `ready` and
`reviewer` keys are now **rejected at config load**, which raises before any dispatch
runs. Worklink crashes emit no events, so an unmigrated `<home>/worklink.yaml` fails
*silently*: nothing appears in `events.jsonl` and the only trace is `run-<id>.log`.
Stale config of exactly this shape has already caused one full dispatch outage — the
retired `codex` backend block, 2026-07-30.

Edit `<home>/worklink.yaml` **before or with** the image rebuild:

```yaml
backends:
  feature_factory:
    # remove:  bin: node /workspace/feature-factory/src/cli.js
    # remove:  args: []
    entrypoint: /opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js
```

`entrypoint` may be omitted entirely when `MIMIR_FACTORY_ENTRYPOINT` is set; the image
sets it to the path above. This is an image-layer change, so redeploy with
`docker compose -p <project> up -d --build` — a plain restart does not pick it up.


```yaml
defaults:
  backend: opencode
  compute_backend: local_subprocess # the only built-in Worklink compute substrate (#832)
  timeout_s: 1800
  priority: normal          # arbiter priority for autonomous dispatch (low|normal|high)
  max_concurrent: 2         # cap on concurrent autonomous claims (poller + tool); CLI uncapped
  reaper_ttl_s: 86400       # >= 2 * max(timeout_s, factory timeout); lower legacy values warn + clamp
  allow_autonomous_local_subprocess: false  # autonomy policy (#460, #832): autonomous dispatch
                            # refuses the unsandboxed local_subprocess substrate unless this
                            # is true. The operator CLI is never gated. See §6.5.
  test_command: "uv run pytest -q"
  max_review_retries: 3             # reviewer-requested rebuild attempts before blocking a leaf
  # Retained-but-INERT since #830 (integrated-epic runner removed). These parse
  # for back-compat but no code consumes them; safe to omit:
  epic_branch_prefix: "epic/"       # (inert) was the integrated-epic branch prefix
  reviewer_backend: opencode        # (inert) was the epic per-slice reviewer backend
  tiered_review:
    # Glob patterns matched against any scope path with fnmatch; `**` is supported.
    # If set, this list REPLACES the framework defaults below; it is not merged.
    high_risk_scope_patterns:
      - "**/migrations/**"
      - "**/*migration*"
      - "**/schema.sql"
      - "**/*auth*"
      - "**/*oauth*"
      - "**/*credential*"
      - "**/*secret*"
      - "**/generated/**"
      - "**/*_pb2.py"
      - "*.lock"
      - "**/*.lock"
      - ".github/workflows/**"
      - "**/Dockerfile*"
      - "**/*.tf"
    high_risk_labels:
      - "risk:high"
      - "security"
      - "auth"
      - "migration"
      - "prod-data"
      - "generated-code"
      - "hotspot"
    multi_vote_reviewer_count: 3     # reviewer count for high-risk leaves

routes:                     # first match wins
  - repo: "jasoncarreira/mimir"
    backend: opencode

backends:                   # ToolBackend adapters — WHAT builds
  opencode:
    bin: opencode
    args: []                 # -m/--model, --dir, and -- are backend-owned
  feature_factory:
    entrypoint: /opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js

# Backend blocked path: coding CLIs can deliberately route planner/human issues
# back to Chainlink by printing `WORKLINK_BLOCKED: <one-line reason>`. This is
# for design contradictions or missing prerequisites, not transient tool errors.

compute_backends:           # ComputeBackend launchers — WHERE it runs (#454).
                            # Today only ``local_subprocess`` is a built-in
                            # substrate; the stanza is only needed when adding
                            # a future isolated compute (docker-sibling broker,
                            # ECS RunTask, k8s Job) without an orchestrator
                            # change. Unknown compute_backend names fail clean
                            # at config-load time.
  local-subprocess: {}      # the built-in default; accept-the-risk fallback
```

Invalid or unknown tool-backend and compute-backend fields fail closed during
registry construction instead of falling back to another backend. Remove any
vestigial `backends.codex`, `backends.claude_cli`, or routes/defaults selecting
those retired names from operator-owned `worklink.yaml` files.

Worklink's OpenCode backend passes a per-run `OPENCODE_PERMISSION` override with
`external_directory: {"/**": "deny"}` and `bash: {"*": "deny", ...allowlist}`.
This is deliberately `deny`, never `ask`: Worklink runs headlessly, so an
approval request would wait forever. A denied tool call is returned by OpenCode
as a permission error and recorded in the Worklink transcript rather than
opening an interactive prompt. Configure `backends.opencode.bash_allowlist` with
the command patterns required by the selected repository's test runner, package
manager/build, and Git workflow. The list replaces the defaults; an empty list
denies every shell command.

This policy is defense-in-depth for **trusted code work only**, not process
confinement. OpenCode's file/path checks and shell command matcher run inside the
OpenCode process. An allowed command and its arguments still have the host
user's filesystem and network authority, so untrusted code work remains
notify-only until Worklink has an isolated compute substrate.

`tiered_review` and the inert `epic_branch_prefix`/`reviewer_backend`,
`max_review_retries`, and `max_claim_attempts` settings
live under `defaults`. Since #830 removed the integrated-epic runner these are
retained only for config back-compat (no code consumes them; the
`tiered_review` risk classifier went with the epic reviewer). The fields are
documented here so an older deployment `worklink.yaml` still parses.
`tiered_review.high_risk_scope_patterns` uses `fnmatch`
matching against scope paths, including a leading-root variant, so patterns can
match anywhere in a scope path and `**` works for nested paths. The framework
defaults are intentionally ecosystem-agnostic: migrations/schema, auth,
OAuth/secrets/credentials, generated code, lockfiles, and CI/CD or infra files.
A deployment should add its own sensitive surfaces here, such as access-control
or production-specific config paths.

Known limitation: setting `tiered_review.high_risk_scope_patterns` replaces the
framework defaults instead of merging with them. To keep the generic defaults
while adding deployment-specific patterns, list both the defaults and the local
patterns in `worklink.yaml`.

Multi-review is selected when any high-risk signal matches: the decomposer
assigns `risk="high"`, a leaf has one of `high_risk_labels`, or a scope path
matches one of `high_risk_scope_patterns`. These signals are OR-combined; an
assigned high-risk leaf is never downgraded, and a pattern hit remains high-risk
even if another signal is absent.

```yaml
tool_pins:
  - name: opencode               # required: stable local tool name
    category: coding-cli         # required: coding-cli | renderer | tracker | helper
    pin: "1.18.21"              # required: version, tag, or SHA currently expected
    smoke: "opencode --version"   # required: command used as bump evidence
    source: npm                  # optional lookup strategy for drift checks
    package: "opencode-ai"       # optional upstream package/repo identifier
  - name: chainlink
    category: tracker
    pin: "git+dollspace-gay/chainlink@<sha>"
    smoke: "chainlink --help"
    source: github-release
    repo: dollspace-gay/chainlink
  - name: mermaid-cli
    category: renderer
    pin: "11.x"
    smoke: "mmdc --version"
```

## 9. Rollout slices (= the #380 subissues)

1. **Slice 0 — lock probe.** Empirically verify `chainlink locks claim`
   atomicity across processes; decide claim mechanism; document in this
   spec.
2. **Slice 1 — vertical, operator-invoked.** `mimir worklink run
   <issue>`: validate-leaf → claim → isolated checkout → coding adapter →
   observed evidence → transitions → PR. Dry-run on one non-critical
   issue (an adversarial-review LOW is a good guinea pig).
3. **Slice 2 — planner.** `mimir/prompt_templates/decompose.md` +
   `chainlink-orchestrator` skill + executor template-refusal test.
4. **Slice 3 — autonomy (#444, shipped).** Ready-queue poller
   (`chainlink-orchestrator` skill `pollers.json`/`scripts/poller.py`, `priority: normal`
   so the scheduler sheds it under TIGHT; dispatches detached `mimir worklink
   run` up to `defaults.max_concurrent`, default 2), `worklink_run` core tool
   (arbiter-gated via `HomeostaticArbiter.should_fire` + cap; operator CLI
   bypasses both), and a TTL reaper scheduler callable (`worklink-reaper`,
   opt-in via `MIMIR_WORKLINK_REAPER_CRON`) that recovers stale claims using
   `defaults.reaper_ttl_s`. Per-issue exclusivity stays guaranteed by the
   Chainlink lock. Gated on an isolation posture (see the trust model) — the
   `ComputeBackend` axis (#454) and/or the verified in-container seccomp profile
   (#452). Autonomous `local-subprocess` requires the explicit accept-the-risk
   opt-in.
5. **Slice 4 — second adapter.** Historical `claude_cli` protocol-parity work;
   retired with the legacy Codex adapter after opencode became the sole coding path.
6. **Slice 5 — tool pins.** Inventory + drift poller + bump-issue
   filing.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Chainlink locks not atomic cross-process | Slice 0 probe; O_EXCL fallback; upstream issue |
| Backend session brittleness | Start operator-invoked (slice 1); autonomy only after dry-run |
| Unsandboxed backend filesystem access | Verified in-container seccomp/userns profile confines bwrap `workspace-write` (#452, `docs/internal/seccomp-userns.json`); `local_subprocess` (the only built-in compute substrate after #832, today's `--sandbox danger-full-access`) is the explicit accept-the-risk fallback, gated before slice-3 autonomous dispatch |
| Evidence gaming by the backend agent | Orchestrator observes diff/tests itself; empty-diff demotion |
| Checkout cost under concurrency | Attempt checkouts are per-issue and short-lived; cap concurrent claims (default 2) |
| Shared quota exhaustion | `quota_pool` + arbiter gating; operator runs bypass |
| Stale claims wedging issues | TTL reaper + `locks steal` + attempts cap |
