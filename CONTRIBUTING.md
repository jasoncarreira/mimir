# Contributing to mimir

Thanks for your interest. mimir is an early-stage open-source project; the
contribution flow is still maturing. Below is what works today.

## Quick path

1. Open a GitHub issue describing the bug or proposal before you start a
   substantial change. Small fixes (typos, obvious bugs, tightening a
   docstring) can go straight to a PR.
2. Fork, branch off `main`, push, open a PR. Reference the issue.
3. Run `uv run pytest` locally — CI is currently best-effort while the repo
   moves to public Actions minutes.
4. Sign your commits if you can; not required.

## What kind of change is in scope

In scope:

- Bug fixes
- Documentation improvements
- New skills (markdown-only, additive)
- New optional bridges (Discord/Slack-style adapters)
- New optional model providers
- Tests, especially regression tests for failures observed in the field

Less likely to be accepted without prior discussion:

- Changes to the memory backend's on-disk schema (touches saga)
- Changes to the agent loop's prompt-construction order (affects every
  deployment's prompt cache)
- Replacing a dependency with a different one for taste reasons

If you're unsure, open an issue first.

## Code style

- Python 3.11+
- Prefer existing patterns over introducing new abstractions
- `from __future__ import annotations` at the top of new modules
- Type hints on public functions; `from typing import Optional` style is fine
- Tests live under `tests/`; one file per module under test, matching name
- No emoji in code or commit messages unless an existing file already uses them

## Tests

```bash
uv sync
uv run pytest                                          # full suite
uv run pytest tests/test_specific_module.py            # one file
uv run pytest --ignore=tests/test_bench_via_mimir.py   # skip slow integration
```

If your change touches the agent loop, run the bench harness before opening
the PR — see `benchmarks/longmemeval_via_mimir/README.md`. Memory-backend
changes (`mimir/saga/`) are covered by `tests/test_saga_*` in the main
test suite — no separate `cd` is needed.

### Run both, and require both green

A scoped run is not evidence. Before submitting a test, run it alone **and** in
the full suite:

```bash
uv run pytest -q tests/test_<file>.py   # your file alone
uv run pytest -q                        # the whole suite
```

**A test that passes alone and fails in the full suite is not flaky. It is
asserting shared state.** Fix the assertion, not the ordering. Do not reach for
ordering plugins, `importlib.reload`, or fixtures that reset globals — those hide
the coupling instead of removing it.

### Don't assert on ambient state you don't own

Asserting on mutable process state is fine when that state is the *subject* of the
test and the test owns it — verifying that a component sets an environment variable
or handles the working directory correctly is a legitimate test, provided the change
is scoped and restored (`monkeypatch`) or isolated in a child process.

The problem is asserting on **ambient** state: shared state outside the ownership of
your test or the component under test, which any other test in the process may also
touch. Two kinds have bitten this repo:

- **Import-time state** — `sys.modules`, import caches, registries, singletons,
  module-level mutable defaults. Fails at collection, because importing a sibling
  test module is enough to populate it.
- **Live runtime state** — the event loop's task set (`asyncio.all_tasks()`), open
  transports and connections, threads, timers. Fails during an `await`, when a task
  another test leaked finishes or spawns inside your measurement window — so it can
  fail even against a clean baseline.

The distinction is ownership, not mutability. `monkeypatch.setenv` on a variable your
component reads is owned and restored. A bare read of the whole event loop's task set
is ambient, because everything that ran before you shares it.

To prove a negative — "this module does not import X", "nothing reaches Y", "no
adapter was started", "no task was created" — pick one:

1. **Scope the claim to the thing under test.** Assert no *new* task belonging to
   your component, not equality over every task in the loop. This is usually the
   right answer and the cheapest.
2. **Prove it statically.** Walk the AST for imports, or read the module's own
   declarations. Works when the property is visible in the source.
3. **Prove it in a child process.** A fresh interpreter running only the code
   under test. Correct but slow; reserve it for properties the other two cannot
   express.

A before/after delta is **not** sufficient on its own. Capturing state, acting,
and re-comparing still fails when the state moves concurrently — that is exactly
how the second kind of failure gets past review, because the test reads as
careful.

## Reviewing

PRs need one approving review before merge. The bar is "does this make the
system better and not regress anything else." Drive-by suggestions are fine
in comments; blocking changes should map to the contribution-scope guidance
above.

## Security

For vulnerabilities, see [SECURITY.md](./SECURITY.md). Do not open public
issues for security concerns.
