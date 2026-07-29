# Repository/PR parity canary

Issue #1049 is an entirely offline gate. Its checked-in result is
`evidence/repo-pr-parity.json`; it is generated from synthesized inputs by
`mimir.repo_pr_parity.offline_canary_probes`. The shadow evaluator has no Git
runner, Forge client, shell command, or execution callback. It records planned
effects only, while effect receipts and audits belong to the recorded primary
path.

The live review/remediation cycle is explicitly out of scope for #1049. It is a
reviewer-executed gate on #1050 and must not be used to widen a Worklink executor
allow-list.

## #1050 reviewer procedure

1. Select one ordinary review request and one changes-requested PR authored by
   the configured bot in the canonical repository. Record each server-issued
   `scope_id` and observed head SHA before acting.
2. Use only the typed PR/repository tools. Submit at most one review in the
   review scope. In the remediation scope, create at most one comment, push,
   and review re-request after the bounded fix and tests pass.
3. Capture provider receipts and durable audit record IDs for every write.
   Verify every receipt and audit pair names the same `scope_id` and head SHA.
4. Verify the shadow audit reports planned effects but zero observed shadow
   effects. A second review, comment, push, or re-request is a failed canary.
5. Advance a test head or use a recorded stale snapshot and verify the typed
   push refuses before a push receipt exists. Do not mutate the selected live
   PR merely to manufacture staleness.
6. Record operation/scenario totals and each mismatch category. Any unaccepted
   category blocks cutover; acceptance must identify the operator and category.

Raw `gh`, `chainlink`, and direct network access are not part of the #1049
procedure or its evidence generation.
