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
- If you cannot complete this issue as specified — contradictory or impossible acceptance criteria, a missing prerequisite, or a design that is wrong — do NOT guess or fabricate success. Write the reason to a file named `.worklink-blocked` in the repository root and stop. Worklink reads that file, routes the issue to `worklink:blocked` for human review, and does not spend a retry attempt on it. An explanation left only in your final output is not enough; the blocked signal must be the `.worklink-blocked` file.
- Do not close the Chainlink issue yourself; Worklink will transition labels after observing evidence.
