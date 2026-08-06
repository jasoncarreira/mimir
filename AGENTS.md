# Working in this repository as an agent

Read [CONTRIBUTING.md](./CONTRIBUTING.md) first — scope, code style and the test
conventions live there and apply to you unchanged. This file covers only what an
automated contributor needs and a human reading CONTRIBUTING.md would not.

Everything below is recorded because it has already cost a run, a false diagnosis,
or a bad merge.

## Evidence

**Run both suites, and require both green.** A scoped run is not evidence:

```bash
uv run pytest -q tests/test_<file>.py   # the file you touched
uv run pytest -q                        # the whole suite
```

A test that passes alone and fails in the full suite is **not flaky** — it is
asserting ambient state it does not own. See *Don't assert on ambient state you
don't own* in CONTRIBUTING.md, which distinguishes that from the legitimate case of
asserting on state the component owns, and covers why a before/after delta is not
sufficient on its own. This has now happened twice: once against `sys.modules`,
once against `asyncio.all_tasks()`.

**Don't trust a venv you didn't sync from this branch's lock.** Each checkout's
`.venv` resolves from *its own* `uv.lock`, so pointing another tree's interpreter
at this one produces failures that are not the code's fault. A real example: four
`tests/test_runtime.py` failures that were entirely `deepagents` 0.6.1 against a
branch requiring `>=0.7.1`. Before attributing any failure to the change under
review, re-run it on the unmodified base and compare the dependency pin.

**Never `uv run` or `uv sync` against a live deployment checkout.** In a sandbox
or worktree it is correct and expected. Against a running deployment it recreates
the venv underneath the live process. Use `.venv/bin/python` directly there.

## Reading outside the diff

**Stay inside the repository.** An unscoped search rooted at `$HOME` wedged a run
for 65 minutes on a blocked write, with every health signal reading normal — the
run looked alive the whole time.

**A missing API means an undeclared dependency, not a missing file.** If what you
need isn't in the tree, declare the dependency, sync it, and read it in `.venv`.

**Resolve a dependency's source from an import, not a search.** Distribution names
and import names differ often enough to mislead (`agent-client-protocol` → `acp`,
`PyYAML` → `yaml`). Find a module that already imports it and follow that name to
`.venv/lib/python3.*/site-packages/<import name>/`. Searching by distribution name
can also collide with first-party code — `mimir/acp/` is not the `acp` SDK — and a
search that returns our own package reads as "the dependency is missing."

## Branches and merging

- `main` is protected: one approving review, `enforce_admins` on, required status
  checks, and **`dismiss_stale_reviews` enabled** as of 2026-08-05.
- `feature/acp` is the integration branch for the ACP feature (issues #1389–#1392,
  parent #1073). It has **no** protection.

**Verify an approval belongs to the current head.** `reviewDecision` reports the
aggregate, not whether anyone reviewed the commit you are about to merge:

```bash
gh api repos/<owner>/<repo>/pulls/<n>/reviews \
  --jq '.[] | "\(.user.login) \(.state) commit=\(.commit_id[0:9])"'
```

Compare against `headRefOid`. `dismiss_stale_reviews` handles this for approvals on
`main`, but it dismisses **approvals only** — a `CHANGES_REQUESTED` still carries
forward onto a head it never saw, and branches without protection get neither.

**Do not open a PR whose integrated suite is red**, even when every scoped command
passed. If the full suite fails, that is the finding; publish nothing until it is
green or the failure is understood and stated in the PR body.

## Proving a negative

To assert that something did *not* happen — a module was not imported, no task was
created, no adapter was started — scope the claim to the component under test,
prove it from the source, or run it in a child process. Never compare whole-process
or whole-event-loop state. CONTRIBUTING.md has the detail and the ordering.
