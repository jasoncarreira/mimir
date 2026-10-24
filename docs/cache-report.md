# Prompt cache-hit report

From the repository checkout, run the read-only report against a deployment home
(do not copy deployment logs into the checkout):

```bash
uv run python scripts/cache_report.py /path/to/MIMIR_HOME --from-date 2026-05-23 --to-date 2026-05-29
```

Dates are inclusive UTC days; without flags the window is the last seven UTC
calendar days. The tool streams the retained `logs/turns.jsonl`; it does not
modify the home. Missing logs produce an error; malformed or undated lines are
skipped and counted. Retention can make older windows incomplete.

By default, `usage.input_tokens` is the **inclusive total prompt count**, as
recorded by `mimir/turn_logger.py::derive_result_fields` when aggregating
LangChain `usage_metadata`. `read` and `write` are its subsets
`usage.cache_read_input_tokens` and `usage.cache_creation_input_tokens`.
`uncached = input_tokens - read - write`; shares divide by the inclusive input
total, not output tokens or input plus cache tokens. A turn whose cache details
exceed its input total is flagged and excluded, never clamped or inferred to use
a different convention. For example, input=100, read=60, write=20 means
uncached=20 and shares 60% / 20% / 20%.

The logger also has a legacy Claude Code fallback: if no AI message has
`usage_metadata`, it copies the final `response_metadata["usage"]` verbatim.
That can preserve Anthropic-raw **exclusive** input counts. For a window known
to contain only that convention, pass `--input-semantics exclusive`: input is
then uncached, and the denominator is input + read + write. The log does not
record which branch produced usage, so the tool cannot reliably distinguish
conventions (even when cache details do not exceed input). Do not combine
mixed-convention windows into one report; select known homogeneous windows.
The current home model label is not evidence of historical usage semantics.

A zero or near-zero `write` share does not establish that the provider created
no cache entries: OpenAI-style providers report cache reads but no separate
cache-write counter. The report measures recorded counters, not cache billing
or cache creation absent from telemetry.

Turns without all three nonnegative integer counters still count as turns, but
are excluded from token shares and shown as unmeasured; `n/a` means no measured
input tokens. Rows break
out UTC day, recorded `trigger`, and session kind inferred from `session_id`
(`poller:`, `scheduler:`, or other channel IDs). The displayed agent model comes
from the home's current `.env` `MIMIR_MODEL_SPEC` when present; process-level
overrides may differ. The log has no per-turn model attribution, so this label
cannot establish which model served earlier turns.
