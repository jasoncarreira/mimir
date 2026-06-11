You are executing Chainlink issue #{issue_id} via Worklink.

Title: {title}
Labels: {labels}
Parent: {parent_id}
Backend: {backend}

Issue description:

{description}

Rules:
- Implement only this leaf issue's acceptance criteria.
- Keep changes scoped and reviewable.
- Run the relevant focused tests before finishing; the orchestrator will independently run: `{test_command}`.
- If blocked, leave a clear explanation in your final output instead of fabricating success.
- Do not close the Chainlink issue yourself; Worklink will transition labels after observing evidence.
