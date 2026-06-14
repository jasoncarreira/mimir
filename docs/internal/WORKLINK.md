# WORKLINK — Chainlink worker orchestration (chainlink #380)

Mimir-native, toolchain-agnostic orchestration for durable work
decomposition and execution. Chainlink is the coordination surface and
source of truth; mimir plans; pluggable coding/maintenance CLIs build;
deterministic machinery connects them.

Status: Slice 1 vertical implemented: manual `mimir worklink run` can claim a
ready leaf, spawn Codex, observe evidence, push a branch, and open a review PR.
Slice 2 adds the planner/decomposer contract (prompt + skill + executor refusal). Later slices still need poller/tool autonomy and additional backends.
Owner issue: chainlink #380; leaf issues are subissues of #380.

## 1. Roles

The open-strix `chainlink-worker` pattern is split into two roles so a
vague parent issue can never be handed directly to a coding agent:

| Role | Who | Intelligence | Output |
|---|---|---|---|
| **Planner / decomposer** | mimir, in LLM turns | mimir's model | Ready *leaf* issues: acceptance criteria, dependency edges, review criteria, labels |
| **Executor** | `mimir/worklink/` — deterministic machinery (no model calls of its own) | the **backend CLI's** own agent loop (codex / claude / cursor / …) | A claimed issue → worktree → backend run → **observed** evidence bundle → state transition |

The executor is a process supervisor. It claims, prepares, spawns,
observes, and reports. All building intelligence lives inside the
spawned backend subprocess, on that tool's own model account and
session mechanics. Up to three model contexts touch one leaf issue —
planner turn (decompose), backend loop (build), reviewer (PR review) —
with the executor as the auditable connective tissue.

Why the split is load-bearing:

- **State transitions never depend on a model following instructions.**
  Claim/evidence/transition is plain Python; the model can only affect
  the worktree contents.
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

Labels on the leaf issue (chainlink `status` stays open/closed):

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
Chainlink mutations. Use the bundled `chainlink-orchestrator` skill and the
operator-tunable `prompts/decompose.md` prompt to turn a parent issue into leaf
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
  orchestrator.py   # claim → worktree → backend → evidence → transition
  claims.py         # lock protocol + TTL reaper (chainlink locks or O_EXCL fallback)
  worktree.py       # per-issue git worktree lifecycle
  evidence.py       # schema, validation, observation (diff/tests run by US)
  backends/
    __init__.py     # registry + capability resolution
    base.py         # ToolBackend protocol, Caps, WorkOrder, RawResult
    codex.py        # first adapter
    claude_cli.py   # second adapter (mechanical addition)
  compute.py       # ComputeBackend protocol, WorkSpec, LaunchHandle, local-subprocess
  # future compute launchers: docker-sibling broker, ecs-runtask worker
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
3. **Worktree.** `git worktree add .worklink/<issue>-<attempt> -b
   issue/<issue>-a<attempt>` from the **configured base branch**
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
   **attempt-scoped**, because failed/blocked worktrees are retained for autopsy
   (§3 step 8): with issue-scoped names the second attempt would
   collide with the first attempt's kept worktree AND its branch, and
   `attempts < 3` could never actually run more than once. Each retry
   gets a fresh path + branch; autopsy artifacts from prior attempts
   stay untouched until the reaper prunes them. Never a long-lived
   per-worker branch.
4. **Render the prompt** from issue fields: description, acceptance
   criteria, review criteria, parent context, repo conventions pointer.
   Template lives at `prompts/worklink-order.md` (operator-tunable). The
   planner-side decomposition prompt lives at `prompts/decompose.md`; its
   `{leaf_template}` slot is rendered from the single
   `mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN` contract, which the
   `chainlink-orchestrator` skill also embeds, so the executor validates the
   same template the planner emits.
5. **Spawn the backend** (adapter `run(WorkOrder)`) under a timeout.
6. **Observe evidence** (`evidence.py`): for shared-filesystem compute,
   the executor itself runs `git -C <worktree> diff --stat`/`status`, runs
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
8. **Cleanup.** Remove the attempt's worktree on success; keep on
   `failed`/`blocked` for autopsy (the reaper prunes attempt worktrees
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
  "backend": "codex",
  "branch": "issue/412-a1",
  "worktree": ".worklink/412-1",
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
    worktree_safe: bool       # tolerates --cd into an arbitrary worktree
    quota_pool: str | None    # e.g. "codex-subscription" — see §6

@dataclass(frozen=True)
class WorkOrder:
    issue_id: int
    worktree: Path
    prompt: str               # rendered from prompts/worklink-order.md
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
and map backend-specific output/errors into the common status terms. Everything else — claiming, worktrees, compute launch/wait/cancel/cleanup, evidence, transitions —
is orchestrator code shared by every backend. Adding a backend is one
file implementing the protocol plus a registry entry; it must never
require touching the orchestrator.

### Initial adapters

| Adapter | Invocation sketch | Notes |
|---|---|---|
| `codex` (first) | `codex exec --cd <worktree> --json <prompt>` | Already installed in the agent containers; JSON output; shares the ChatGPT-account quota pool with codex-routed agents |
| `claude_cli` (second) | `claude -p <prompt> --output-format json` in worktree cwd | Implemented as a protocol-parity adapter; proves the interface is mechanical; separate Max-plan pool. Requires a deployment image with the `claude` CLI installed before real runs. |
| `cursor` / others | per their headless CLIs | Added on demand |

Selection is config, not code (§7): per repo / label / issue-type, with
a per-category default. The executor consults `Caps` rather than
assuming (e.g. it opens the PR itself unless the backend both declares
and is configured for `native_pr_creation`).

### Execution substrate (the orthogonal `ComputeBackend` axis)

`ToolBackend` answers **what** builds (codex / claude / cursor). A second,
orthogonal axis answers **where** it runs: `ComputeBackend` ∈
{`local-subprocess`, `docker-sibling`, `ecs-runtask`, `k8s-job`}. This is the
portable, design-level home of the "containerized worker" idea, generalized so
the same Worklink runs on OrbStack today and AWS ECS/Fargate later. Full
design: chainlink #454.

Four facts shape it:

- **"Spawn a container" is not a portable primitive.** The Docker family
  (OrbStack, Docker Desktop, plain Docker, ECS-on-EC2) exposes a daemon socket;
  ECS Fargate and Kubernetes do not — they have job APIs (`RunTask`, k8s `Job`).
  A socket implementation covers the Docker family but is impossible on Fargate.
  (The seccomp/userns profile below likewise does not port to Fargate.) So the
  abstraction is required, not `docker run` baked into the orchestrator.
- **Git handoff is the portable common denominator.** The orchestrator today
  assumes a *local* worktree it creates, a *local* backend subprocess, and
  *local* `git diff`/test observation — a remote worker breaks all three.
  Instead of shipping a worktree, ship a git ref: the `WorkSpec` carries
  `{repo_url, base_ref, branch, prompt, test_command, creds_ref, …}`. The
  bundled worker entrypoint (`python -m mimir.worklink.worker` or
  `mimir worklink worker <payload.json>`) accepts a `WorkerPayload`, clones or
  fetches the repo, checks out the base ref, creates/resets the attempt branch,
  runs the selected `ToolBackend`, runs tests, **pushes the branch**, and emits
  `evidence.json`. Bind-mounting a worktree is a Docker-only optimization (and
  the sibling-mount footgun: `-v /path` resolves on the *daemon* host, not the
  parent container's namespace).
- **Observe-don't-trust survives.** The orchestrator can no longer diff a local
  worktree, so it **fetches the pushed branch and re-derives** diff/tests from
  `base..head` itself. The worker's `evidence.json` is a hint; the orchestrator's
  re-derivation from the immutable pushed commit is the gate. Empty-diff demotion
  and tests-exit-0 carry over unchanged. The worker payload
  (clone→checkout→backend→tests→push→evidence) is identical across substrates;
  only the launcher differs (Docker reuses the agent image, AWS publishes to ECR).
- **The Docker socket is root-equivalent** on the host VM, so the `docker-sibling`
  launcher is a **broker**: a tiny service *outside* the agent container owns the
  socket and accepts only one narrow request ("run worklink job N on branch B"),
  building the spec itself (worktree-only, no extra caps). On ECS the equivalent
  is a scoped IAM task role permitted to `RunTask` only the worklink task def.

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
    local_worktree: Path | None                  # local-subprocess compatibility only
    local_argv: tuple[str, ...] | None            # local-subprocess compatibility only

@dataclass(frozen=True)
class WorkerPayload:
    spec: WorkSpec
    repo_dir: Path                                # worker-local clone directory
    evidence_path: Path                           # where evidence.json is written
    transcript_root: Path | None = None           # outside worktree when possible
    safe_env: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class LaunchHandle:
    substrate: str
    identifier: str                              # pid / container id / ECS task ARN

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

**Slice #455 implementation note.** The first implemented substrate is
`local-subprocess`, so it necessarily uses `local_worktree` to preserve today's
manual in-container behavior. The same `WorkSpec` still carries the git
coordinates and handle-based protocol required by #454; Docker/ECS slices should
ignore `local_worktree` and use the git handoff fields.

**Operator decision (2026-06-12):** `local-subprocess` (today's behavior — the
backend runs as a local subprocess with full container filesystem access)
remains available as an explicit **accept-the-risk fallback**. The isolated
worker (`docker-sibling` / `ecs-runtask`) is the recommended path for
unsandboxed or autonomous use; operators may opt back into `local-subprocess`
and accept the documented blast radius. The safe path should be easy, while the
risky path stays possible for bounded operator-invoked runs.

### Backend trust model

The executor always isolates **git state** by running each backend in a
per-issue worktree, pushing only an issue/attempt branch, and requiring
normal PR review before merge. It also uses an explicit environment
allowlist, so backend subprocesses do not inherit arbitrary ambient
secrets by default. Those are the durable safety rails.

That is **not the same as filesystem sandboxing**. In the current agent
container, the Codex CLI only works with `--sandbox danger-full-access`;
the default bwrap sandbox fails before useful work starts. Therefore a
Codex-backed Worklink run has full container filesystem access from the
backend process. The worktree cwd limits where the backend is asked to
work, but it does not prevent reads or writes elsewhere in `/workspace`
or `/mimir-home` if the backend agent/tool chooses to do them. Treat
worktree isolation as an audit/review boundary, not a security boundary.

This is acceptable for **operator-invoked** slice-1 runs on bounded issues
because the output still lands as a review PR and the executor observes
diff/tests itself. It is not acceptable as a silent autonomous-dispatch
boundary without a fresh risk decision. Before slice 3 turns on ready-queue
polling or in-turn `worklink_run` dispatch for Codex, pick an isolation
posture — the options are now concrete (chainlink #452 / #454):

1. **In-container sandbox (verified).** A one-line seccomp profile permitting
   user namespaces (`docs/internal/seccomp-userns.json`) lets Codex's own bwrap
   `--sandbox workspace-write` confine writes to the worktree and deny network —
   verified model-free on the agent image (#452). Trade-off: userns widens the
   container's kernel attack surface. Does **not** port to ECS Fargate.
2. **Out-of-container worker** via the `ComputeBackend` axis above
   (`docker-sibling` through a broker, or `ecs-runtask`) — chainlink #454. The
   portable path; the only isolation that survives a move to AWS.
3. **Accept-the-risk** `local-subprocess` (today's behavior) as an explicit,
   documented operator policy decision — recorded 2026-06-12 as a permitted
   fallback, not the default.


The planner-supplied `Suggested test command` is executable shell input
(`shell=True`) from Chainlink issue descriptions, within the same
operator/agent-private trust boundary as poller commands.

For non-shared-filesystem compute substrates, controller-side evidence
re-derivation is intentionally diff-only for now: the orchestrator fetches refs
and runs `git diff`, but it must **not** check out the worker branch and run
`test_command` on the controller. Test re-derivation for remote workers needs a
fresh sandboxed compute job in the same substrate; until that exists, remote
evidence marks tests `observed=false`, so the gate fails closed instead of
executing backend-authored branch code on the controller.

Prompt content originates from chainlink issues (operator/planner-authored),
not arbitrary external text; the planner must not paste untrusted web/PR
content verbatim into acceptance criteria.

## 6. Mimir integrations

- **Quota arbitration.** A backend whose `quota_pool` matches the
  agent's own provider shares real quota (mimirbot + codex workers =
  one ChatGPT account). Autonomous dispatch (poller/tool) consults
  `HomeostaticArbiter.should_fire(priority=<worklink priority>)` —
  default `normal`, configurable — so worker launches shed under TIGHT
  with everything else. Operator-invoked `mimir worklink run` always
  proceeds.
- **Events.** `worklink_claimed`, `worklink_evidence`,
  `worklink_transition`, `worklink_attempts_exhausted` land in
  events.jsonl with feedback rules (attempts-exhausted negative), so
  the agent sees its delegated work's health in the algedonic block.
- **Tool-pin inventory (external-toolchain drift).** `worklink.yaml`
  may carry deployment-specific `tool_pins:`, but the source-controlled
  seed inventory lives in `mimir.worklink.tool_pins.DEFAULT_TOOL_PINS`.
  The seed covers pinned external executables that affect agent/worklink
  operation: codex, chainlink, mermaid-cli, optional claude-code, and
  gmail-poller's gogcli helper. Each entry records category, upstream
  lookup source, current pin, install surface, smoke command, and risk
  notes; guard tests compare those pins against Dockerfile/scaffold/
  skill-fragment literals so the inventory cannot silently drift from
  shipped installs. A low-priority maintenance poller inventories pins
  vs upstream and **files chainlink bump issues** with changelog/risk
  notes; those flow through this same pipeline (planner refines →
  executor bumps in a worktree → the smoke command is the evidence).
  Version-drift checks are a tool-class concern, not a coding-CLI
  special case.

  Initial seed inventory:

  | Tool | Category | Source lookup | Current pin | Install surface | Smoke command | Risk surface |
  |------|----------|---------------|-------------|-----------------|---------------|--------------|
  | codex | `coding-cli` | npm package `@openai/codex` | `0.137.0` | scaffold Dockerfiles when `codex-plus` is selected | `codex --version && env -u MIMIR_MODEL_SPEC uv run pytest -q tests/test_worklink_backends.py` | High: first Worklink coding backend; CLI drift can affect prompt execution, sandbox flags, transcript shape, and quota use. |
  | chainlink | `issue-cli` | GitHub release/tag `dollspace-gay/chainlink` | `chainlink-1.6.0` | bundled Chainlink skill `dockerfile.fragment` | `chainlink --version && chainlink issue ready` | High: Worklink coordination depends on issue, lock, comment, and dependency semantics. |
  | mermaid-cli | `renderer` | npm package `@mermaid-js/mermaid-cli` | `11.15.0` | scaffold Dockerfiles | `mmdc --version` | Low: renderer/Chromium drift affects diagram generation, not coding execution. |
  | claude-code | `coding-cli` | npm package `@anthropic-ai/claude-code` | `2.1.168` | root/scaffold Dockerfiles when `MIMIR_ENABLE_CLAUDE_CODE=1` | `claude --version` | Medium: optional second coding backend; real smoke requires a deployment with `claude` installed. |
  | gogcli | `integration-cli` | GitHub release/tag `steipete/gogcli` | `v0.9.0` | gmail-poller optional-skill `dockerfile.fragment` | `gog --version` | Medium: Gmail/Calendar helper drift can break polling independent of Worklink coding backends. |

## 7. Operator runbook

Slice 1 is intentionally operator-invoked. The executor is deterministic
plumbing around an external coding CLI; use it for small, well-scoped ready
leaf issues with explicit acceptance criteria and review criteria.

### Prerequisites

- Run from the target git repository (`/workspace/mimir` for mimir source
  work) and point `--home` at the agent home that owns Chainlink and Worklink
  state (`/mimir-home` in production).
- The Chainlink tracker must have an agent identity for the executor. If lock
  claims fail with a missing-agent/identity error, initialize it once from the
  Chainlink repo: `cd /mimir-home && chainlink agent init mimir-worklink`.
- The issue should be an unblocked leaf, not a vague parent. Worklink expects
  concrete acceptance criteria plus review criteria; the prompt renderer passes
  those to the backend, and the evidence gate only checks observed mechanics.
- Configure backend quirks in `<home>/worklink.yaml`. In the current production
  container Codex requires `--sandbox danger-full-access`; the default bwrap
  sandbox can fail before any code runs. That means the backend is **not
  filesystem sandboxed**; see §5's trust model before using Worklink on
  sensitive issues or enabling slice-3 autonomous dispatch.

Minimal current config shape:

```yaml
defaults:
  backend: codex
  compute_backend: local_subprocess  # accept-the-risk fallback; docker-sibling below is selectable once configured
  timeout_s: 1800
  priority: normal
  test_command: "env -u MIMIR_MODEL_SPEC uv run pytest -q"
  base_branch: main          # worktrees cut from + PRs target this branch

backends:
  codex:
    bin: codex
    args: ["exec", "--json", "--sandbox", "danger-full-access"]
  claude_cli:
    bin: claude
    args: ["-p", "--output-format", "json"]

compute_backends:
  docker-sibling:
    broker_url: "unix:///run/worklink-broker.sock"
    image: mimirbot-mimirbot
    policy: {}
```

The agent-side `docker-sibling` backend is a broker **client**, not a Docker
launcher. Its contract is deliberately narrow: `POST /jobs` submits `{image,
policy, worker_payload}`, `POST /jobs/<id>/wait` returns bounded compute result
fields, `GET /jobs/<id>/logs` returns text/JSON logs, `POST /jobs/<id>/cancel`
cancels, and `DELETE /jobs/<id>` cleans up. The submitted worker payload is the
same JSON schema consumed by `mimir worklink worker <payload.json>`; do not add a
second Docker-only work schema. The client supports `unix://`, `http://`, and
`https://` broker URLs and contains no direct docker CLI or docker.sock access.

`claude_cli` is registered by default but is only runnable in deployments that
actually install the `claude` binary. The current production container may omit
it when Claude Code CLI installation is disabled; in that state the adapter can
be selected/configured and unit-tested, but a real Worklink smoke run must wait
for an image with `claude` on `PATH`.

### Dry run

Before a first real run on a new issue shape, render the work order without
claiming or spawning the backend:

```bash
cd /workspace/mimir
uv run mimir worklink run <issue-id> --home /mimir-home --dry-run
```

A dry run is a prompt/config validation step only. It does not create a claim,
worktree, evidence bundle, branch, or PR.

### Real run

```bash
cd /workspace/mimir
uv run mimir worklink run <issue-id> \
  --home /mimir-home \
  --test-command 'env -u MIMIR_MODEL_SPEC uv run pytest -q <focused-tests> --tb=short'
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
- `transcript`: path to the backend JSON transcript outside the worktree.
- `pr_url`: review PR if the evidence gate passed.

Do not treat a backend's prose as evidence when these fields disagree. Empty
diff with a nominal backend success is demoted to failure.

### Recovery and cleanup

Current slice-1 recovery is manual:

- **Backend/sandbox failure before useful diff:** read the transcript path in
  the evidence bundle or `<home>/state/worklink/transcripts/`, adjust
  `<home>/worklink.yaml`, and rerun only if the Chainlink comments/attempt
  state indicate the next attempt will use a fresh attempt number.
- **Claim lock left behind:** inspect with `cd /mimir-home && chainlink locks
  check <issue-id>` and release the executor's own known-stale lock with
  `chainlink locks release <issue-id>`. Use `locks steal` only after independent
  TTL/heartbeat evidence; Chainlink can report a fresh lock as stale when no
  heartbeat has been written yet.
- **Retained failed worktree/branch:** failed or blocked attempts are retained
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

```yaml
defaults:
  backend: codex
  compute_backend: local_subprocess # WHERE the backend runs: local_subprocess | docker-sibling | ecs-runtask
  timeout_s: 1800
  priority: normal          # arbiter priority for autonomous dispatch
  test_command: "env -u MIMIR_MODEL_SPEC uv run pytest -q"

routes:                     # first match wins
  - label: "render"
    backend: mermaid        # tool_category: renderer
  - repo: "jasoncarreira/mimir"
    backend: codex

backends:                   # ToolBackend adapters — WHAT builds
  codex:
    bin: codex
    args: ["exec", "--json"]
  claude_cli:
    bin: claude

# Backend blocked path: coding CLIs can deliberately route planner/human issues
# back to Chainlink by printing `WORKLINK_BLOCKED: <one-line reason>`. This is
# for design contradictions or missing prerequisites, not transient tool errors.

compute_backends:           # ComputeBackend launchers — WHERE it runs (#454)
  local-subprocess: {}      # today's behavior; accept-the-risk fallback
  docker-sibling:
    broker_url: "unix:///run/worklink-broker.sock"   # broker owns the docker socket, not the agent
    image: mimirbot-mimirbot
    policy: {}             # optional broker policy map; actual enforcement arrives with broker slices
  # ecs-runtask is a future #459 substrate; uncomment only once that launcher exists.
  # ecs-runtask:
  #   cluster: worklink
  #   task_definition: worklink-worker
  #   subnets: ["subnet-…"]

`compute_backend` also accepts the legacy spelling `compute`; backend names are normalized so `docker-sibling` in YAML selects Python registry key `docker_sibling`. Invalid or unknown DockerSibling fields fail closed during config load instead of falling back to local execution. The agent-side broker client is implemented, but the broker process/policy layer is a later slice; until that broker is running, selecting `docker-sibling` will fail at launch rather than silently falling back to local execution.

tool_pins:
  - name: codex                  # required: stable local tool name
    category: coding-cli         # required: coding-cli | renderer | tracker | helper
    pin: "0.99.0"                # required: version, tag, or SHA currently expected
    smoke: "codex --version"     # required: command used as bump evidence
    source: npm                  # optional lookup strategy for drift checks
    package: "@openai/codex"     # optional upstream package/repo identifier
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
   <issue>`: validate-leaf → claim → worktree → codex adapter →
   observed evidence → transitions → PR. Dry-run on one non-critical
   issue (an adversarial-review LOW is a good guinea pig).
3. **Slice 2 — planner.** `prompts/decompose.md` +
   `chainlink-orchestrator` skill + executor template-refusal test.
4. **Slice 3 — autonomy.** Ready-queue poller, multi-claim concurrency,
   TTL reaper, arbiter gating, `worklink_run` tool. Gated on an isolation
   posture (see the trust model) — the `ComputeBackend` axis (#454) and/or the
   verified in-container seccomp profile (#452). Autonomous `local-subprocess`
   requires the explicit accept-the-risk opt-in.
5. **Slice 4 — second adapter.** `claude_cli`, proving mechanical
   addition.
6. **Slice 5 — tool pins.** Inventory + drift poller + bump-issue
   filing.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Chainlink locks not atomic cross-process | Slice 0 probe; O_EXCL fallback; upstream issue |
| Backend session brittleness | Start operator-invoked (slice 1); autonomy only after dry-run |
| Unsandboxed backend filesystem access | Verified in-container seccomp/userns profile confines bwrap `workspace-write` (#452, `docs/internal/seccomp-userns.json`), or out-of-container `ComputeBackend` isolation (#454); `local-subprocess` (today's `--sandbox danger-full-access`) is the explicit accept-the-risk fallback, gated before slice-3 autonomous dispatch |
| Evidence gaming by the backend agent | Orchestrator observes diff/tests itself; empty-diff demotion |
| Worktree cost under concurrency | Worktrees are per-issue and short-lived; cap concurrent claims (default 2) |
| Shared quota exhaustion | `quota_pool` + arbiter gating; operator runs bypass |
| Stale claims wedging issues | TTL reaper + `locks steal` + attempts cap |
