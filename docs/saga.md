# Saga Maintenance

## Recall Health

Atom and session vector indexes use the configured live embedder's dimension,
not the most common stored dimension. Legacy dimensions no longer block recall:
matching vectors remain searchable, while incompatible vectors are counted and
warning-logged once per index kind per process. FTS and session recency pathways
remain available for excluded records.

The Saga dashboard and its authenticated stats API (`/api/saga?view=stats`)
show `embedding_health`: target dimension, per-dimension distributions, and
`excluded_from_recall` counts for live atoms and embedded sessions. Counts also
include malformed vector lengths and unknown dimensions. Provider unavailability
is reported as unknown, not zero exclusions. A cold local sentence-transformer
model is not loaded just for dashboard health. Database `ready` remains a
readability signal, distinct from embedding health. The ops view does not report
live Saga health; use the Saga view linked from it.

## Repair Stale Dimensions

`mimir saga-reembed` repairs existing embeddings whose recorded dimensions
differ from the configured provider's live `dimensions()` or are NULL. It covers the
`embeddings` sidecar for non-tombstoned atoms and `sessions.embedding` for
sessions with nonempty summaries. Atom content and session summaries are embedded
in passage mode. This is an offline maintenance command, not a startup repair.

1. Stop mimir and every other writer using this Saga database. Disable automatic
   restarts/watchdogs for the maintenance window. Back up the stopped database
   with SQLite's backup tooling (include committed WAL data).
2. Configure the intended provider and credentials in the agent home's
   `saga.toml` and `.env`, or the process environment.
3. Preview the live provider, model, dimensions, and stale counts:

   ```sh
   mimir saga-reembed --home /path/to/agent-home --dry-run
   ```

4. Check the reported provider before applying. Missing API keys can cause the
   normal `get_provider()` fallback to ONNX; both target dimensions and atom
   provenance come from that live instance, not the configured provider label.

   ```sh
   mimir saga-reembed --home /path/to/agent-home --batch-size 50 --batch-delay 1
   ```

5. After successful completion, restart mimir with the same configuration so its
   in-memory atom and session vector indexes are rebuilt. Re-enable watchdogs.

Home selection follows `--home`, then `MIMIR_HOME`, then the working directory.
The home's `.env` supplies defaults without overriding exported variables.
An explicit `SAGA_CONFIG` takes precedence over `<home>/saga.toml`. As in the
runtime, relative `[storage] db_path` values resolve under `<home>/.mimir/`;
the default is `<home>/.mimir/saga.db`. A missing database is an error, not created.
On a deployment checkout use the installed `mimir` executable, not `uv run` or
`uv sync`, which may replace the deployment's virtual environment.

Batches run sequentially with at most `--batch-size` rows in each provider call.
Inputs are clipped to `[embedding] max_input_chars` (default 2000).
`--batch-delay` sets finite, nonnegative seconds between provider calls, including
the transition from atoms to sessions (default 0). API providers retry transient
errors with exponential backoff, but do not pace successful requests. Set a delay
appropriate to your provider quota; provider-internal sub-batches still follow
the provider's own behavior. Dry-run neither embeds nor sleeps.
Progress is printed after every committed batch. All returned vector counts,
dimensions, finite values, and float32 serialization are validated before any
batch writes. An error or interrupt leaves earlier commits intact and the
unfinished batch uncommitted. Rerun the same command to resume; matching rows
are never rewritten, including their timestamps and provenance.

`--dry-run` opens SQLite read-only and does not call `batch_embed` or write any
rows. Provider initialization/dimension discovery may still load or download a
local model. Normal apply runs may contact the configured embedding service.

This command deliberately does not migrate between models with the **same
dimension**, repair absent embeddings, or change
triples, file-search indexes, ACLs, atom metadata, or session metadata. Sessions
without usable summaries and tombstoned atoms are left alone. Stale sessions with
NULL, empty, or ASCII-whitespace-only summaries are included in the stale total
and explicitly reported as unrepaired, in both preview and apply. Apply exits
with status 1 if any stale rows remain after repair; dry-run only reports them.
Previously committed repairs are preserved. Restore the original summary from
an authoritative source and rerun; the command cannot invent missing text.
NULL dimensions on existing embeddings are repairable when source text exists.
Session embeddings
have no embedding-provider/model columns; existing session `provenance` is ACL
metadata and is not repurposed. No schema changes or live index refreshes occur.
