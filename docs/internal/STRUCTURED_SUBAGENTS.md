# Structured subagents

Mimir does not currently register a typed DeepAgents subagent. It explicitly registers the standard `general-purpose` role so delegated calls retain Mimir's authorization, budget, and fetched-content middleware instead of receiving DeepAgents' ungated default.

`StructuredOutputRetryMiddleware` remains generic runtime machinery for compiled child runnables that use structured output. It retries an empty native structured-output parse failure but does not retry a non-empty schema mismatch. There is no model-visible role using it today.

Worklink's pre-PR `work-reviewer` is separate from the interactive DeepAgents `task` menu. OpenCode loads that read-only agent from the pinned `opencode-feature-factory` plugin; Worklink invokes it directly and persists its advisory result with orchestrator-observed evidence.
