# Requester/resource authorization policy

Status: decision artifact for Chainlink #854/#856. This document defines the security contract that implementation slices must preserve. It does not enable multi-user ingress or change the single-operator default.

## Frame decision

A static `admin / regular-scoped / open` label on a tool is not a complete authorization model. It is only a coarse capability default. Authorization is a decision over:

```
(requester principal, turn provenance, tool provenance, operation,
 resource arguments, resource ownership, information-flow state)
```

The runtime must therefore classify an action as one of:

- **Open** — callable without an authenticated requester only when the operation is safe for every resource the tool can reach. Open tools must not accept an argument that widens their authority.
- **Admin-required** — requires an authenticated principal with the `admin` role, or an explicitly trusted autonomous principal admitted by a server-owned scheduler/poller path.
- **Resource-scoped** — requires an authenticated regular or admin principal and an executable resource adapter that maps the concrete tool arguments to resources and checks them against the principal. A bare `regular-scoped` label without an adapter fails closed.

Argument-dependent tools can produce different decisions per call. For example, `send_message` to the triggering channel is resource-scoped and may be allowed; the same tool targeting another channel is admin-required.

## Assets and adversaries

Protected assets include operator and other users' channel history, turns, SAGA memory, commitments, files and configuration, credentials, external-service data exposed by MCP tools, outbound messaging identities, schedules/pollers, source repositories, and process execution.

Threats considered:

- an allowlisted regular user intentionally requesting another user's or the operator's data;
- an unknown external caller reaching a bridge or HTTP ingress;
- prompt injection causing the model to select a tool or arguments outside the requester's authority;
- a model forging `session_id`, source names, channel ids, tool provenance, or ownership claims;
- an external MCP server changing its advertised tool name or schema;
- legacy records lacking owner metadata;
- concurrent turns causing ContextVar or single-active-turn fallback to resolve the wrong requester;
- a read that is individually allowed followed by an outbound send that leaks the returned data.

The model is not a policy enforcement point. Tool descriptions, prompt-injection labels, and instructions to “only use your own data” are guidance, not authority.

## Principals and authoritative context

### Principal kinds

1. **Authenticated admin** — a server-resolved canonical identity with the `admin` role.
2. **Authenticated regular** — a server-resolved canonical identity with an allowlisted non-admin role.
3. **Unknown external** — missing, unresolved, unauthenticated, or ambiguous external identity. It has no regular-user authority.
4. **Trusted autonomous** — a server-created scheduler, poller, synthesis, or system turn. This is a distinct service principal, not “admin because author is missing.” Its admitted capabilities should be explicit by turn type; the initial compatibility policy may grant the existing internal surface while logging the principal kind.

### Required server-owned authorization context

Each turn needs a separate frozen authorization context created at ingress and carried directly to tool middleware:

- canonical principal id and principal kind;
- immutable role snapshot from the server-side identity resolver;
- triggering channel id and bridge/source instance;
- authenticated ingress type;
- server-owned turn id and interactivity classification;
- policy/enforcement version.

The context must be attached before model execution and must not be reconstructed from model arguments. It is not the mutable `TurnContext`: ordinary LangChain calls receive it through the exact request/runtime bound to the acquired turn; MCP wrappers close over or receive a server-generated opaque turn binding; subagents receive an explicitly attenuated derived context; detached tasks receive no requester authority unless explicitly delegated. Missing carrier metadata denies every non-open operation under enforcement. Mutating the original event or reloading the identity resolver cannot change the immutable snapshot mid-turn.

### Compatibility and trusted-autonomous truth table

| Ingress/principal | Enforcement off | Enforcement on |
|---|---|---|
| Authenticated interactive admin/regular | Preserve the current single-operator behavior, but log the decision that enforcement would make; prohibited-action guards remain active. | Apply the full policy. |
| Generic HTTP event | Possession of the current API key authenticates the transport only, not an admin principal. Deny every non-open operation unless a future server-owned credential mapping establishes a named principal and admitted ingress. | Same fail-closed rule. Forged event fields cannot widen authority. |
| Authenticated web chat | Treat as an interactive authenticated principal, not generic HTTP-event ingress: the server derives a per-user identity and channel from the per-user credential. The admin master API key is deliberately not a chat identity. | Apply the full policy using that resolved principal; admin-tool use requires the per-user identity to carry the admin role. |
| Unknown/missing external | Open operations only. | Open operations only. |
| Registered trusted-autonomous service principal | Apply its explicit capability matrix; no source-name or missing-author shortcut. | Same. |
| Unknown synthetic trigger | Open operations only. | Open operations only. |

An unknown native, built-in, dynamically registered, or external operation is admin-required under compatibility mode and denied to non-admins under enforcement; it is never implicitly open. The runtime inventory must cover the final assembled tool surface, not only `all_mimir_tools()`.

Web chat is HTTP-transported but is not currently stamped as generic HTTP-event ingress. Its route resolves a server-owned per-user credential mapping, derives `web-<canonical principal>` rather than trusting a client channel, and rejects the shared admin master key as a chat identity. Consequently the operator can retain admin-tool use in web chat only through a named per-user credential mapped to an admin principal; possession of `MIMIR_API_KEY` alone is intentionally insufficient.

**Enablement ordering invariant:** enforcement must not be enabled until the trusted-autonomous capability registry and entries required by deployed scheduler, poller, synthesis, and system turns have landed. Removing `resolve_active_ctx` fallback authority in leaf 1 is safe while enforcement remains off, but enabling enforcement after that removal and before leaf 2's registry exists would incorrectly strip internal turns of required access.

Trusted-autonomous authority is registered by stable service principal and server-owned creation path. Each entry specifies exact trigger type, allowed operations/adapters, readable resource domains, sink destinations, cross-principal-read permission, and declassification permission. Scheduler, poller, synthesis, and system turns are separate entries rather than one blanket admin class. Unknown trigger names receive no privileged capability. The initial compatibility registry may reproduce currently required maintenance access, but must enumerate it and must not rely on `trigger != "user_message"`.

Administrator status can authorize a sink decision, but it does not erase information labels. Durable declassification remains a distinct audited action.

Chainlink #840's server-computed `TurnInteractivity` is necessary but insufficient. It answers whether a turn is interactive; it does not establish who the requester is, whether the ingress authenticated them, or what resources they own.

The following are never security authority:

- client/model-provided `source`, `trigger`, `author`, `channel_id`, or `session_id` fields on generic HTTP events;
- source names such as `web`, `api`, or `stdin` by themselves;
- ContextVar values inherited through detached/forked tasks;
- `get_only_active_turn()` or another single-active-turn heuristic;
- prompt-rendered identity text.

If authoritative context is missing or ambiguous, external turns are treated as unknown external and resource-scoped/admin-required actions are denied. Tool handlers must not downgrade a denial because access-control context lookup failed.

## Decision order

For every model-invoked operation, evaluate steps 1-6 below. Every egress path, including harness-emitted egress that does not originate in a model tool call, must then pass steps 7-8 before emitting content:

1. Resolve the immutable server-owned authorization context. Missing/ambiguous external context denies anything not open.
2. Resolve stable tool provenance and operation identity. Unknown external MCP provenance is admin-required.
3. Apply prohibited-action policy. Authorization cannot make a prohibited action permissible.
4. Determine the capability class for this call, including argument-dependent escalation.
5. For admin-required calls, require an admin or an explicitly admitted trusted-autonomous capability.
6. For resource-scoped calls, run the registered adapter, resolve every referenced resource, and require all predicates to pass. Missing adapter, unknown resource, mixed-owner batch, wildcard, or parse failure denies the call.
7. At the final egress boundary, apply read-to-egress policy to the accumulated labels and concrete destination before any model-invoked or harness-emitted sink executes.
8. Emit a structured allow/deny audit event with principal kind/id, stable operation or egress-path id, resource classes/ids (redacted where sensitive), policy version, decision, and reason.

Enforcement mode may remain off by default for the current single-operator deployment. However, server-stamped HTTP-event ingress remains fail-closed for privileged actions, and any future multi-user ingress must require enforcement rather than relying on the compatibility flag.

## Resource ownership and scope

Ownership uses canonical principal ids, not display names or platform aliases. A resource may instead be explicitly public, admin-owned, service-owned, or shared with a documented ACL. “Same deployment” is not ownership.

### Triggering channel and messaging

- The triggering channel is server-stamped in the authorization context.
- A regular principal may reply only to that triggering channel, and only through a bridge instance admitted for that turn.
- Cross-channel sends, broadcast/list targets, an explicit different channel, or a bridge change require admin.
- A DM channel is owned by its participants. A guild/public channel is not automatically readable or writable by every allowlisted user; bridge membership/ACL evidence or an operator-configured channel ACL is required.
- `react` follows the same channel/message scope as `send_message`; reacting to a message outside the triggering channel requires admin.
- Channel aliases must resolve server-side before comparison.

### Turn and message history

- Regular users may read turns/messages whose authoritative channel scope is the triggering channel and whose ACL admits that principal.
- Cross-channel history, scheduler/poller/system turns, operator DMs, and records with no authoritative channel ownership are admin-only.
- `turn_id`, message id, limit, and time range are selectors, not capabilities. Guessing an id must not bypass the scope predicate.
- `fetch_channel_history` with an omitted channel defaults to the triggering channel; an explicit different channel requires admin.

### Commitments

- New commitment records must carry `owner_principal_id` and, where applicable, `recipient_principal_id` in addition to `channel_id`.
- A regular principal may list or mutate a commitment only if they own it. Recipient status alone does not grant mutation authority.
- Channel-scoped commitments additionally require access to that channel.
- Agent-internal/service-owned commitments and legacy records missing owner metadata are admin-only.
- `commitment_list` must filter before rendering; complete/snooze/dismiss must check ownership after lookup and before append.

### Filesystem and indexed files

- Regular-user filesystem access is denied by default. Existing `/mimir-home`, source, benchmark, attachment, state, memory, prompt, credential, and repository roots are admin resources.
- A future regular-user file surface must be an explicit principal-owned virtual root, resolved canonically before filesystem access. Adapters must reject absolute paths outside that root, `..`, symlink escapes, alternate aliases, and glob expansion that crosses the root.
- `read_file`, `ls`, `glob`, `grep`, `file_search`, and attachment access need the same root policy. A read-only verb is not open merely because it does not mutate files.
- Legacy files without ownership metadata are admin-only; path naming conventions are not ownership proof.

### SAGA memory and sessions

- SAGA atoms, observations, triples, and session summaries need an authoritative owner/visibility domain. At minimum: owner principal, originating channel, and visibility (`private`, explicit shared ACL, or service/admin).
- Regular-user query/get operations must apply scope in the storage query, not fetch globally and filter formatted output afterward.
- Atom ids and model-provided `session_id` are selectors, not authority.
- Stores, feedback, contribution marking, session closure, skill-learning writes, and forgetting mutate shared memory integrity and are admin/service-only until SAGA supports principal-partitioned write policy.
- Legacy atoms/sessions without ownership metadata fail closed for regular users. They remain visible to admin/trusted autonomous maintenance under explicit policy.
- Cross-principal synthesis and consolidation may run only as a trusted autonomous capability and must not make private source content visible to a different principal.

### Ownership creation, derivation, and migration

Schema migrations may add ownership metadata, but must not infer ownership from free text, display names, or a model-generated summary. Deterministic provenance (authoritative channel/turn records plus identity resolution) may backfill ownership; otherwise mark the record legacy/admin-only.

Commitment ownership is assigned to the authenticated requester when extracted from or created for their request, including an agent promise made to them. `recipient_principal_id` does not grant mutation rights. Group-channel ownership requires an explicit owner at creation; absent that it is service/admin-only. CLI/operator-created records are admin-owned, and scheduler/poller-created records are owned by their registered service principal. Deduplication keys include owner so records belonging to different principals cannot collapse together.

Raw SAGA atoms inherit the authenticated principal and authoritative originating channel. Session summaries inherit that session's owner/domain. Derived observations and triples receive the intersection of all source ACLs; mixed-owner or missing-source provenance that cannot form a safe intersection becomes service/admin-only. Feedback, deletion, and forgetting require authority over every affected source/derived object. Cross-principal synthesis may compute as an admitted service principal but cannot widen the resulting ACL.

Channel sessions maintain that inheritance as a frozen, monotonic accumulator built only from server-created inbound authorization contexts. The synthesis service may use its own authority to read and compute, but both the session-summary writer and commitment extraction consume the accumulated source ACL from the server carrier. A change in owner or domain, or any missing authoritative provenance, irreversibly collapses the accumulator to legacy/admin-only for that session; model arguments and synthesized identity cannot replace it.

## External MCP tools

External tool authorization keys on stable provenance, not the LangChain display name alone. A versioned provenance record contains:

- an operator-generated immutable `server_config_id`;
- transport type and stable endpoint/command identity;
- a canonical digest of non-secret config plus secret-reference names (never secret values);
- original MCP tool name;
- canonical input-schema digest and, when available, output/capability-schema digest;
- adapter id/version, approval timestamp, and policy version.

Canonical JSON uses sorted object keys and normalized schema representation before hashing. Routine secret-value rotation does not change the digest, while changing the secret reference, endpoint/command identity, original tool name, or schema does. Server restarts do not change identity. Removed tools retain a tombstoned policy record for audit but are not callable.

The bridge must preserve the record as metadata on the wrapped tool and pass it to authorization middleware. Parsing `mcp_<server>_<tool>` is not authoritative because underscore normalization can collide and remote names can change.

Policy rules:

- An unclassified external MCP tool is admin-required.
- A configured `open` tool is allowed only if review establishes that no argument or remote default can select a protected resource or external sink. Open should be rare.
- A configured resource-scoped tool must name a registered adapter version. A tier label without an adapter is invalid and fails closed.
- Adapters validate the concrete input schema and arguments, identify source/sink resources, enforce ownership/ACL predicates, reject wildcard/unknown/batch ambiguity, and return a structured decision.
- Schema/name/provenance drift invalidates the classification until the operator re-approves it. Startup should surface stale policy rather than silently matching by suffix.
- Config mutation (`set_mcp_tools`, `clear_mcp_tools`, UI/API policy edits) is not agent-callable regular authority and remains admin/operator controlled.

Example: a Gmail `get_message` adapter may require the authenticated external account to be mapped to the requester and the message to belong to that account. `send_email` is also a sink and must pass the egress policy. A generic “gmail = regular-scoped” label is insufficient.

## Read-to-egress information-flow policy

Per-tool checks do not prevent an allowed private read from being copied into a separately allowed send. The conservative policy is per-turn taint tracking enforced outside the model:

- Labels are initialized before the first model call from every server-supplied input: inbound/folded messages, recent history, automatic SAGA retrieval, session summaries, skill-memory or indexed-file injection, continuation/recovery context, and attachments. Protected tool results and subagent results add labels afterward, including partial results; a failed call adds labels if protected content may have been returned.
- Source labels contain principal/domain, channel/resource scope, and sensitivity class. They propagate into subagents, spawned processes, continuations, and resumed turns; delegated contexts may only attenuate capabilities, not labels.
- Sinks are egress paths, not merely model-invoked tools. They include `send_message`, reactions/directives carrying text or files, outbound email/chat/calendar/GitHub/Jira/Drive MCP writes, file uploads, webhook/HTTP posts, `fetch_url` or shell/process invocations that can transmit data, spawned agents/processes with external connectivity, and any future notification tool.
- Harness-emitted channel egress is subject to the same check at its final send/edit boundary even when no model tool call occurs. This explicitly includes `MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS` delivery of captured final text; `MIMIR_RESEND_NUDGE_CHANNELS` recovery and any delivery it induces; and `MIMIR_ACTIVITY_PANEL_CHANNELS` panel posts/edits in both coarse and detailed modes. Detailed activity-panel tool-result previews carry the source labels of those results; `scrub_detail` secret/path redaction does not declassify them. Coarse activity metadata also remains labeled and checked rather than being presumed harmless.
- A sink is allowed only when every accumulated label is permitted to flow to the sink's destination. Same-principal/same-channel replies may be allowed; cross-principal, cross-channel, public, or unknown destinations require admin or a narrowly defined declassification action. Targeting the triggering channel does not by itself permit content labeled from a different principal or protected domain.
- Unknown source labels or unknown sink destinations deny for regular users.
- Taint is monotonic for the turn. Summarizing, paraphrasing, transforming, or having the model assert “no secrets included” does not clear it.
- Declassification must be an explicit admin action with auditable source labels and destination, not a prompt instruction.

Residual limits: coarse tool-result taint over-restricts mixed public/private results and cannot prove semantic non-disclosure. It is still enforceable and safer than prompt-only controls. Finer field-level IFC may be added later, but multi-user ingress must not claim strong exfiltration resistance without at least the coarse turn-level guard.

## Initial capability baseline

Until resource adapters and ownership schemas exist:

- Admin-required: shell/async shell, file reads and writes, source/state searches, SAGA reads and writes, commitments reads and writes, schedule/poller/config mutation, spawns, Worklink, proposals, updates, cross-channel messaging/reactions, all unclassified external MCP tools, and external sinks.
- Resource-scoped only after the relevant adapter lands: same-trigger-channel `send_message`/`react`, same-channel history, owner-scoped commitments, principal-partitioned SAGA reads, principal-owned virtual filesystem reads, and classified MCP operations.
- Open: deterministic computation and introspection that accepts no protected-resource selector and produces no external side effect. Existing tools should not be presumed open; each needs an inventory decision.

## Implementation invariants and tests

Implementation leaves must prove:

- tool authorization consumes the server-owned context directly and fails closed when it is absent/ambiguous under enforcement;
- no `web`/`api`/`stdin` source-name carve-out grants authority without authenticated ingress provenance;
- model-provided `session_id`, ContextVar fallback, and single-active-turn resolution cannot select another turn's principal;
- static native-tool coverage has an executable inventory test, including dynamically registered built-ins;
- external MCP classification uses preserved provenance, defaults unknown tools to admin, detects schema/provenance drift, and requires an adapter for resource scope;
- argument tests cover same-channel versus cross-channel sends, explicit/implicit targets, aliases, batches, wildcards, and unknown resources;
- commitments/files/SAGA legacy records fail closed for regular users;
- source taint blocks a later incompatible sink even when both calls would pass in isolation, including when the protected content was injected before the first tool call;
- auto-deliver final text, resend-nudge recovery/delivery, and coarse and detailed activity-panel posts/edits pass through the same egress gate; tests cover cross-principal taint to the triggering channel and detailed tool-result previews;
- enforcement cannot be enabled until the deployed trusted-autonomous capability registry is complete; enforcement-off preserves the current single-operator behavior, while generic HTTP events deny every non-open operation and prohibited-action policy remains intact;
- authenticated web chat is tested as named interactive-principal ingress with a server-derived channel, while the master API key remains transport/admin API authority rather than a chat identity;
- denials are structured and observable without logging secrets or protected result bodies.

## Follow-on Worklink leaf DAG

Each implementation issue must use the Worklink leaf template, preserve the single-operator default, and keep multi-user ingress disabled. Dependencies below are semantic ordering constraints.

1. **Frozen authorization context and carrier** — no dependencies. Add the server-created principal/service context and exact-turn carrier for normal, built-in, MCP, subagent, and detached-task paths; remove security authority from `resolve_active_ctx` fallbacks. Acceptance includes concurrent-turn, forged-session, resolver-mutation, detached-task, and missing-carrier denial tests. Likely files: `models.py`, ingress/server/bridge paths, `agent.py`, `_context.py`. Focused validation: `uv run pytest tests/test_access_control.py tests/test_agent.py tests/test_context.py -q`.
2. **Decision engine, stable operation catalog, and service-capability registry** — depends on 1. Replace allow-through name matching with open/admin/resource-scoped decisions, unknown-operation fail-closed semantics, argument adapter hooks, and explicit trusted-autonomous entries. Add an executable inventory over the final assembled native+built-in tool surface and shadow-decision logging in compatibility mode. Likely files: `access_control.py`, `tools/budget_gate.py`, tool assembly tests. Focused validation: `uv run pytest tests/test_access_control.py tests/test_budget_gate.py tests/test_tool_registry.py -q`.
3. **Channel/message resource adapter** — depends on 2. Enforce triggering-channel/bridge scope for `send_message`, `react`, and history reads; aliases resolve server-side; cross-channel, wildcard, unknown, and mixed targets require admin. Focused validation: `uv run pytest tests/test_channeltools.py tests/test_budget_gate.py tests/test_message_buffer.py -q`.
4. **Commitment ownership and adapter** — depends on 1 and 2. Add owner/recipient/service metadata, owner-inclusive dedupe, deterministic creation stamping, legacy admin-only replay, list filtering, and pre-append mutation checks. Focused validation: `uv run pytest tests/test_commitments_models.py tests/test_commitments_store.py tests/test_commitment_tools.py -q`.
5. **Filesystem/admin-read baseline** — depends on 2. Classify all file/index/search/attachment reads as admin until a principal-owned virtual-root design exists; ensure built-ins cannot escape the catalog through aliases. Focused validation: `uv run pytest tests/test_budget_gate.py tests/test_file_tools.py tests/test_file_search.py -q`.
6. **SAGA ownership, storage-scoped reads, and derived ACL propagation** — depends on 1 and 2. Add owner/channel/visibility provenance, storage-level query/get filters, service-only fallback for legacy/ambiguous/mixed derivations, and admin/service-only shared-memory writes until partitioned write semantics exist. Focused validation: `uv run pytest tests/test_saga.py tests/test_saga_tools.py tests/test_saga_session_boundaries.py -q`.
7. **MCP provenance and classification substrate** — depends on 2. Preserve the versioned provenance record on wrappers; default missing/unclassified metadata to admin; add adapter registry and canonical drift detection; reject bare resource-scoped labels. The #855 UI/API depends on this substrate but is not part of the leaf. Focused validation: `uv run pytest tests/test_mcp_client.py tests/test_budget_gate.py tests/test_config.py -q`.
8. **Information-flow labels and egress gate** — depends on 1-3 and defines extension hooks used by 5-7. Initialize labels from prompt inputs, propagate protected results/delegation/continuations, enumerate model-invoked and harness-emitted sinks, and bind the check at every final egress boundary rather than only tool middleware. Cover auto-deliver final text, resend-nudge recovery/delivery, and coarse/detailed activity-panel posts and edits; block incompatible destinations unless a distinct admin declassification action exists. Focused validation: `uv run pytest tests/test_information_flow.py tests/test_channeltools.py tests/test_resend_nudge.py tests/test_activity_panel.py tests/test_spawn_tools.py tests/test_mcp_client.py -q`.
9. **Compatibility, generic-HTTP, audit, and adversarial integration matrix** — depends on 3-8. Verify the truth table, structured redacted events, forged fields, unknown native/built-in/MCP operations, legacy records, concurrent turns, preloaded private context, same-scope success, and incompatible egress denial. Run the full suite after focused tests. Focused validation: `uv run pytest tests/test_access_control.py tests/test_server.py tests/test_budget_gate.py tests/test_information_flow.py -q && uv run pytest`.

Leaves 3-7 may proceed in parallel after their dependencies; leaf 8 should define the common label/sink interfaces early and can be implemented alongside them, but cannot close until every protected source and sink adapter is integrated. Leaf 9 is the integration gate. #854 closes only after all implementation leaves and the #855 policy-management surface (or an explicit decision to defer that UI while retaining operator configuration) are reconciled.

## Chainlink #872 integration-gate status

**Status: not ready for enforcement or multi-user ingress.** The implementation
leaves now have an adversarial integration inventory, but this gate deliberately
does not set `MIMIR_ACCESS_CONTROL_ENFORCED`, does not change its default, and
does not expose multi-user ingress.

### Configuration/default reconciliation

- `Config.from_env()` reads `MIMIR_ACCESS_CONTROL_ENFORCED` with the default
  `false`; `docs/configuration.md` documents the same default. No deployment,
  compose, example-env, or test configuration in this gate sets it to true.
- Enforcement-off remains compatibility mode for ordinary non-HTTP
  single-operator turns. Prohibited-action guards remain active.
- Generic `/event` credentials authenticate transport only. Server stamping
  marks that ingress untrusted for principal authority, so every non-open call
  is denied in both compatibility and enforcement modes unless a future
  server-owned credential-to-principal mapping is introduced.
- Unknown or missing authorization carriers fail closed under enforcement.
  Unknown native, built-in/dynamically assembled, and MCP operations are not
  implicitly open.

### Adversarial matrix and exact evidence

“Gate” below means a #872 integration test added at the final gate. “Inherited”
means an exact earlier-leaf test that remains the primary proof; the gate does
not duplicate it.

| Adversarial row | Exact pytest node(s) | Evidence |
|---|---|---|
| Enforcement-off non-HTTP compatibility | `tests/test_dispatcher.py::test_default_compat_allows_non_allowlisted_discord_user`; `tests/test_budget_gate_and_alias.py::test_admin_gate_missing_context_allows_sensitive_tool_when_not_enforced` | Inherited: bridge dispatch and a non-open built-in preserve existing behavior. |
| Generic HTTP is transport-only; forged author/source/trigger/extra cannot grant non-open authority | `tests/test_server.py::TestHandleEvent::test_event_stamps_http_ingress_as_untrusted_for_privileged_side_effects`; `tests/test_agent.py::test_run_turn_http_event_ingress_reaches_turn_context_before_admin_tool_auth`; `tests/test_access_control.py::test_http_transport_principal_mapping_absence_denies_every_non_open_call` | Gate + inherited: server overwrites forged ingress, the live turn carries it, and resource-scoped/unknown calls deny with enforcement both off and on. |
| Enforcement-on missing/unknown contexts | `tests/test_access_control.py::test_enforcement_on_missing_context_denies`; `tests/test_access_control.py::test_enforcement_on_unknown_context_denies` | Gate. |
| Structured redacted allow/deny events | `tests/test_access_control.py::test_inbound_audit_events_are_structured_and_redacted` | Gate: invokes the live dispatcher gate for allow and deny, asserts the exact schema, and proves message/extra secrets are absent. |
| Forged session ids and mutable fields | `tests/test_access_control.py::test_forged_session_id_cannot_select_concurrent_admin_turn`; `tests/test_access_control.py::test_auth_context_ignores_mutated_resolver_and_event`; `tests/test_access_control.py::test_malformed_runtime_carrier_fails_closed_under_process_enforcement` | Inherited: model selectors, post-ingress mutation, and auth-lookalikes do not become authority. |
| Concurrent turns | `tests/test_access_control.py::test_concurrent_turns_keep_authority_and_ifc_scope_isolated`; `tests/test_access_control.py::test_exact_request_carrier_resists_concurrent_principal_swap` | Gate + inherited: genuinely concurrent middleware calls keep principal and IFC carriers isolated; the regular turn cannot borrow admin execution. |
| Unknown native/built-in/dynamic operation | `tests/test_tool_registry.py::test_unknown_operation_fails_closed_only_when_enforced`; `tests/test_tool_registry.py::test_runtime_inventory_replaced_from_final_model_surface`; `tests/test_tool_registry.py::test_budget_middleware_publishes_final_runtime_inventory_per_model_call`; `tests/test_budget_gate_and_alias.py::test_middleware_catches_unregistered_tools` | Inherited: unknown operation denies under enforcement and the final model-bound surface includes native/built-in/unregistered tools. |
| Unknown MCP operation | `tests/test_access_control.py::test_unknown_mcp_tool_denies_under_enforcement`; `tests/test_mcp_client.py::TestMCPResourceAdapter::test_regular_principal_cannot_call_arbitrary_provenanced_write_tool` | Gate + inherited: an unclassified MCP name is admin-required and denied to a regular principal. |
| Unknown/forged synthetic trigger | `tests/test_tool_registry.py::test_unknown_and_http_triggers_cannot_inherit_service_capabilities`; `tests/test_access_control.py::test_http_transport_principal_mapping_absence_denies_every_non_open_call` | Inherited + gate: unknown triggers have no service principal and a forged registered trigger over HTTP gains none. |
| Legacy records | `tests/test_commitments_store.py::test_legacy_record_no_owner_is_admin_only`; `tests/test_saga_read_authorization.py::test_get_atoms_missing_context_does_not_reveal_legacy_or_private` | Inherited: ownerless commitments reject regular mutation; legacy-admin SAGA rows are filtered before rendering. |
| Preloaded private context | `tests/test_information_flow.py::test_initializes_before_first_model_call_from_ingress_and_preloaded_context`; `tests/test_information_flow.py::test_preloaded_private_context_blocked_at_incompatible_auto_delivery_without_tool_call` | Inherited integration: pre-model labels initialize monotonically and block harness egress without relying on a tool call. |
| Same-scope success | `tests/test_access_control.py::test_same_scope_private_egress_succeeds_through_live_middleware`; `tests/test_channel_resource_adapter.py::TestOperationCatalogIntegration::test_omitted_channel_send_passes_gate_as_same_scope` | Gate + inherited: the live middleware permits labeled content back to its source/trigger channel. |
| Incompatible egress denial | `tests/test_access_control.py::test_concurrent_turns_keep_authority_and_ifc_scope_isolated`; `tests/test_information_flow.py::test_preloaded_private_context_blocked_at_incompatible_auto_delivery_without_tool_call`; `tests/test_information_flow.py::test_activity_panel_post_and_detailed_edit_use_live_labels_and_fail_closed` | Gate + inherited integration: tool egress, final auto-delivery, and activity-panel edits all fail closed for incompatible labels/destinations. |

### Deferred #855 UI gate

The #855 React policy-management UI/API remains intentionally deferred. This
is not a missing runtime switch to compensate for by enabling enforcement:
operator configuration stays the only current policy-management surface, and
multi-user ingress stays disabled. The MCP provenance/classification substrate
must remain the authority source if/when #855 is implemented; UI-supplied names
alone cannot confer authority.

### Enforcement-enablement blocker: trusted service recall

Enforcement is **not ready** because trusted service-triggered turns
(`scheduled_tick`, `poller`, `saga_session_end`, and `upgrade`) currently derive
SAGA read scope from owned/public rows plus their configured
`readable_domains`. Most existing SAGA data is migrated as
`legacy_admin`/admin-owned, so those turns cannot recall it under enforcement
and would become memory-blind even though parts of their tool capability matrix
are admitted.

A broad policy allowing every trusted service to read all `service` and
`legacy_admin` memory was considered but is **not adopted here**. It may be too
broad once services are partitioned. Enforcement must remain off until a
narrow, reviewed service-recall policy and migration strategy preserve required
autonomous recall without granting unrelated services each other's memory.
Passing capability-matrix preflight does not resolve this data-plane blocker.

## Out of scope for #856

This artifact does not implement the policy, migrate schemas, add the #855 UI/API, enable access-control enforcement, or enable multi-user ingress. Those changes are follow-on leaves gated by focused tests and review.
