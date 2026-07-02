# Claude Code soak requires CLI and OAuth in the Worklink container

Observed on 2026-07-02 during Chainlink #739: the promotion gate cannot run from
a container that only has the source checkout. Safe probes showed `claude` was
not on `PATH`, `langchain-claude-code-mimir` was not installed in the local uv
environment, `CLAUDE_CODE_OAUTH_TOKEN` was unset, and no `.credentials.json`
file was discoverable by filename.

Do not promote `claude-code:*` support from this state. First provide the
Claude Code CLI, install the `claude-code` extra, and mount or inject OAuth
credentials. Validate with safe probes only; never paste token values or
`.credentials.json` contents into logs, Chainlink, or chat.
