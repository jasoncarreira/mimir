# Structured subagents

Mimir registers one explicit typed subagent role in addition to DeepAgents' default `general-purpose` role:

- `critic-structured` — skeptical review role with a `CriticFindings` Pydantic schema.

DeepAgents validates the child agent's structured response inside the subagent graph. At the normal `task` tool boundary, the validated object is serialized back to the parent as JSON `ToolMessage` text. That means the parent LLM gets schema-shaped JSON instead of untrusted prose, but regular chat turns do not receive a parsed Python object.

If a non-LLM controller needs parsed objects, it should invoke a compiled child runnable directly and read `structured_response`; it should not scrape the parent-visible transcript.

## Permissions boundary

`critic-structured` uses a read-only DeepAgents filesystem permission profile: built-in filesystem write operations are denied. This is accidental-overreach protection, not a security sandbox. It does not secure arbitrary shell/process execution, so the role does not receive shell/process tools by default; strong isolation belongs in Worklink-style worktree/container execution.
