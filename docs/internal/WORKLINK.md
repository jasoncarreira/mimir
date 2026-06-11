# WORKLINK — Chainlink worker orchestration (chainlink #380)

Mimir-native, toolchain-agnostic orchestration for durable work
decomposition and execution. Chainlink is the coordination surface and
source of truth; mimir plans; pluggable coding/maintenance CLIs build;
deterministic machinery connects them.

Status: SPEC (pre-implementation). Owner issue: chainlink #380; leaf
issues are subissues of #380.

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
worklink:in-progress ──blocked_reason──▶ worklink:blocked  (human unblocks → ready)
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
```

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
   issue/<issue>-a<attempt>` from fresh `main` — **attempt-scoped
   names**, because failed/blocked worktrees are retained for autopsy
   (§3 step 8): with issue-scoped names the second attempt would
   collide with the first attempt's kept worktree AND its branch, and
   `attempts < 3` could never actually run more than once. Each retry
   gets a fresh path + branch; autopsy artifacts from prior attempts
   stay untouched until the reaper prunes them. Never a long-lived
   per-worker branch.
4. **Render the prompt** from issue fields: description, acceptance
   criteria, review criteria, parent context, repo conventions pointer.
   Template lives at `prompts/worklink-order.md` (operator-tunable).
5. **Spawn the backend** (adapter `run(WorkOrder)`) under a timeout.
6. **Observe evidence** (`evidence.py`): the executor itself runs
   `git -C <worktree> diff --stat`/`status`, runs the declared test
   command capturing exit code, collects the transcript pointer.
7. **Validate + transition.** Evidence gates the label move (§4).
   Push branch, open PR (`gh` via the executor, or the backend's
   native PR creation when `Caps.native_pr_creation` and configured).
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

@dataclass(frozen=True)
class RawResult:
    exit_code: int
    transcript_path: Path | None
    backend_status: str       # backend-specific; adapter maps to common terms
    error: str | None

class ToolBackend(Protocol):
    name: str
    def capabilities(self) -> Caps: ...
    async def run(self, order: WorkOrder) -> RawResult: ...
```

Adapters own **only** CLI session mechanics: how to start/reuse a
session, pass prompt/rules, detect completion or failure, collect the
transcript, and map backend-specific errors into the common status
terms. Everything else — claiming, worktrees, evidence, transitions —
is orchestrator code shared by every backend. Adding a backend is one
file implementing the protocol plus a registry entry; it must never
require touching the orchestrator.

### Initial adapters

| Adapter | Invocation sketch | Notes |
|---|---|---|
| `codex` (first) | `codex exec --cd <worktree> --json <prompt>` | Already installed in the agent containers; JSON output; shares the ChatGPT-account quota pool with codex-routed agents |
| `claude_cli` (second) | `claude -p <prompt> --output-format json` in worktree cwd | Proves the interface is mechanical; separate Max-plan pool |
| `cursor` / others | per their headless CLIs | Added on demand |

Selection is config, not code (§7): per repo / label / issue-type, with
a per-category default. The executor consults `Caps` rather than
assuming (e.g. it opens the PR itself unless the backend both declares
and is configured for `native_pr_creation`).

### Backend trust model

The backend agent runs with full write access to its **worktree** and
whatever its own CLI permissions grant. The executor constrains the
blast radius: per-issue worktree (not the live checkout), explicit env
allowlist (no ambient secrets beyond what the backend needs — the
poller env-assembly discipline applies), branch push + PR (never push
to main), and review-gated close. Prompt content originates from
chainlink issues (operator/planner-authored), not arbitrary external
text; the planner must not paste untrusted web/PR content verbatim into
acceptance criteria.

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
  carries `tool_pins:` — every pinned external executable (codex,
  chainlink, mermaid-cli, helper CLIs) with category, pin
  (version/SHA), and a smoke command. A low-priority maintenance
  poller inventories pins vs upstream and **files chainlink bump
  issues** with changelog/risk notes; those flow through this same
  pipeline (planner refines → executor bumps in a worktree → the smoke
  command is the evidence). Version-drift checks are a tool-class
  concern, not a coding-CLI special case.

## 7. Configuration (`<home>/worklink.yaml`)

```yaml
defaults:
  backend: codex
  timeout_s: 1800
  priority: normal          # arbiter priority for autonomous dispatch
  test_command: "env -u MIMIR_MODEL_SPEC uv run pytest -q"

routes:                     # first match wins
  - label: "render"
    backend: mermaid        # tool_category: renderer
  - repo: "jasoncarreira/mimir"
    backend: codex

backends:
  codex:
    bin: codex
    args: ["exec", "--json"]
  claude_cli:
    bin: claude

tool_pins:
  - name: codex
    category: coding-cli
    pin: "0.99.0"
    smoke: "codex --version"
  - name: chainlink
    category: tracker
    pin: "git+dollspace-gay/chainlink@<sha>"
    smoke: "chainlink --help"
  - name: mermaid-cli
    category: renderer
    pin: "11.x"
    smoke: "mmdc --version"
```

## 8. Rollout slices (= the #380 subissues)

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
   TTL reaper, arbiter gating, `worklink_run` tool.
5. **Slice 4 — second adapter.** `claude_cli`, proving mechanical
   addition.
6. **Slice 5 — tool pins.** Inventory + drift poller + bump-issue
   filing.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Chainlink locks not atomic cross-process | Slice 0 probe; O_EXCL fallback; upstream issue |
| Backend session brittleness | Start operator-invoked (slice 1); autonomy only after dry-run |
| Evidence gaming by the backend agent | Orchestrator observes diff/tests itself; empty-diff demotion |
| Worktree cost under concurrency | Worktrees are per-issue and short-lived; cap concurrent claims (default 2) |
| Shared quota exhaustion | `quota_pool` + arbiter gating; operator runs bypass |
| Stale claims wedging issues | TTL reaper + `locks steal` + attempts cap |
