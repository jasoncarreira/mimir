# Conversation-history offload provenance

DeepAgents' `SummarizationMiddleware._offload_to_backend` and
`_aoffload_to_backend` append the evicted messages (excluding earlier summary
messages) to `/conversation_history/{session_id}.md`. Mimir wraps these methods
in `_deepagents_summarization.py`. `config.DEFAULT_FOLDERS` makes the directory
writable by the middleware; write permission does **not** establish content trust.

Before #1994, `readonly_backend._publish_read_paths` called
`access_control.protected_result_source` for each successful file read. The
`_filesystem_read_trust_anchor` HOME rule excludes `conversation_history` from
`_SELF_AUTHORED_FILE_ROOTS`, so the resulting filesystem source was **untrusted,
active_ingest** for both same-turn and later-turn reads, even for trusted-only
conversations. There was no per-file persistence of the original labels. An
admin could read the file; the non-admin protected-read boundary normally
refuses direct reads of this HOME subtree before any content is returned.

Now each successful offload snapshots the live turn's complete IFC source set
(a conservative superset of the messages evicted), plus the labels of the
existing bytes when appending. A content digest and source records are written
atomically in the server-owned `.mimir/conversation_history_labels/` directory,
outside the agent's file-tool writable roots. An existing offload without a
matching record contributes an untrusted filesystem source. Missing turn labels,
missing/stale/malformed records, and changed file bytes retain the untrusted
HOME default. Earlier untrusted input cannot be raised by a later clean append.
Owner attestations are not recreated from disk.

For `read_file`/`aread`, `grep`/`agrep`, and `glob`/`aglob`, the native backend
publishes the resolved match paths via `_publish_read_paths`. A matching record
adds the original sources to the protected-result capture; only if *all* stored
sources are trusted does `protected_result_source` raise the offload's filesystem
source to trusted. `classify_protected_result` merges the captured sources into
the turn's live IFC state through BudgetGate. This is the same route for a read
in the offloading turn and one in a subsequent turn. `ls`/`als` report directory
entries using the same path publisher; the directory itself retains the
untrusted HOME classification. Unlabelled/non-native reads cannot assert a
trusted result through `classify_protected_result`'s incomplete-provenance
fallback.

`file_search` only queries its memory/state index (`tools/extra.py`); it does
not index `conversation_history`. `memory_query` and `memory_get` query SAGA
atoms (`tools/memory.py`) and publish their own per-atom IFC metadata; agent
auto-recall uses that separate memory path. None of these recall routes reads
the offload directory. A model that explicitly reads an offload through file
tools receives the captured labels above, and an untrusted active source
re-enables the `_TAINTED_TURN_WRITE_TOOLS` and file-write vetoes in either
access-control mode.
