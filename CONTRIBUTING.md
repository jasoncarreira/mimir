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

## Reviewing

PRs need one approving review before merge. The bar is "does this make the
system better and not regress anything else." Drive-by suggestions are fine
in comments; blocking changes should map to the contribution-scope guidance
above.

## Security

For vulnerabilities, see [SECURITY.md](./SECURITY.md). Do not open public
issues for security concerns.
