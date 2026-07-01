# Retired: ChatClaudeCode Tool Results Streaming Gap

Status: retired by Chainlink #736.

The Claude Code streaming path now records SDK hook-derived `tool_events` in
turn metadata and `mimir.turn_logger.extract_turn_events()` emits paired
`tool_call` and `tool_result` records using the stable `tool_use_id`.

Covered event families:

- Built-in Claude Code tools such as `Bash` and `Read`.
- Mimir bridged tools exposed through Claude Code's MCP-style
  `mcp__langchain-tools__...` names.
- External MCP-style tools such as `mcp__github__get_issue`.
- Error results from `PostToolUseFailure`, surfaced as `is_error=True`.

This is observability only. Safety enforcement remains pre-execution and is not
derived from turn-log reconstruction.
