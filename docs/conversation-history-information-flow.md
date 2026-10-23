# Conversation-history offload provenance

DeepAgents' `SummarizationMiddleware._offload_to_backend` and
`_aoffload_to_backend` append evicted messages (excluding previous summaries)
to `/conversation_history/{session_id}.md`. Mimir wraps these methods in
`_deepagents_summarization.py`. `config.DEFAULT_FOLDERS` permits middleware
writes; that permission does **not** establish content trust.

## File-tool reads: same turn and later turns

Before and after #1994, `_filesystem_read_trust_anchor` excludes
`conversation_history` from `_SELF_AUTHORED_FILE_ROOTS`. Its HOME path label is
**untrusted active_ingest**, even for trusted-only conversations. There is no
trust raise or ad-hoc anchor override. A clean original source remains trusted
in the attached provenance, but cannot declassify the filesystem path. Admins
can read it; the non-admin protected-read boundary normally refuses this HOME
subtree before returning content.

`read_file`/`aread`, `grep`/`agrep`, and `glob`/`aglob` publish resolved paths
through `readonly_backend._publish_read_paths`. The backend attaches the HOME
label plus any matching original-source record. BudgetGate merges the capture
via `classify_protected_result` into live IFC state. Reads in the offloading
turn and subsequent turns use the same mechanism. `ls`/`als` names and the
directory itself remain untrusted HOME. Unlabelled native-file results use the
incomplete-provenance fallback; that fallback does not assert trust.

The additional source attachment preserves original ACLs and sensitivity.
Offload snapshots use `ifc_state.current`, not ingress-only `auth.ifc_labels`:
web, MCP and poller inputs observed mid-turn are retained. Existing bytes add
their earlier sources; an unknown prefix or a disagreement between the
anticipated and returned history paths adds an untrusted filesystem source.
The proxy hashes the content supplied to a successful write, or the observed
download plus exact edit, rather than later disk contents. A subsequent writer
therefore makes the record stale. Downloaded prefixes differing from the
pre-offload snapshot are unknown, not attested by the old record. Async
snapshotting, hashing and atomic record persistence run in worker threads.

Records live in `.mimir/conversation_history_labels/`, outside file-tool writable
roots. Version 1, nonempty complete sources, an exact digest, a direct parent
under HOME/conversation_history, and a non-symlink path are required. Missing,
stale, malformed, unsupported or incomplete records add no original sources;
the untrusted HOME label still applies. Owner attestations are never recreated.
This is server-owned **relative to file tools**, not a cryptographic signature:
a clean turn's authorized `shell_exec` can delete or forge records. Deletion
fails closed. Forgery requires that clean-turn model's shell authority; the
record never raises path trust even then.

## Shell read-back: known gap, follow-up required

`classify_protected_result("shell_exec", {"command": "cat .../conversation_history/x.md"}, ...)`
currently yields an untrusted **informational** shell source, not active ingest.
The same applies to shell `rg`/`grep`: these routes do not use the native backend
path publisher or restore the saved original labels. Thus a later clean turn
can ingest untrusted history through shell without activating the write veto.
This predates #1994 and is not fixed by this file-tool provenance change. It
requires a separate shell-read classification follow-up (requested in PR #2420),
including direct, recursive and indirect shell reads without relaxing existing
authorization. Do not claim every read-back route is protected until that lands.

## Recall

`file_search` queries the memory/state index (`tools/extra.py`), not this
directory. `memory_query`/`memory_get` and auto-recall query SAGA atoms
(`tools/memory.py`) and publish their separate per-atom IFC metadata. None reads
these offload files. Explicit file-tool read-back activates
`_TAINTED_TURN_WRITE_TOOLS` and file-write vetoes in either enforcement mode;
the shell exception above remains outstanding.
