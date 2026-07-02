# Claude Code soak requires CLI/OAuth and the controlled adapter

Observed on 2026-07-02 during Chainlink #739: the promotion gate cannot run from
a container that only has the source checkout. Safe probes showed `claude` was
not on `PATH`, `langchain-claude-code-mimir` was not installed in the local uv
environment, `CLAUDE_CODE_OAUTH_TOKEN` was unset, and no `.credentials.json`
file was discoverable by filename.

A later local soak in the operator container passed when launched through
`uv run --extra claude-code`: `claude --version` worked, auth smoke returned
`ok=True`, `langchain-claude-code-mimir==0.1.2` was installed, the upstream
`langchain-claude-code` distribution was absent, and the PreToolUse canary
blocked a prohibited Bash call before execution.

Do not promote `claude-code:*` support from source alone. The running daemon
must also install the `claude-code` extra / `langchain-claude-code-mimir>=0.1.2,<0.2`
and must not post-install the old `langchain-claude-code` fork after `uv sync`.
Validate with safe probes only; never paste token values or `.credentials.json`
contents into logs, Chainlink, or chat.
