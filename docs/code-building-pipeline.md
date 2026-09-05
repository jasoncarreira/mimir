# Code-building pipeline

This guide takes an operator from an existing GitHub checkout to a Worklink
leaf that is built, tested, and opened as a pull request, then explains how the
separate GitHub poller surfaces review work. Paths below are relative to the
agent home selected by `MIMIR_HOME`.

## 1. Declare the repository

Create `<MIMIR_HOME>/repositories.yaml`:

```yaml
repositories:
  - slug: owner/service
    root: /var/lib/mimir-worklink/base/service
    mode: rw
    origin: https://github.com/owner/service.git
    base_branch: main
    test_command: uv run pytest -q
```

All six repository fields are significant:

| Key | Meaning |
|---|---|
| `slug` | Lower-cased `owner/repository` identity. It must name the same GitHub repository as `origin`. |
| `root` | Absolute path to the dedicated Worklink base clone. It must be the checkout's top level and must not be the running controller source. |
| `mode` | `rw` includes the root in the inventory's writable-root projection; `ro` excludes it. Use `rw` for a code-building target. |
| `origin` | Exact `remote.origin.url` expected in the checkout; HTTPS and GitHub SSH URL forms are accepted. |
| `base_branch` | Branch from which Worklink creates attempt checkouts and against which it opens leaf PRs. |
| `test_command` | Optional repository-specific evidence gate. When present it wins over `worklink.yaml`'s `defaults.test_command`. It is split into arguments and run without a shell. |

The root must already exist and its Git `origin` must equal the configured
value. Duplicate slugs or roots are rejected. See the
[repository inventory reference](configuration.md#repository-inventory) for
the startup checks and legacy-setting reconciliation.

## 2. Configure Worklink

Create `<MIMIR_HOME>/worklink.yaml`. This file selects execution behavior;
`repositories.yaml` declares which repository and filesystem root that behavior
may use.

```yaml
repository: owner/service
defaults:
  backend: opencode
  timeout_s: 1800
  test_command: uv run pytest -q
  max_concurrent: 2
  reaper_ttl_s: 86400
  allow_autonomous_local_subprocess: true
backends:
  opencode:
    bash_allowlist:
      - "git *"
      - "uv *"
```

`repository` must match a `repositories[].slug`. That match lets startup derive
`WORKLINK_REPO` from the inventory's `root`; it also rejects a configured
target that is not in the inventory. Repository-level `base_branch` and
`test_command` take precedence over `defaults.base_branch` and
`defaults.test_command` for that target.

Mimir never derives this path from its installation. Provision the clone before
startup, including for PyPI installs, and set `MIMIR_SOURCE_DIR` when the controller
runs from an editable checkout so Worklink can enforce non-overlap. A host with no
base fails closed rather than cloning into an inferred location.

`local_subprocess` is the shipping compute backend and has shared filesystem
access. Autonomous dispatch refuses it unless
`defaults.allow_autonomous_local_subprocess` is `true`; a manually invoked
`mimir worklink run` is not subject to that autonomy gate. The OpenCode
`bash_allowlist` must admit the configured test runner. The complete key
reference is [Worklink YAML](configuration.md#worklink-yaml).

Install the ready-queue skill into the same home:

```bash
mimir skills install chainlink-orchestrator --home "$MIMIR_HOME"
```

Restart `mimir run` after changing these files or installing the skill. A
running agent can instead activate a newly installed poller through its
`reload_pollers` tool. The ready queue runs every ten minutes by default.

### Factory epics

Factory epics use the same repository declaration and isolated-checkout allocator,
but have separate admission and concurrency. The image installs
`feature-factory@0.8.3` and `opencode-feature-factory@0.8.3` under
`/opt/mimir-opencode`; `MIMIR_FACTORY_ENTRYPOINT` names the absolute
`feature-factory/bin/factory.js`. Set `MIMIR_FACTORY_EPICS_ENABLED=1` to let the
poller dispatch `worklink:epic` issues. `MIMIR_FACTORY_MAX_CONCURRENT` defaults
to `1`, independently of the leaf default `2`.

The launch ends with `--command feature " --autonomous --max-retries 5
<issue>"`. `MIMIR_FACTORY_MAX_RETRIES` defaults to `5`, accepts exactly ASCII
`[0-9]+` in range `1..9007199254740991`, and falls back to `5` for absent or
invalid values. feature-factory 0.7.5 stages the workflow inside the run
directory; exact token `--auto` is never passed. Worklink's base selects the
checkout start point and PR target; it is not factory `--base`, which is never
passed.

Before first factory dispatch only, after checkout creation and before process
launch, Worklink reads the effective checkout `git config --get user.name` and
`git config --get user.email`. Both must be nonblank. The child receives
`GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and
`GIT_COMMITTER_EMAIL`; Worklink never writes sandbox Git identity configuration.

Worklink reads the nonblank `publishing_identity` from
`MIMIR_FACTORY_PUBLISHING_IDENTITY` when that variable is set, otherwise from
the trusted controller checkout's `.factory.json`. A set but blank or non-string
override fails instead of falling back. For GitHub publication Worklink
verifies the credential this process is already bound to, `GITHUB_TOKEN`, rather
than selecting among candidates: `GH_TOKEN` is a child-only alias for `gh`, and
verifying a second credential in a process that already verified one is refused
by the forge identity memo before `/user` is ever reached. That token's owner is
compared against the selected identity before dispatch, then both child aliases
are normalized to it. `GH_TOKEN` and `GITHUB_TOKEN` set to different values fail
dispatch as an operator ambiguity rather than one being preferred, and a missing
`GITHUB_TOKEN` fails naming that variable - in both cases without disclosing
values.

Worklink supervises OpenCode while `/feature` owns factory transitions. The
12-hour `MIMIR_FACTORY_RUN_TIMEOUT_S` default is only a process liveness
backstop. The 900-second `MIMIR_FACTORY_STALE_HEARTBEAT_S` threshold produces
diagnostics and never authorizes duplicate dispatch or takeover. A
`needs-human` result remains parked in the retained sandbox. Resume performs
status, justified run-ID-first lock acquisition, status, explicit resume,
authoritative status, reconciliation, and exact OpenCode relaunch.

A factory `completed` status does not by itself move the issue to review.
Worklink independently runs the configured repository test command, requires a
clean stable checkout HEAD, and verifies the canonical GitHub PR is open,
non-draft, in the declared repository, based on the expected base, and points
the expected head branch at that same tested SHA. Any mismatch retains the
sandbox and fails closed. `mimir worklink stop` cancels the verified OpenCode
process group; there is no factory cancel command.

## 3. File a strict leaf

The executor checks a new non-epic leaf before acquiring a claim. Its
description must contain every line in this validator marker contract, with the
shown capitalization optional but the text, colons, and dash prefixes present:

```text
Acceptance criteria:
Review criteria:
Worklink notes:
- Scope:
- Out of scope:
- Suggested test command:
```

It must also contain at least one checklist item beginning at column 0 in the
form `- [ ] ` (checked forms `- [x] ` and `- [X] ` also satisfy the validator).
A complete starting description is:

```markdown
Acceptance criteria:
- [ ] <observable, testable outcome>
- [ ] <focused validation command or evidence requirement>

Review criteria:
- <what a reviewer/operator should verify before approval>

Worklink notes:
- Scope: <files/subsystems expected to change, or "docs only">
- Out of scope: <nearby work not included in this leaf>
- Suggested test command: <advisory validation command for the backend to consider>
```

The suggested command is context, not the evidence gate: Worklink runs the
operator-configured repository or deployment test command. Save the description
to a file and create the issue without adding the dispatch label yet:

```bash
chainlink issue create "<leaf title>" \
  --description "$(cat /tmp/worklink-leaf.md)" \
  --priority medium
```

For a child of an existing issue, the planner uses
`chainlink issue subissue <parent-id> --description "$(cat /tmp/worklink-leaf.md)" "<leaf title>"`.
Add dependency edges with
`chainlink issue block <ID-that-is-blocked> <BLOCKER>`; the blocked issue is the
first argument.

Validate the exact rendered work order without claiming or changing Git or
Chainlink state:

```bash
mimir worklink run <issue-id> --home "$MIMIR_HOME" \
  --repo /workspace/service --dry-run
```

### Invalid leaves are silently removed from the queue

On a non-dry autonomous or manual run, a new leaf missing any marker or the
column-0 checklist is rejected before dispatch. Worklink best-effort removes
`worklink:ready`, adds `worklink:blocked`, and posts a `WORKLINK_BLOCKED leaf
template validation failed before dispatch` comment listing the missing parts.
No claim attempt is consumed.

If a labelled leaf never starts, inspect it and the queue:

```bash
chainlink issue show <issue-id>
chainlink issue ready
```

Look for `worklink:blocked` and the `WORKLINK_BLOCKED` comment. Correct the
description so it contains the exact contract above, then remove
`worklink:blocked` and re-add `worklink:ready`:

```bash
chainlink issue unlabel <issue-id> worklink:blocked
chainlink issue label <issue-id> worklink:ready
```

Do not merely re-add
`worklink:ready` to an invalid description; it will be demoted again.

## 4. Dispatch and claim lifecycle

Apply `worklink:ready` only when the leaf is intended to run:

```bash
chainlink issue label <issue-id> worklink:ready
```

Two independent conditions make a leaf dispatchable:

1. It carries `worklink:ready`, which records operator/planner intent to build.
2. It appears in `chainlink issue ready`, which means it is open and has no open blockers.

An unblocked issue can therefore appear in `chainlink issue ready` and still
never be claimed because it lacks `worklink:ready`. Conversely, a pre-labelled
leaf with an open blocker remains untouched until the blocker closes.

On each poll, `worklink-ready-queue` intersects those two sets, counts active
Chainlink locks, and starts detached
`mimir worklink run <id> --home <home> --repo <root> --autonomous` processes up
to `defaults.max_concurrent` (default 2). The poller returns immediately; logs
go to `<MIMIR_HOME>/state/pollers/worklink-ready-queue/run-<id>.log`.
`WORKLINK_MAX_CONCURRENT` is only a legacy fallback when `worklink.yaml` does
not exist. Manual `mimir worklink run` calls are not concurrency-capped.

The detached executor, not the poller, owns the claim protocol:

1. It atomically reserves the issue with `chainlink locks claim`.
2. It removes `worklink:ready`, adds `worklink:in-progress`, and writes a structured `WORKLINK_CLAIM` comment.
3. While backend compute is active it appends a refreshed claim heartbeat every 60 seconds.
4. It creates an isolated attempt checkout, runs the backend, independently derives the diff and configured test evidence, and writes evidence under `<MIMIR_HOME>/state/worklink/evidence/`.
5. It releases the lock after the terminal label transition.

The stale-claim reaper is enabled by setting a schedule for
`MIMIR_WORKLINK_REAPER_CRON`. It uses `defaults.reaper_ttl_s` (default 86400)
against the latest claim/heartbeat timestamp, steals only stale locks, and
returns the leaf to `worklink:ready` unless its attempt budget is exhausted.
Existing deployments must set this value to at least twice the greater of
`defaults.timeout_s` and `MIMIR_FACTORY_RUN_TIMEOUT_S` (86400 with the shipped
12-hour factory timeout). During migration, lower configured values are raised
to that floor with a warning rather than disabling Worklink at startup.

The leaf claim budget is three charged attempts in the current runtime. The
parsed `defaults.max_claim_attempts` key is compatibility-only and does not
change that budget. At exhaustion the leaf receives `worklink:blocked`. After
fixing an infrastructure failure that unfairly consumed attempts, an operator
may post:

```bash
chainlink issue comment <issue-id> 'WORKLINK_CLAIM_RESET {"reason": "<why prior attempts should be forgiven>"}'
```

A valid reset starts a fresh three-attempt budget generation. At most two reset
markers per issue are honored; later markers are inert, preventing an infinite
retry loop. Remove `worklink:blocked` and add `worklink:ready` only after the
underlying failure is fixed.

## 5. What arrives for review

When the backend exits successfully, Worklink does not trust its success claim
alone. The controller observes the checkout diff, reruns the configured test
gate, commits the changes, verifies a clean checkout, pushes the attempt branch,
and runs `gh pr create` against the configured base branch. It then:

- stores the final evidence JSON under `<MIMIR_HOME>/state/worklink/evidence/`;
- comments the evidence and PR URL on the Chainlink issue;
- replaces `worklink:in-progress` with `worklink:review`;
- leaves the GitHub pull request open for review. It does not merge it.

`GITHUB_TOKEN` or `GH_TOKEN` must reach the ready-queue poller so the detached
controller can create the PR. Both names are in that poller's `pass_env` list.
A failed gate is returned to `worklink:ready` while attempts remain; an explicit
backend block or exhausted budget becomes `worklink:blocked`.

## 6. GitHub activity and review flow

The GitHub poller is a separate opt-in skill. Install it into the same home and
restart Mimir (or use `reload_pollers`):

```bash
mimir skills install github-poller --home "$MIMIR_HOME"
```

It runs every 15 minutes and watches each repository in `GITHUB_REPOS`. When
`repositories.yaml` is declared, startup derives that value from
`repositories[].slug`; an explicitly configured legacy value must agree. The
poller requires GitHub authentication (`GITHUB_TOKEN`, falling back to
`gh auth token`) and should be given `MIMIR_GITHUB_SELF_LOGIN` when Mimir has a
dedicated GitHub identity, so its own comments do not wake it. For repository
remediation tools and the configured `repo_test` capability, enable the coding
surface described in [configuration.md](configuration.md).

For each watched repository it detects new issues, new PRs, issue/PR comments,
inline review comments, submitted reviews, pushes to open PRs, review requests,
stale unresolved changes-requested reviews on Mimir's PRs, and mergeability
state on Mimir's PRs, and completed check failures on open PR heads. A failed
check on a PR authored by `MIMIR_GITHUB_SELF_LOGIN` creates a remediation turn
bound to the observed repository, PR, and immutable head; external contributors'
failures are notification-only. Main-branch workflow failures remain the
separate `github-ci-watch` responsibility. The poller deliberately does not poll
general issue/PR close, reopen, merge, and label changes.

The poller emits an agent turn for actionable activity; it does not
deterministically approve or merge. A new PR, PR push, or review request carries
a rule requiring the turn to submit its review through the typed review tool or
`gh pr review`, rather than only writing review prose in chat. Turn finalization
emits `poller_review_missed_submission` if the expected submission call is
absent. While `MIMIR_GITHUB_SELF_LOGIN` remains in GitHub's requested reviewers,
the poller reconciles and retries a failed/missing review turn up to three times,
then emits a `pr_review_request_gave_up` signal for operator attention. A review
already submitted at the current head satisfies a later duplicate request.

For Worklink-created PRs, the human review path is always available through the
PR URL in the Chainlink evidence comment. The GitHub poller additionally opens
an agent review turn only when the resulting GitHub event is in a watched
repository and survives its self-identity and trust filters. Its cursor is
stored at `<MIMIR_HOME>/state/pollers/github-activity/cursor.json`; inspect
`<MIMIR_HOME>/logs/events.jsonl` for `poller_complete`, `poller_stderr`, or
review give-up signals when expected activity is missing.

CI remediation requires `MIMIR_GITHUB_SELF_LOGIN` to identify owned PRs and the
same writable repository binding and coding surface used by other remediation
events. Its scope keeps `pr_comment` so an unsuccessful repair can leave an
operator-visible trace, while excluding `pr_edit` and `pr_rerequest`. Delivery
receipts are stored beside the cursor under `.delivery-receipts/`; enqueue
rejection leaves no receipt, so the failure is retried rather than silently
consumed. Before checkout, Mimir re-fetches the PR and current checks and
terminates closed, superseded-head, or already-green work.

The first observation after rollout deliberately baselines failures that already
predate its collection window: an untouched red head stays quiet until its head
or failure set changes. Open-PR discovery is also intentionally bounded to the
100 most recently updated PRs. Newly completed checks normally refresh the
relevant PR into that window; this is a bounded approximation, not complete
enumeration of repositories with more than 100 open pull requests.
