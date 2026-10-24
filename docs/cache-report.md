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

`read`, `write` and `uncached` are respectively the turn's
`usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens` and
`usage.input_tokens` (the Anthropic-shaped counters the token-usage history
rolls up). Shares divide each count by their sum, not by output tokens. Turns
without all three counters still count as turns, but are excluded from token
shares and shown as unmeasured; `n/a` means no measured input tokens. Rows break
out UTC day, recorded `trigger`, and session kind inferred from `session_id`
(`poller:`, `scheduler:`, or other channel IDs). The displayed agent model comes
from the home's current `.env` `MIMIR_MODEL_SPEC` when present; process-level
overrides may differ. The log has no per-turn model attribution, so this label
cannot establish which model served earlier turns.
