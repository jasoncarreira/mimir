# Conversation-history information flow

DeepAgents summarization offloads message text into `MIMIR_HOME/conversation_history`.
The offload is storage, not an integrity endorsement: a previous turn's outside
author can have supplied the bytes even when the turn reading them is clean.

File-tool reads of this root carry an `untrusted` / `active_ingest` filesystem
source. `shell_exec` read-back carries the same filesystem source in addition
to the shell output label. This includes direct readers, recursive search,
redirection, globs and nested `env`/`sh -c` readers. The classifier resolves
read targets against the shell's effective working directory and applies the
existing filesystem trust policy. Unresolved read targets and unparsed reader
options that could select HOME fail closed as active ingest. Reads of trusted
HOME paths retain their trusted filesystem source and do not taint the turn.

The live turn carrier merges the shell read source. A later write in
`_TAINTED_TURN_WRITE_TOOLS` is refused, including when the offloaded text was
written by an earlier turn whose own taint has already expired.
