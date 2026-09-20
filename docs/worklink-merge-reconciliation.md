# Worklink merge reconciliation

Worklink's existing reaper sweep reconciles completed leaf pull requests when
`MIMIR_WORKLINK_REAPER_CRON` is enabled. It does not depend on GitHub webhook
delivery: every sweep reads current Chainlink and GitHub state, including
durable work left by an earlier interrupted sweep.

## Qualification

Automatic closure is intentionally narrower than GitHub's closing syntax. The
first nonblank PR-body line must be exactly `Closes chainlink #N.` for the
Chainlink being considered, and `chainlink` may occur only once in the body.
Negation, partial-work, follow-up, stack/epic language, another GitHub closing
clause, controls, quoted declarations, and malformed or conflicting
associations are refused. Titles, branch names, PR numbers, ordinary comments,
and evidence status can discover a candidate but never authorize closure.

The home must declare `repositories.yaml`, select that repository from
`worklink.yaml`, grant it `rw` mode, and set `WORKLINK_REPO` (or its supported
alias) to the exact configured Git root. The local Git top-level and origin,
the PR URL and API identity, and the PR base repository must all match that
inventory. The completing base is the inventory's `base_branch`; target branch
overrides and intermediate stack bases are deliberately left for a human even
when otherwise legitimate.

Only an open `worklink:review` ordinary leaf qualifies. `worklink:epic` and
competing lifecycle labels do not. A parent ID alone does not make an issue an
epic. Closed-unmerged PR evidence retains the existing archive behavior only
when the association is unique and its bytes are unchanged.

## Durable action and recovery

The sweep serializes local closers with
`state/worklink/merge-closure.lock`. Its intent and refusal ledger is the
`merge_reconciliations` namespace in
`state/pollers/worklink-ready-queue/dispatch_failures.json`. Every external
mutation has a durable start record first.

For a qualifying merge, Worklink posts one deterministic audit marker, reads a
fresh strict issue snapshot that contains that exact marker, then starts and
submits the close. It verifies the issue is closed before removing
`worklink:review`, and verifies label absence before finalizing. The audit names
the PR URL, merge timestamp, merge commit, configured completion base, and
stable intent key. A finalized tombstone makes repeated sweeps harmless and
prevents an operator-reopened issue from being closed again.

An absent marker after audit submission is uncertain, not proof that posting
failed, so Worklink never reposts automatically. Likewise, an open or
unreadable issue after close submission is never automatically closed again;
the issue may have been reopened by an operator. Positive closed state plus the
audit marker can complete recovery. Cleanup retries only while the issue is
still closed. `--dry-run` takes no lock and writes no tracker, ledger, receipt,
or archive state.

The lock covers cooperating local closers, not GitHub, Chainlink, or an
independent evidence writer. The archive path rechecks the evidence digest and
fsyncs the directory, but it is not an external compare-and-swap guarantee.

## Human reconciliation

Every refusal and uncertain outcome has a deterministic durable notice. When
the optional `chainlink-orchestrator` poller is installed and enabled, it
delivers these notices before reading Worklink configuration or tracker queues,
so a tracker outage does not hide the refusal. Delivery explicitly asks for
human inspection and forbids automatic close, audit repost, or intent reset.
Receipts are retained until the ledger durably acknowledges delivery, including
for a notice resolved before delivery. Notice and intent tombstones remain for
deduplication after receipts are pruned.

Without that poller, operators can inspect the ledger directly. A malformed or
unflushable ledger stops mutation and receipt pruning; repair durable storage
rather than deleting uncertain intent.
