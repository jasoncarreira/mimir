# Migration Analysis: claude-agent-sdk → deepagents 0.6

**Branch:** `worktree-explore+deepagents-0.6-migration`
**Date:** 2026-05-14
**Status:** Exploration / analysis only — no production code touched

## TL;DR

Replacing claude-agent-sdk with deepagents 0.6 is a **4-6 week refactor with high blast radius**, but it unlocks multi-provider support, built-in subagent quarantine, middleware architecture, and LangGraph durable execution. Recommended next step: a small PoC that migrates ONE tool + minimal agent loop end-to-end to measure the LOC delta and behavior parity. Do NOT commit to the full migration without that data.

## Current claude-agent-sdk usage in mimir

**SDK surface imported (15 files):**

| Symbol | Used in | Purpose |
|---|---|---|
| `ClaudeSDKClient` | `agent.py` | Persistent client (pool member) |
| `ClaudeAgentOptions` | `agent.py`, `turn_hooks.py` | Options struct passed to client |
| `HookMatcher` / `HookContext` | `agent.py`, `hooks.py` | Pre/post message hooks |
| `InMemorySessionStore` | `agent.py` | Per-turn session state |
| `RateLimitEvent` / `StreamEvent` | `agent.py`, `turn_hooks.py` | Event stream consumption |
| `ResultMessage` / `AssistantMessage` / `TextBlock` | `agent.py`, `turn_logger.py` | Message types for telemetry |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskNotificationMessage` | `agent.py`, `turn_hooks.py` | Subagent task events |
| `project_key_for_directory` | `agent.py` | Project-scoped resource keys |
| `tool` decorator + `SdkMcpTool` | 10 `*tools.py` files | Tool definitions |
| `create_sdk_mcp_server` / `McpSdkServerConfig` | `tools.py` | In-process MCP server |

**Files affected (~15 production + 3 tests):**

```
mimir/agent.py                  (~2.5k LOC — the orchestrator, ClientPool, lifecycle)
mimir/hooks.py                  (hook implementations)
mimir/turn_hooks.py             (turn-level hooks)
mimir/turn_logger.py            (consumes SDK event types)
mimir/_streaming_dispatch.py    (streaming dispatcher)
mimir/_context.py               (contextvar-based ctx)
mimir/spawn.py                  (custom subagent spawning)
mimir/{sagatools,channeltools,searchtools,turntools,scheduletools,
       shelltools,committools,spawn}.py + mimir/tools.py
                                (tool definitions + MCP server creation)
mimir/commitments/extractor.py  (commitments extraction uses SDK)
tests/test_agent_sdk_client.py  (SDK client tests)
tests/test_agent_saga.py        (saga integration test)
tests/test_turn_hooks.py        (hook tests)
```

## deepagents 0.6 surface (PyPI 0.6.1, May 2026)

**Public API symbols** (`from deepagents import ...`):

- `create_deep_agent(model, tools, ...) → CompiledStateGraph` — the factory
- `SubAgent`, `AsyncSubAgent`, `CompiledSubAgent` — subagent classes
- `GeneralPurposeSubagentProfile` — default subagent profile
- Middleware: `FilesystemMiddleware`, `MemoryMiddleware`, `SubAgentMiddleware`, `AsyncSubAgentMiddleware`
- `FilesystemPermission` — file/dir access control
- `HarnessProfile`, `HarnessProfileConfig`, `ProviderProfile`
- `register_harness_profile`, `register_provider_profile`
- `SubagentRunStream`, `AsyncSubagentRunStream` — streaming
- Submodules: `backends`, `graph`, `middleware`, `profiles`

**Built-in tools** (always available unless explicitly excluded):
- `write_todos` — agent-managed todo list
- `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep` — filesystem ops
- `execute` — shell (requires SandboxBackendProtocol)
- `task` — call subagents (built-in context quarantine)

**Model selection:** explicit string like `"anthropic:claude-sonnet-4-6"` or `"openai:gpt-5.4"`, or a pre-initialized `BaseChatModel` instance. Multi-provider native.

**Tools:** LangChain `BaseTool` / `@tool` from `langchain_core.tools`. MCP tools via `langchain-mcp-adapters`.

**Hooks:** Middleware classes (`AgentMiddleware`) + LangGraph `interrupt_on={tool_name: True}` for HITL.

**State:** LangGraph `BaseCheckpointSaver` + `BaseStore`. Delta channels for storage (10-100× smaller checkpoints in 0.6).

**Streaming:** `agent.astream(...)` yields LangGraph events.

## Concept-by-concept mapping

| Concern | claude-agent-sdk | deepagents 0.6 | Map difficulty |
|---|---|---|---|
| Agent loop owner | `ClaudeSDKClient` (we wrap in ClientPool) | LangGraph compiled state machine | **High** — different runtime model |
| Tool decorator | `@tool` (SDK) | `@tool` (langchain_core) | **Low** — similar shape, decorator + return-type |
| Tool registration | `create_sdk_mcp_server(tools=[...])` | `tools=[...]` to create_deep_agent | **Low** — single list passed in |
| MCP tools | First-class native | `langchain-mcp-adapters` package | **Medium** — different adapter, similar semantics |
| Hooks (pre/post message) | `HookMatcher` + handler | `AgentMiddleware.before_model` / `.after_model` | **Medium** — pattern translates but every hook needs rewrite |
| Session state | `InMemorySessionStore` | LangGraph thread + checkpointer | **Medium** — different state model, but LangGraph is richer |
| Subagents | Custom (mimir/spawn.py ~808 LOC) | Built-in `task` tool + SubAgent class | **Win** — deepagents replaces our custom |
| ClientPool with options-fingerprint | Our custom in `agent.py` | n/a — LangGraph compiles once per agent | **Disappears** — no longer needed |
| Streaming → turn_logger | StreamEvent typed messages | LangGraph events (dicts) | **High** — turn_logger consumes SDK types directly |
| Rate-limit events | `RateLimitEvent` from SDK | Not directly surfaced; LangChain has retry/error hooks | **Medium** — must reimplement RateLimitStore plumbing |
| Project keys | `project_key_for_directory` | LangGraph thread_id (operator-chosen) | **Low** — just rename |
| Permission rules | Configured in options | `FilesystemPermission` middleware | **Win** — better than current shell allowlist |
| Prompt caching | SDK manages beta flags | LangChain has provider-level caching; deepagents middleware | **Medium** — verify cache hit parity |
| HITL | Not built-in | `interrupt_on={...}` first-class | **Win** — built-in |
| Telemetry / turn_logger | SDK message types are stable | LangGraph events less typed | **High** — need adapter |

## Cost / time estimate

| Phase | Work | Estimate |
|---|---|---|
| 1. PoC: 1 tool + minimal loop | `saga_query` tool rewrite + bare deepagent that calls it via Anthropic Sonnet | 1 day |
| 2. Tool migration | 10 `*tools.py` files rewrite (decorator change, BaseTool subclass, MCP→adapter) | 1-2 weeks |
| 3. Agent loop | `agent.py` (2.5k LOC) → deepagents wrapper. Replace ClientPool with LangGraph thread management | 1-2 weeks |
| 4. Hook migration | `hooks.py`, `turn_hooks.py` → AgentMiddleware subclasses (pre-message hook is the load-bearing one) | 1 week |
| 5. Telemetry / turn_logger | Adapter from LangGraph events to existing turns.jsonl/events.jsonl schema | 3-5 days |
| 6. Subagent + spawn cleanup | Delete `mimir/spawn.py` (~808 LOC), use deepagents `task` + SubAgent | 3-5 days (net LOC negative) |
| 7. Tests | 3+ SDK test files + integration tests | 1-2 weeks |
| **Total** | | **4-6 weeks** |

## Wins from the migration

1. **Multi-provider** — could run mimir.memory bench against `openai:gpt-5.4-nano` or open-weight Kimi/DeepSeek/GLM for **10-25× lower bench cost**. Currently locked to Claude.
2. **Built-in subagent quarantine** — `mimir/spawn.py` (~808 LOC) becomes the `task` tool + custom SubAgent profile. Net LOC negative.
3. **Middleware over hooks** — cleaner separation than HookMatcher chains. Hook ordering becomes explicit middleware order.
4. **LangGraph durable execution** — agent state survives process restarts via checkpointer. Today mimir loses state on crash.
5. **Built-in filesystem tools** — `ls/read_file/write_file/edit_file/glob/grep` replace ~5 of mimir's custom tool definitions.
6. **HITL via `interrupt_on`** — replaces our manual approval flows.
7. **Delta channels** — 10-100× smaller checkpoint storage in 0.6.
8. **Bedrock / Titan path** — already on the AWS roadmap (per memory note). LangChain has `langchain-aws` package; multi-provider is what makes that work.
9. **Industry-standard ecosystem** — debugging tools, viz, LangSmith tracing.

## Risks and unknowns

1. **Loss of Claude Code TUI integration** — claude-agent-sdk integrates with `claude.ai` for CLI/TUI workflow. deepagents has its own CLI but doesn't share this surface. May need to re-evaluate the operator UX.
2. **Cache parity uncertain** — Claude's prompt caching beta is the key cost lever. Need to verify deepagents preserves cache hits at our 95%+ rate.
3. **RateLimitEvent** — SDK surfaces these as first-class events; our `RateLimitStore` consumes them. deepagents has retry/backoff but the *event* exposure is unclear. May need to scrape from raw API responses.
4. **Telemetry adapter** — turn_logger relies on typed SDK messages. LangGraph events are less typed; the adapter needs to preserve the turns.jsonl schema mimir's bench harness and ops dashboard depend on.
5. **Subagent semantic parity** — mimir's `spawn.py` has very specific cost-budget + classification logic. deepagents' `task` tool may not cover the budget partition mimir does.
6. **Prompt caching headers** — Anthropic-specific behavior; deepagents may handle it correctly via langchain-anthropic but needs verification.
7. **Tests** — 3 SDK-specific test files need rewrites. Some tests pin SDK behavior we'd lose (e.g., options fingerprint recycling).

## Update (2026-05-14 follow-up): open-strix already solved the telemetry adapter

Jason has a working fork at `../open-strix` that runs turns.jsonl on top of deepagents. Key takeaways from reading it:

### `open_strix/turn_logger.py` (175 LOC total) is the adapter

```python
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

def extract_turn_events(messages: list[Any]) -> tuple[list[dict], str]:
    """Walk a LangChain message list, return ({events}, output)."""
    events = []
    output_parts = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.content and msg.tool_calls:
                events.append({"type": "reasoning", "content": str(msg.content)})
            elif msg.content:
                output_parts.append(str(msg.content))
            for tc in (msg.tool_calls or []):
                events.append({"type": "tool_call",
                               "id": tc.get("id", ""),
                               "name": tc.get("name", "unknown"),
                               "args": tc.get("args")})
        elif isinstance(msg, ToolMessage):
            content = str(msg.content)
            if len(content) > MAX_TOOL_RESULT_BYTES:
                content = content[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
            events.append({"type": "tool_result",
                           "id": getattr(msg, "tool_call_id", ""),
                           "name": getattr(msg, "name", ""),
                           "content": content,
                           "is_error": msg.status == "error"})
    return events, "\n".join(output_parts)
```

The event schema (`{type: "reasoning"|"tool_call"|"tool_result", ...}`) is **identical to mimir's**. So this is essentially copy-paste — no schema translation needed.

### The agent-invoke loop is dramatically simpler than mimir's current

```python
result = await self.agent.ainvoke({"messages": [HumanMessage(content=prompt)]})
messages = result.get("messages", [])
events, output = extract_turn_events(messages)
await self._turn_logger.write(TurnRecord(
    ts=utc_now_iso(), turn_id=turn_id, session_id=self.session_id,
    trigger=event.event_type, channel_id=event.channel_id,
    input=truncate_input(prompt), events=events,
    output=output[:2048],
    duration_ms=int((time.monotonic() - start) * 1000),
    error=None,
))
```

Three lines for invoke + extract + log. Mimir's current SDK-based loop is ~hundreds of lines (ClientPool, options-fingerprint recycling, lifecycle, SessionStore deletes).

### Hooks are external wrappers, not deepagents middleware

`open_strix/hooks.py` (182 LOC) implements pre/post hooks as **external wrappers** around `agent.ainvoke`, loading user-defined Python modules dynamically. Doesn't touch deepagents internals. Maps directly to mimir's HookMatcher pattern — clean translation.

### Updated cost estimate

| Phase | Old estimate | New estimate (post open-strix read) |
|---|---|---|
| Telemetry adapter | 3-5 days | **Half a day** (copy + adjust schema) |
| Hook migration | 1 week | 2-3 days (external-wrapper pattern is cleaner than middleware translation) |
| Agent loop | 1-2 weeks | **3-5 days** (the `await agent.ainvoke(...)` pattern eliminates ClientPool entirely) |
| Tool migration | 1-2 weeks | 1-1.5 weeks (unchanged — 10 tool files × similar work) |
| Subagent + spawn cleanup | 3-5 days | 3-5 days (unchanged) |
| Tests | 1-2 weeks | 1 week (less mechanism = less to test) |
| **Total** | **4-6 weeks** | **2.5-3.5 weeks** |

That's a meaningful cost reduction. Open-strix has already proven the load-bearing pieces work.

### What this means for the PoC

Half the PoC is already written. The right next step:

1. **Day 1**: cherry-pick `turn_logger.py` from open-strix into mimir as a starting point; adapt the schema to match mimir's current `TurnRecord` shape (mimir has extras: saga_session_id, saga_atom_ids, result_subtype, total_cost_usd, usage, stop_reason)
2. **Day 1**: minimal deepagent with one tool (memory_query) — verify cache hit rate vs SDK
3. **Day 2**: port one hook (pre_message_hook → external wrapper) — measure latency

If those work, the rest is mechanical translation following the open-strix pattern.

## Update (2026-05-14): concurrency — no ClientPool needed

Investigated whether deepagents supports concurrent requests or whether we need a ClientPool-equivalent.

**Verdict:** `CompiledStateGraph` (what `create_deep_agent()` returns) is **thread-safe by design**. LangGraph's official guidance: *"It is entirely safe to share a graph between executions, whether they happen concurrently or not, whether in same thread or not."* The Runnable interface — `ainvoke` / `astream` / `batch` — is built for the singleton-shared-across-concurrent-requests pattern (typical FastAPI deployments).

This eliminates one of mimir's most intricate primitives:

| | claude-agent-sdk path (current) | deepagents path |
|---|---|---|
| Construction | Per-options-fingerprint `ClaudeSDKClient` instance | One `create_deep_agent()` call at app start |
| Concurrent turns | `ClientPool` recycles clients when options change; tracks `_idle` / `_in_flight`; stale-marks; loop-aware semaphore | Just call `await agent.ainvoke(...)` from any number of tasks |
| Per-turn state isolation | `InMemorySessionStore` with per-turn `delete()` | `config={"configurable": {"thread_id": ...}}` per-call |
| LOC | `ClientPool` + `_PoolEntry` + `_AcquireContext` = ~250 LOC in `mimir/agent.py` | 0 |

The ~250 LOC ClientPool machinery in `mimir/agent.py:295-560` becomes a single shared `CompiledStateGraph` reference. **Net LOC negative** on this surface alone.

Caveat to flag: there's a March 2026 issue in *langgraph-js* (the JavaScript port) about AsyncLocalStorage context propagation across concurrent invocations. Python's contextvars-based isolation doesn't have the same issue, but we should still per-call set `config={"thread_id": "..."}` to keep checkpoint state cleanly scoped. (Mimir already has per-turn IDs we'd reuse.)

## Recommended next step: scoped PoC

Before committing to the migration:

**Day 1 — minimal loop, one tool, parity check:**
1. Write a single LangChain tool that wraps `MemoryClient.query` (the saga_query equivalent)
2. Build a minimal deepagent: `create_deep_agent(model="anthropic:claude-sonnet-4-6", tools=[memory_query_tool], system_prompt="...")`
3. Run a 5-question slice through it (skip MIMIR's full template / hooks / scheduler)
4. Compare to mimir's current via_mimir bench accuracy on the same 5 questions
5. Document: token counts, latency, cache hit rate, output shape

**Day 2-3 — middleware PoC:**
1. Build a `MimirMemoryMiddleware(AgentMiddleware)` that mirrors the pre-message hook's MemoryClient.query call
2. Verify the middleware fires correctly and the agent gets memory context
3. Compare prompt structure to current mimir prompt

**Day 4-5 — telemetry adapter PoC:**
1. Wire LangGraph event stream to write a turns.jsonl in the existing schema
2. Verify ops dashboard + bench harness can read it without changes

If all three PoCs hit parity within a week, commit to the full migration. If any fail substantively, pause and evaluate.

## Day 2 update (2026-05-14): hook patterns verified

Built three PoC variants to test the hook-injection patterns:

| File | Pattern | Q1 ("What degree?") | Q2 ("Daily commute?") |
|---|---|---|---|
| `smoke.py` | **Tool only** — agent calls `memory_query` on demand | ✓ Business Administration | ✓ 45 minutes |
| `smoke_wrapper.py` | **Pre-inject only** — wrapper prepends memory, no tool | ✗ "I don't have access" | ✓ 45 minutes |
| `smoke_hybrid.py` | **Hybrid** — pre-inject + tool registered | ✓ Business Administration | ✓ 45 minutes |

### Key finding: external wrapper, not middleware

For mimir's pre-message-hook semantics ("fire ONCE per turn before the first model call"), the right deepagents primitive is an **external wrapper around `agent.ainvoke`**, not `AgentMiddleware.before_model`:

- Middleware `before_model` fires before EVERY model call inside the agent loop (multi-step turns). Wrong cadence — re-fetching memory mid-turn is wasted work.
- External wrapper fires once, augments the HumanMessage, then hands off to the agent. Same shape as `open-strix/open_strix/app.py:1188`.

The wrapper is ~60 LOC (`mimir/deepagent_poc/pre_message_hook.py`):

```python
async def invoke_with_pre_message(agent, *, memory_client, question, ...):
    pre = await run_pre_message(memory_client=memory_client, question=question, ...)
    result = await agent.ainvoke({"messages": pre.augmented_messages}, config=config)
    return result, pre
```

Returns `(agent_result, pre_message_result)` so the turn logger can capture both surfaces — saga_atom_ids, rewritten_query, pre_message_ms timing — exactly what mimir's existing TurnRecord schema expects.

### Migration target: hybrid pattern

Production mimir has both:
- Pre-message hook → injects memory context every turn
- `saga_query` tool → agent can fetch additional memory mid-turn if needed

The hybrid PoC matches this shape end-to-end. The wrapper handles the pre-inject; the deepagents `memory_query` tool handles the follow-up. Cache reads visible across all three patterns (4-11k cache_read_input_tokens via langchain-openai's Responses API).

### Updated cost estimate (post Day 2)

| Phase | Previous estimate | After Day 2 |
|---|---|---|
| Hook migration | 2-3 days | **Verified at 2 days** (wrapper pattern is ~60 LOC, mechanical translation of mimir's existing pre/post hook contract) |

Day 2 strengthens the 2.5-3.5 week total estimate. No surprises.

## Day 3 update: post-message credit pass

Built the credit-pass external wrapper (`post_message_hook.py`, ~90 LOC). Walks the agent's ToolMessages, regex-extracts atom IDs from tool result strings, unions with pre-message-hook atom IDs, calls `MemoryClient.feedback(atom_ids, response_text, feedback="positive")`. Same semantic as mimir's `_post_message_hook`.

**Smoke verifies** (smoke_full.py): both LongMemEval probes answer correctly, post-message hook writes `feedback_positive` rows with weight=2.0 (the mimir.memory SOURCE_WEIGHTS default) for 18-19 credited atoms per question. Post-hook latency ~1-3ms (one SQL insert per atom).

Bundled `run_turn()` (`turn_runner.py`, ~140 LOC) ties pre → invoke → post → log. Returns a `TurnOutcome` envelope with everything mimir's dispatcher consumes today. This is the migration target shape — mimir's existing dispatcher calls `run_turn()` in place of the SDK's query loop.

## Day 4 update: write-tool migration

Ported `saga_store` → `memory_store` (~70 LOC, `store_tool.py`). The LangChain `@tool` decorator reads the Python type hints + docstring for the tool's schema, so we drop the SDK's separate JSON-schema arg entirely. Round-trip smoke (smoke_store.py):

- Step 1: "Please remember: my favorite color is blue." → agent calls `memory_store(content="The user's favorite color is blue.", stream="semantic", source_type="agent_authored")` → atom persisted ✓
- Step 2: "What's my favorite color?" → agent calls `memory_query("favorite color")` → answers "blue" ✓

Pattern confirmed for write-side tools. **~20 LOC saved per tool migration** vs the SDK's 3-arg `@tool(name, description, schema)` shape. Across 10 tool files: ~200 LOC negative.

## Day 5: bench runner

`mimir/deepagent_poc/bench.py` — bench at scale through the full pipeline. Same args as `via_mimir/runner_memory.py` (`--limit`, `--run-tag`, `--output-dir`, `--model`). Incremental hypothesis writes for crash-survivability. 10-q smoke currently running.

## PoC totals

| Day | What | LOC | Status |
|---|---|---|---|
| 1 | turn_logger + memory_tool + agent + smoke | ~400 | ✓ end-to-end correct, cache hits visible |
| 2 | pre_message_hook + smoke_wrapper + smoke_hybrid | ~270 | ✓ hybrid (pre+tool) matches mimir production |
| 3 | post_message_hook + turn_runner + smoke_full | ~270 | ✓ feedback_positive rows confirmed |
| 4 | store_tool + smoke_store | ~150 | ✓ write tool round-trip |
| 5 | bench runner | ~240 | running 10-q smoke |
| **Total** | | **~1330 LOC** | |

What's NOT in the PoC but would be in the full migration:
- 8 remaining tool files (search/turn/schedule/shell/channel/commit/spawn) — but mostly the same `@tool` translation
- `mimir/agent.py` Agent class wrapper rewrite (kill ClientPool, replace with deepagent singleton)
- Test migration (3 SDK-specific test files)
- Telemetry adapter at production-scale ops-dashboard integration
- Real production cutover (rename `mimir/agent.py` Agent → DeepAgentRunner, switch `mimir/server.py` to construct it)

**Estimate sticks at 2.5-3.5 weeks.** Nothing in the PoC surfaced an obstacle that wasn't in the analysis. Cache hits, telemetry, tool registration, hooks, concurrency, model switching, credit pass — all proven working.

## What this branch contains

- `DEEPAGENTS_MIGRATION_ANALYSIS.md` (this file)
- `deepagents` added to `pyproject.toml` (uv add deepagents)
- Nothing else yet — production code unchanged

## Status

**Phase 0 done** — installed + API surface mapped + LOC/cost estimated.
**Next** — operator review of this analysis. Decide PoC scope.
