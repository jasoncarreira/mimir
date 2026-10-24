# Conversation-history offload provenance

DeepAgents' `SummarizationMiddleware._offload_to_backend` and
`_aoffload_to_backend` append evicted messages (excluding previous summaries)
to `/conversation_history/{turn_id}/{session_id}.md` for a live turn. Mimir wraps these methods in
`_deepagents_summarization.py`. `config.DEFAULT_FOLDERS` permits middleware
writes; that permission does **not** establish content trust.

## File-tool reads during a turn

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
turn use the same mechanism. A completed turn's offload is wiped, so later turns
have no history bytes to read from that turn. `ls`/`als` names and the
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
Provenance bookkeeping is non-fatal: a snapshot, capture or persistence failure
preserves the upstream offload result and invalidates the sidecar best-effort.
Missing or stale records still read back as untrusted; no fallback mints labels.

Records live in `.mimir/conversation_history_labels/`, outside file-tool writable
roots. Version 1, nonempty complete sources, an exact digest, a direct parent
under HOME/conversation_history (or its turn-id child), and a non-symlink path are required. Missing,
stale, malformed, unsupported or incomplete records add no original sources;
the untrusted HOME label still applies. Owner attestations are never recreated.
This is server-owned **relative to file tools**, not a cryptographic signature:
a clean turn's authorized `shell_exec` can delete or forge records. Deletion
fails closed. Forgery requires that clean-turn model's shell authority; the
record never raises path trust even then.

## Shell read-back: accidental-read protection

Offloads, evicted large results and fetched bodies live beneath their existing
labeled roots in turn-id directories. Each directory is wiped on turn completion,
failure or cancellation, and crashed-turn leftovers are swept at startup. DeepAgents'
HumanMessage eviction and inline-media offloads still use shared `conversation_history/`
paths; the shell name-match backstop covers these too, and startup sweeps them.
The first startup after upgrade purges pre-existing shared bytes whose source turn
cannot be established. Gmail's operator-owned `triage-dropped.jsonl` is preserved
unchanged. A HOME `.ignore` keeps `rg` from unqualified searches of these roots,
`logs/`, `messages/` and `state/pollers/`. This also affects the admin file-tool
`grep` path that uses `rg`; the non-admin Python fallback does not consult `.ignore`.
Unsafe symlinked stores, CI-log/poller containers, rollout-marker containers or
`.ignore`, and unreadable sweep subdirectories, are skipped with a warning, not
followed or allowed to prevent startup. Startup draft cleanup is limited to
installed pollers whose manifests grant research proposal capabilities; arbitrary
poller `drafts/` directories are preserved. Late tool threads cannot allocate a turn store after
final cleanup has closed that context; this is not process isolation for already
open descriptors.

For other persistent content (including turn logs and Worklink run logs),
`shell_exec` and `bash_async` classify output as untrusted **active_ingest**
when raw command text names `turns.jsonl`, `events.jsonl`, `chat_history.jsonl`,
`triage-dropped.jsonl`, `conversation_history`, `logs/` or `messages/` as a path
component (including leading `/logs` and `/messages`), or `worklink-ready-queue`.
Bare prose words `logs` and `messages` do not match. Recursive searches use a
small lexical pipeline-segment classifier, not a full shell parser. `rg`/`ag`
are recursive; `grep`/`egrep`/`fgrep` recognize `-r`/`-R` (including bundled
flags), `--recursive`, `-d recurse` and `--directories=recurse` anywhere among
their arguments. Non-recursive long options such as `--color` do not enable
this rule. Patterns and option values are not treated as path operands.

A recursive search taints when an operand (or cwd for an implicit search) is
HOME, an ancestor, or anywhere under HOME, except when all searched paths are
inside the self-authored `memory/`, `docs/`, `skills/`, `prompts/` or
`.mimir_builtin_skills/` roots. `state/` is deliberately not a recursive-search
exception because it contains poller content. `$MIMIR_HOME`, `${MIMIR_HOME}`,
`$HOME`, `${HOME}` and `~` expand to agent HOME for this check; dots and globs
retain their searched-tree boundary. Simple `cd` chains update the lexical cwd.
Unrelated `echo ~` segments no longer broaden a checkout-local search. External
checkouts and sibling paths stay clean, while unknown HOME descendants taint.
A cwd inside a named
content store taints ordinary reads too. Both shell tools resolve omitted cwd
through the shell default/session cwd helper for classification. The command
still runs; the existing tainted-turn write veto then applies. Ordinary shell
results remain informational. This is protection against accidental reads by
a clean-turn model, not a security boundary against a deliberate bypass: there
is no shell parser, OS isolation or log redaction in this design.

## Recall

`file_search` queries the memory/state index (`tools/extra.py`), not this
directory. `memory_query`/`memory_get` and auto-recall query SAGA atoms
(`tools/memory.py`) and publish their separate per-atom IFC metadata. None reads
these offload files. Explicit file-tool read-back activates
`_TAINTED_TURN_WRITE_TOOLS` and file-write vetoes in either enforcement mode;
the shell name match covers accidental reads of persistent logs.
