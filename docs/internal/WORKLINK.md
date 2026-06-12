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
   Template lives at `prompts/worklink-order.md` (operator-tunable). The
   planner-side decomposition prompt lives at `prompts/decompose.md`; its
   `{leaf_template}` slot is rendered from the single
   `mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN` contract, which the
   `chainlink-orchestrator` skill also embeds, so the executor validates the
   same template the planner emits.
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
polling or in-turn `worklink_run` dispatch for Codex, choose one of:

1. make a real sandbox profile work in-container, scoped to the worktree
   plus transcript/evidence roots;
2. run backends in a sibling/containerized worker with only the intended
   mounts; or
3. explicitly accept unsandboxed autonomous backend runs as an operator
   policy decision and document the added blast radius.


The planner-supplied `Suggested test command` is executable shell input
(`shell=True`) from Chainlink issue descriptions, within the same
operator/agent-private trust boundary as poller commands.

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
  carries `tool_pins:` — every pinned external executable (codex,
  chainlink, mermaid-cli, helper CLIs) with category, pin
  (version/SHA), and a smoke command. A low-priority maintenance
  poller inventories pins vs upstream and **files chainlink bump
  issues** with changelog/risk notes; those flow through this same
  pipeline (planner refines → executor bumps in a worktree → the smoke
  command is the evidence). Version-drift checks are a tool-class
  concern, not a coding-CLI special case.

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
  timeout_s: 1800
  priority: normal
  test_command: "env -u MIMIR_MODEL_SPEC uv run pytest -q"

backends:
  codex:
    bin: codex
    args: ["exec", "--json", "--sandbox", "danger-full-access"]
```

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
   TTL reaper, arbiter gating, `worklink_run` tool.
5. **Slice 4 — second adapter.** `claude_cli`, proving mechanical
   addition.
6. **Slice 5 — tool pins.** Inventory + drift poller + bump-issue
   filing.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Chainlink locks not atomic cross-process | Slice 0 probe; O_EXCL fallback; upstream issue |
| Backend session brittleness | Start operator-invoked (slice 1); autonomy only after dry-run |
| Unsandboxed backend filesystem access | Current Codex route uses `--sandbox danger-full-access`; treat worktree isolation as audit/review only, and require a fresh sandbox/container/policy decision before slice-3 autonomous dispatch |
| Evidence gaming by the backend agent | Orchestrator observes diff/tests itself; empty-diff demotion |
| Worktree cost under concurrency | Worktrees are per-issue and short-lived; cap concurrent claims (default 2) |
| Shared quota exhaustion | `quota_pool` + arbiter gating; operator runs bypass |
| Stale claims wedging issues | TTL reaper + `locks steal` + attempts cap |
