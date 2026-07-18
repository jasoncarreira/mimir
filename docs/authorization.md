# Authorization reference

This is the authoritative operator and contributor reference for Mimir's
requester-resource authorization system. It describes the implementation in
`mimir/access_control.py`, the frozen carrier in `mimir/models.py`, SAGA
ownership in `mimir/saga/ownership.py`, and the tool boundary in
`mimir/tools/budget_gate.py`.

Authorization is implemented but ships in compatibility (shadow) mode. Do not
enable it for a deployment until the [enablement runbook](#enablement-runbook)
has been completed. The earlier
[requester/resource policy](security/requester-resource-authorization.md) is a
design and adversarial-review artifact; where its historical status notes differ
from this document, this reference and current code control.

## Model

Mimir authorizes a concrete request, not a tool name in isolation. The effective
decision includes:

```text
(requester, ingress provenance, operation, tool provenance, arguments,
 resource ownership, information-flow labels, destination)
```

The model is not a policy enforcement point. Prompt text, tool arguments,
`session_id`, source names, trigger strings, ContextVars, and active-turn lookup
cannot confer authority.

### Human principals and tiers

Human identity and roles come from `<MIMIR_HOME>/state/identities.yaml` through
`IdentityResolver`. Access metadata belongs to the canonical identity, so all
of that person's platform aliases receive the same role snapshot.

```yaml
people:
  - canonical: alice
    aliases: [slack-U123, discord-456]
    access:
      roles: [user, admin]
```

- A regular requester has `user` or `admin`. This admits USER-tier inbound use
  and operations that are open or pass a resource adapter.
- An administrator has `admin`. This also admits ADMIN-tier actions and
  `ADMIN_REQUIRED` operations.
- Missing access metadata, an empty/malformed role list, and an unknown identity
  grant no human-user authority when enforcement is enabled.
- `service` and `is_service` identity metadata do not by themselves admit an
  external caller as a human user or as a trusted internal service.

`authorize_inbound()` and `authorize_action()` return structured
`AccessDecision` values. With enforcement off, they retain the reason that would
have denied the request but return `LEGACY_ALLOWED`; with enforcement on, the
same missing, unknown, unallowlisted, or non-admin request is denied.

### Frozen server-created `AuthContext`

`AuthContext` is a frozen dataclass in `mimir/models.py`. `create_auth_context()`
in `mimir/access_control.py` creates it before model execution and snapshots:

- raw and canonical principal;
- roles;
- server-owned ingress provenance and trigger;
- triggering channel and server-computed interactivity;
- policy version and enforcement state;
- trusted-service status;
- immutable information-flow labels.

LangGraph carries this exact object in `ToolCallRequest.runtime.context`.
`_auth_context_from_request()` in `mimir/tools/budget_gate.py` accepts only an
actual `AuthContext`, not a duck-typed object. SAGA tool handlers repeat the
exact-carrier check as defense in depth. A missing or malformed carrier grants
no non-open authority under enforcement.

Client-supplied identity is never trusted as this carrier. In particular, a
model cannot select authority with a `session_id`, and a concurrent turn cannot
borrow authority through a ContextVar or a "single active turn" heuristic.
Reloading identities after turn creation also cannot widen that turn's frozen
role snapshot.

### HTTP ingress marker

Generic `POST /event` authenticates transport, not a named requester. The server
removes any client-provided marker and stamps
`extra["_mimir_event_ingress"] = "http_event"`; it does not copy a client
`service_principal` or IFC carrier. `create_auth_context()` snapshots that value
as `event_ingress`.

Every non-`OPEN` tool operation from this generic HTTP ingress is denied by the
live tool gate even when global enforcement is off. Forging an admin author or
a registered service trigger does not widen authority. Authenticated web chat
is different: it resolves a server-owned per-user key to a canonical identity
and is not treated as generic `/event` ingress. The shared master
`MIMIR_API_KEY` is API/transport authority, not a web-chat identity.

### Trusted service principals

Autonomous scheduler, poller, synthesis, and upgrade turns do not become admins.
They use separate `ServicePrincipal` entries keyed by a server-owned trigger.
Internal event constructors set the expected `service_principal`, and
`create_auth_context()` recognizes it only when the trigger, canonical service
name, and absence of HTTP ingress all agree.

`get_trusted_service_from_auth_context()` rechecks authority at use time. Its
trust check requires all of the following:

- `event_ingress is None`;
- `is_service is True`;
- `trigger` is a registered service trigger;
- `canonical_principal` exactly matches that trigger's registered canonical
  service.

This is the service two-factor invariant: a service assertion (`is_service` and
registered trigger) is insufficient without the canonical match, and neither
is accepted from any marked ingress. A trigger string, `is_service`, an identity
role, or a canonical name alone grants nothing.

## Operation decisions

`OperationCatalog` in `mimir/access_control.py` produces an
`OperationDecision` for each tool call. The catalog contains explicit built-in
sets and adapter hooks; the final model-bound tool inventory in `ToolRegistry`
is observational and does not grant authority.

Resolution order is:

1. An exact custom registration from `register_operation()`.
2. Adapter hooks in registration order (currently channel, then MCP).
3. The exact built-in `OPEN` set.
4. The exact built-in `ADMIN_REQUIRED` set.
5. Protected built-in aliases.
6. Namespaced/suffixed forms of an admin-required operation.
7. `UNKNOWN`.

The current operation lists live in `OperationCatalog._OPEN_OPERATIONS`,
`_ADMIN_REQUIRED_OPERATIONS`, and `_ADMIN_BUILTIN_TOOL_NAMES`. Refer to those
sets rather than duplicating them in deployment policy: they are the executable
catalog. Protected metadata operations are also pinned by tests so that global
channel, schedule, and shell-job metadata cannot become open accidentally.

### Decision tiers

| Decision | Meaning | Enforcement off | Enforcement on |
|---|---|---|---|
| `OPEN` | The general tool middleware imposes no admin or resource-adapter gate. Some open operations perform mandatory ownership checks inside their handler or storage query. | Allowed by general middleware; leaf checks still apply. | Allowed by general middleware; leaf checks still apply. |
| `ADMIN_REQUIRED` | Requires an `admin` role or an exact trusted-service capability with all mapped domain/sink constraints. | Ordinary failures are allowed and emitted as `shadow_tool_decision`; a valid service grant is also marked shadow. Generic HTTP remains denied. | Denied without admin or a valid service grant. |
| `RESOURCE_SCOPED` | Arguments identify resources and a concrete adapter must authorize them. | Adapter failures are shadow-allowed; generic resource-scoped calls without an evaluator are shadow-allowed. Generic HTTP remains denied. | The channel adapter enforces same-scope access. Any other resource-scoped call without a concrete evaluator is denied. |
| `UNKNOWN` | No catalog or adapter classification exists. It is never implicitly open. | Shadow-allowed for ordinary non-HTTP callers. | Denied. An exact trusted-service capability is the narrow exception. |

`BudgetGateMiddleware` enables shadow logging. Tool decisions include stable
fields such as operation, tier, reason, service principal, enforcement state,
and whether the result was a shadow decision. Prohibited-action and budget
guards are separate controls and remain active when authorization enforcement
is off.

## Trusted-service capability matrix

The matrix is `_TRUSTED_SERVICE_PRINCIPALS` in `mimir/access_control.py`. Each
entry has exact operation grants (`capabilities`), a read-domain allowlist
(`readable_domains`), an active-sink allowlist (`sink_destinations`), trigger,
and a documented server creation path. Runtime tool registration cannot add a
service capability.

Capabilities grant a trusted service access to matching `ADMIN_REQUIRED`
operations and provide the narrow service exception for `UNKNOWN`; they are not
a complete deny-by-default inventory for `OPEN` or resource-scoped operations.
Open calls still follow their handler/storage checks, and resource-scoped calls
follow their adapter. Service sink bypass remains separately constrained by
declared destinations.

The matrix currently contains:

| Principal | Trigger | Declared creation path | Capabilities |
|---|---|---|---|
| `scheduler` | `scheduled_tick` | `mimir.scheduler.Scheduler._fire_job` | `shell_exec`, `bash_async`, shell-job reads, all three spawn operations, `task`, `saga_forget`, file read/search/write/edit operations, proposal operations, `worklink_run`, and turn reads |
| `poller` | `poller` | `mimir.pollers.run_poller` | `shell_exec`, `bash_async`, all three spawn operations, `task`, file read/search/write/edit operations, proposal operations, `worklink_run`, turn reads, `send_message`, and `list_channels` |
| `synthesis` | `saga_session_end` | `mimir.server._on_session_idle` | `saga_end_session`, `saga_mark_contributions`, `saga_feedback`, `saga_record_skill_learning`, `memory_get`, `memory_store`, turn reads, and file read/search/write/edit operations |
| `system` | `upgrade` | `mimir.defaults_upgrade.enqueue_upgrade_prompt_turns` | `shell_exec`, `bash_async`, file read/search/write/edit operations, proposal operations, `add_schedule`, `set_schedule_priority`, `list_schedules`, and `send_message` |

The file operation groups above mean the exact synchronous/asynchronous names
present in the matrix (`read_file`/`aread`, `ls`/`als`, `glob`/`aglob`, and
`grep`/`agrep`); `file_search` is included for scheduler and poller. "Turn
reads" means `get_turn` and `mimir_get_turn`. The exact tuple remains the source
of truth when changing the matrix.

The scheduler entry currently declares `Scheduler._fire_job`; the live producer
is `Scheduler._fire` in `mimir/scheduler.py`. The declared string is audit
metadata rather than an executable binding; service trust is established by the
trigger/canonical/ingress checks described above.

| Principal | `readable_domains` | `sink_destinations` |
|---|---|---|
| `scheduler` | `configured_inputs`, `filesystem`, `turn_history`, `shell_jobs` | `configured_channel`, `filesystem`, `shell_process`, `spawn_process`, `proposal`, `saga`, `worklink` |
| `poller` | `poller_payload`, `filesystem`, `turn_history`, `channel_metadata` | `configured_channel`, `filesystem`, `shell_process`, `spawn_process`, `proposal`, `worklink`, `message` |
| `synthesis` | `session`, `saga`, `filesystem`, `turn_history` | `session_boundary`, `saga`, `filesystem` |
| `system` | `defaults`, `proposal`, `filesystem`, `schedule_metadata` | `operator_alert`, `filesystem`, `shell_process`, `proposal`, `scheduler`, `message` |

`_OPERATION_READABLE_DOMAIN` and `_OPERATION_SINK_DESTINATION` map operations
to additional constraints. `service_can_invoke_operation()` requires the exact
capability and every mapped domain and sink. This function is authoritative for
service execution of admin-gated operations and for the trusted-service
exception to `UNKNOWN`. `can_write_saga()` separately requires a canonical SAGA
mutation and then either an admin or the same exact service capability check;
SAGA handlers call it again, so bypassing general middleware does not bypass
memory-integrity authorization.

### Enable-time completeness gate

`_capability_matrix_errors()` verifies that:

- all required service triggers exist and each entry declares its own trigger;
- every required entry has nonempty capabilities, readable domains, and sink
  destinations;
- every capability with a mapped read domain or sink declares it;
- every canonical SAGA mutation has a sink mapping, is not `OPEN`, and is
  explicitly `ADMIN_REQUIRED`.

`assert_capability_matrix_complete()` raises `CapabilityMatrixError` when any
check fails. `resolve_access_control_enforcement()` runs this assertion whenever
enforcement is requested, so an incomplete matrix blocks startup.
`check_capability_matrix_complete()` and `get_capability_matrix_report()` are
available for preflight and audit.

This gate is deliberately narrower than a full operation inventory: it does not
prove that every runtime tool or every service capability is cataloged. Unknown
operations still fail closed under enforcement, but they run in shadow mode and
an exact service capability can authorize one. Contributors must therefore add
catalog and inventory tests as described in [How to extend](#how-to-extend), not
treat a passing matrix assertion as complete operation coverage.

## Resource adapters

An `OperationDecision.RESOURCE_SCOPED` label is not itself an authorization
predicate. A concrete adapter must resolve model arguments to server-known
resources and reject missing, ambiguous, wildcard, mixed-owner, or unknown
resources. Without a runtime evaluator, resource-scoped operations deny under
enforcement.

### Channels

`ChannelResourceAdapter` classifies `send_message`, `react`, and
`fetch_channel_history` as resource-scoped. It compares the target with the
server-carried triggering channel after server-side channel alias resolution.
An omitted target means the triggering channel, matching tool runtime behavior.

- Same canonical channel is allowed for a regular requester.
- A different or unknown channel requires admin.
- A missing triggering channel fails closed under enforcement.
- Channel names and targets supplied by the model do not replace the triggering
  channel in `AuthContext`.

IFC is still evaluated separately. Admin authorization for a cross-channel
operation does not erase taint or make the destination IFC-compatible.

### MCP

`MCPResourceAdapter` is a provenance-aware classification extension point for
`mcp_` tools. It uses in-process `MCPProvenance`, not the display name alone, to
carry a generated server-config id, transport and endpoint label, non-secret
config digest, schema digest, original name, adapter name/version, approval
version, and policy version.

Missing or tombstoned provenance, an absent adapter, version/policy mismatch,
adapter failure, or an invalid adapter result becomes `ADMIN_REQUIRED`. A
matching registered classifier may return `OPEN`, `RESOURCE_SCOPED`, or
`ADMIN_REQUIRED`, after which production role and service evaluation occurs
through `ToolRegistry.authorize_tool()`.

The current production assembly does not register a classifier or run the
provided drift/stale-policy helpers, so configured MCP tools currently resolve
to `ADMIN_REQUIRED`. The classifier call receives the tool name and authorization
context, not model arguments; therefore the current substrate cannot safely
authorize an argument-dependent `RESOURCE_SCOPED` MCP call. Adapter registration,
argument-aware runtime evaluation, durable approval identity, and automatic
drift tombstoning/startup checks are extension obligations, not currently wired
production behavior. The lower-level `authorize_mcp_tool()` helper is also not a
substitute for the full registry path.

## SAGA ownership

`mimir/saga/ownership.py` defines row-level authorization for atoms, sessions,
observations, triples, and world state. Ownership is independent of tool-tier
authorization and IFC.

`Ownership` records:

- `owner_principal`: canonical user or `service:<canonical>` owner;
- `origin_channel`: source channel/session;
- `origin_domain`: resource namespace;
- `visibility`: `public`, `private`, `service`, or `legacy_admin`;
- provenance metadata.

Unproven and migrated data defaults to owner/visibility `legacy_admin`.
Derived ACLs use `intersect_acl()`: mixed owners/domains, missing provenance,
unknown visibility, or any legacy ambiguity collapses to the fail-closed
`legacy_admin` ACL.

`get_authorization_scope()` accepts only an actual `AuthContext`; a caller-made
`AuthorizationScope` is a query value, not authority. `authorization_predicate()`
and its session/triple variants produce parameterized SQL predicates so content
and existence are filtered before ranking or rendering:

- missing context reads only `public` rows;
- regular users read public rows and rows they own;
- admins read all rows;
- regular services read public, service-owned, and declared-domain rows;
- the four trusted platform/maintenance services read the internal corpus
  without becoming admins.

Broad platform-service reads are intentional and are contained by derived ACL
intersection and IFC on outputs. They do not widen SAGA mutation authority:
destructive mutation scope is independently limited by admin status, exact
service owner/domain scope, and `can_write_saga()`.

## Information flow and sinks

Row/read authorization answers whether data may enter a turn. IFC answers where
data accumulated by that turn may leave.

`InformationFlowLabels` in `mimir/models.py` carries monotonic sensitivity
labels (`private`, `confidential`, `internal`, `public`) and source channels.
Labels are initialized before the first model call from inbound/folded messages,
history, attachments, continuations, and preloaded server context. Automatic
memory, session, skill, file, and other prompt injection is conservatively
tainted `private`. Labels propagate into delegated/forked work, continuations,
and resumed turns. Summarizing or transforming content cannot remove them;
`audit_declassification()` is the only removal path and requires an admin.

`SinkCategory` and `_SINK_CATEGORY_MAP` classify channel egress, MCP, HTTP,
network, shell, spawn, notification, and file destinations. `SinkGate.check_sink_flow()`:

- fails closed on a missing label carrier, unknown category, unknown label, or
  missing destination when enforcement is active;
- permits unlabeled data only for a known category and nonempty destination;
- permits ordinary labeled same-channel flow only when every source channel
  resolves to the triggering channel;
- does not let an ordinary admin bypass IFC;
- permits a trusted service only for compatible declared active-sink
  destinations; poller payloads cannot use that bypass for shell, spawn, or file
  sinks.

The sink check runs before a model-invoked egress tool. Harness-owned final-text
delivery and activity-panel post/edit paths invoke an enforced sink check at the
final boundary even while general authorization is in shadow mode. SAGA
ownership does not currently generate field-level IFC labels; after authorized
recall, injected prompt context receives the conservative turn-level taint.

## Configuration

The exhaustive environment-variable contract is
[`docs/configuration.md`](configuration.md). The settings below are the subset
that directly configures authorization, identity admission, or an authorization
boundary.

### Enforcement and identity

| Setting | Default | Authorization effect |
|---|---|---|
| `MIMIR_ACCESS_CONTROL_ENFORCED` | `false` | Primary switch. False is compatibility/shadow mode; true enforces inbound identity, operation, resource, and IFC decisions. Generic HTTP non-open denial and harness egress checks remain fail-closed when false. Boolean parsing accepts `1/true/yes/on/y` and `0/false/no/off/n`; unset, empty, or invalid values resolve to the safe shipped default (`false`, with a warning for invalid input). |
| `MIMIR_MODEL_SPEC` | `claude-code:claude-sonnet-4-6` | Selects the provider and participates in the enforcement compatibility preflight. The default provider is incompatible with enforcement, so enabling authorization also requires changing this setting. |
| `<MIMIR_HOME>/state/identities.yaml` | generated with no people | Canonical aliases and human roles. `user` admits normal inbound use; `admin` also admits admin-required operations. This is a policy file, not an environment variable. |
| `MIMIR_CROSS_PLATFORM_PULL` | `true` | Controls cross-platform recent-context pull. It does **not** isolate authorization roles: aliases still resolve to one canonical identity and role snapshot when false. |

Pairing can add the required identity role with
`mimir identities approve-pairing <identity>`; add `--admin` for both `user` and
`admin`. The identities populator may add aliases and metadata but preserves
operator-managed access fields.

### Denied-user handling

| Setting | Default | Authorization effect |
|---|---|---|
| `MIMIR_UNAUTHORIZED_USER_BEHAVIOR` | `ignore` | Controls the additional `inbound_pairing_prompted` event for an enforced public/shared-channel denial. Every enforced denial may still be recorded as a pending pairing and notify the operator; denied turns are never enqueued. No public reply is sent by this setting. |
| `MIMIR_PAIRING_PENDING_MAX` | `100` | Caps newly recorded pending identities. `0` rejects new pending identities; a negative value disables the cap. |
| `MIMIR_PAIRING_OPERATOR_DIGEST_DELAY_SECONDS` | `1.0` | Coalesces operator pairing notifications; clamped to zero or greater. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_ENABLED` | `false` | Enables a fixed best-effort DM response to a denied user; it does not grant access. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_INTERVAL_SECONDS` | `30.0` | Global DM response interval, clamped to zero or greater. |
| `MIMIR_PAIRING_DM_AUTO_REPLY_TEXT` | `Request forwarded to operator; no access until approved.` | Verbatim denial response text. |
| `MIMIR_OPERATOR_ALERT_CHANNEL` | empty | Destination for pairing digests/cap alerts and other operator alerts. Empty leaves pairing recorded without an operator message. |
| `MIMIR_IDENTITIES_POPULATE_CRON` | empty | Enables identity alias/metadata discovery. It does not grant roles. |

### HTTP identity and transport

| Setting | Default | Authorization effect |
|---|---|---|
| `MIMIR_API_KEY` | empty | Master API transport and route-admin key. It is not a named chat principal and cannot make generic `/event` non-open calls authoritative. Per-user web keys are hashed aliases in `identities.yaml` and use that identity's roles. |
| `MIMIR_WEB_HOST` | `127.0.0.1` | A non-loopback bind requires `MIMIR_API_KEY` or startup refuses. |
| `MIMIR_ALLOW_UNAUTHENTICATED` | `false` | Suppresses the empty-key localhost warning only. It does not disable authorization or permit a keyless non-loopback bind. |

### Protected resource and sink surface

These settings do not grant a requester role. They change which resources or
egress paths exist and therefore must be reviewed with the catalog/adapters:

- `MIMIR_FOLDERS`, `MIMIR_FILE_TOOL_ROOTS`, and `MIMIR_FILE_OP_ROOTS` constrain
  filesystem reachability; they do not replace operation authorization.
- `MIMIR_MCP_SERVERS_JSON` and `MIMIR_MCP_SERVERS_PATH` add external tools.
  Missing or unapproved provenance remains admin-required.
- `MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS`, `MIMIR_RESEND_NUDGE_CHANNELS`,
  `MIMIR_ACTIVITY_PANEL_CHANNELS`, and `MIMIR_ACTIVITY_PANEL_DETAIL` enable
  harness egress that remains subject to the final IFC sink gate.
- `MIMIR_MIDTURN_INJECTION_CHANNELS` and `MIMIR_ATTACHMENTS_MAX_BYTES` shape
  inbound data that contributes to the turn's labels; they do not establish
  requester authority.
- `MIMIR_CHAT_SKILLS_ENABLED` and `MIMIR_CHAT_SKILL_ALLOWLIST` change the
  chat-visible operation surface; they do not bypass the operation catalog.

### Code-owned policy

There is no environment override for the operation catalog, service capability
matrix, resource adapters, SAGA ownership predicates, or sink map. They live in:

- `mimir/access_control.py`: catalog, adapters, matrix, domain/sink mappings,
  `SinkGate`, and enable-time assertions;
- `mimir/mcp_client.py`: MCP provenance and adapter registrations;
- `mimir/saga/ownership.py`: row ownership, scopes, SQL predicates, and ACL
  intersection;
- `mimir/models.py`: frozen `AuthContext` and IFC carriers;
- `mimir/tools/budget_gate.py`: exact runtime-carrier extraction and live tool
  middleware.

### Provider compatibility gate

`resolve_access_control_enforcement()` raises
`ProviderEnforcementCompatibilityError` when enforcement is requested with a
normalized `claude-code` provider. The Claude Code SDK subprocess hook cannot
receive Mimir's exact server-created per-turn `AuthContext`; startup rejects the
combination rather than running a partially usable agent whose non-open tools
all fail for a missing carrier.

Use `anthropic:`, `openai:`, or `codex-plus:` with enforcement. Provider names
are normalized to lowercase with `_` changed to `-`, so both `claude-code:` and
`claude_code:` are rejected. The preflight is an explicit rejection of Claude
Code, not a general provider allowlist; normal model resolution separately
rejects invalid/unsupported provider specs.

## How to extend

### Add an operation

1. Identify the exact stable runtime tool name and all aliases/namespaced forms.
2. Add it to exactly one executable policy path: the built-in `OPEN` or
   `ADMIN_REQUIRED` set, or an adapter hook that can return
   `RESOURCE_SCOPED`. Use `register_operation()` only for deliberately dynamic
   policy with equivalent startup/tests.
3. Treat `OPEN` as a security claim about the general middleware. If the
   operation accepts a protected selector or mutation, put an exact-carrier and
   ownership predicate in the handler/storage path and test it independently.
   Never use `OPEN` for an operation whose safety depends only on model guidance.
4. If it reads a protected domain or writes an active destination for services,
   add the operation to `_OPERATION_READABLE_DOMAIN` and/or
   `_OPERATION_SINK_DESTINATION`.
5. If it is an egress path, add an exact/prefix `SinkCategory` mapping and pass
   the concrete destination to `SinkGate`. Harness-owned egress must check at
   its final send/edit boundary, not only model middleware.
6. Add catalog tests for the exact name and aliases, enforcement-on deny/allow,
   enforcement-off shadow behavior, missing `AuthContext`, generic HTTP ingress,
   and any resource arguments. Assemble the relevant final model tool surface in
   a test and assert the new name and aliases resolve to the intended non-UNKNOWN
   tier; there is no repository-wide inventory-to-catalog assertion today.
7. Run `_capability_matrix_errors()` tests and the full suite. For a SAGA
   mutation, also add it to `_SAGA_MUTATION_OPERATIONS`, keep it explicitly
   `ADMIN_REQUIRED`, add a sink mapping, call `can_write_saga()` inside the
   handler, and test owner/domain mutation scope.

Do not rely on `UNKNOWN` as the intended classification. It fails closed only
after enforcement is enabled and is shadow-allowed before then. A test asserting
`get_operation_catalog().get_decision(name)` is the intended non-`UNKNOWN` tier
is the completeness obligation that prevents a newly shipped operation from
remaining uncataloged silently.

### Add a trusted service or capability

1. Add a distinct `ServicePrincipal` keyed by a stable server-owned trigger,
   with canonical name, exact operation capabilities, readable domains, sink
   destinations, and creation path.
2. Have only the internal producer set `AgentEvent.service_principal` to the
   canonical value. Never infer service authority from a trigger or missing
   author alone.
3. If the service is mandatory for an enforcement-enabled deployment, add its
   trigger to `_REQUIRED_SERVICE_PRINCIPALS`. If it is a platform maintenance
   service that requires broad SAGA recall, review and update
   `PLATFORM_SERVICE_TRIGGERS` in `mimir/saga/ownership.py` explicitly.
4. For each capability, add/read the domain and sink mappings that reflect its
   data flow, then grant those exact values in the service entry. Do not add a
   capability merely to exploit the service exception for `UNKNOWN`; catalog
   the operation and assert the intended decision.
5. For SAGA writes, update the canonical mutation set and preserve both the
   tool-handler `can_write_saga()` check and row owner/domain scope.
6. Test wrong canonical identity, wrong/unregistered trigger, `is_service=False`,
   any non-`None` ingress, missing domain/sink, forbidden capabilities, IFC, and
   concurrent turns. Assert `assert_capability_matrix_complete()` passes.

### Add a resource adapter

1. Define the resource identity and ownership/ACL predicate using canonical,
   server-resolved data. Document which arguments are selectors and which
   server fields establish authority.
2. Implement a classification hook that returns `None` for unrelated tools and
   a deliberate `OperationDecision` for its own calls. Register it in a stable
   order on the global catalog.
3. Implement runtime argument authorization. A bare `RESOURCE_SCOPED` result or
   stored `ResourceScope` metadata is not generically evaluated by
   `ToolRegistry`; wire the evaluator into the live authorization path as the
   channel adapter is wired today.
4. Fail closed on absent adapters, parse failures, missing/unknown resources,
   wildcards, aliases that do not resolve, and mixed-resource batches. Preserve
   stable provenance for external systems.
5. Add same-scope, cross-scope, omitted/explicit target, alias, unknown,
   wildcard/batch, admin, service, HTTP, shadow, enforcement, and IFC tests.

For MCP, also version the adapter and policy, bind it to durable provenance,
thread concrete model arguments to the runtime evaluator, and wire and test
schema/name/config drift, tombstoning, and startup stale-policy reporting. The
current MCP classifier API alone is not sufficient for argument-dependent
resource authorization.

## Enablement runbook

Enforcement remains default-off and review-gated. A passing startup preflight is
necessary but not sufficient for production enablement.

### 1. Establish readiness

- Complete the planned adversarial-review/hardening rounds, including the
  current #912-#920 series, until repeated reviews produce diminishing findings
  and no unresolved HIGH or MEDIUM authorization findings.
- Run the full suite on the exact revision to deploy. Review any local tools,
  MCP adapters, bridges, harness egress, service triggers, and persisted SAGA or
  commitment migrations that are not represented by upstream tests.
- Confirm `state/identities.yaml` has deliberate canonical aliases and `user` or
  `admin` roles for every expected human requester. Back it up and inspect
  malformed/duplicate identities.
- Audit `get_capability_matrix_report()` against every deployed scheduler,
  poller, synthesis, and upgrade workflow. Confirm required operations, domains,
  sinks, and internal creation paths, not just that the tuples are nonempty.
- Confirm protected legacy data and derived ACL behavior are acceptable. Current
  trusted platform services intentionally retain broad internal SAGA read scope;
  verify their outputs remain owner/ACL-intersected and IFC-gated.

### 2. Validate in shadow first

Keep `MIMIR_ACCESS_CONTROL_ENFORCED=false` through representative interactive,
scheduled, poller, synthesis, upgrade, web-chat, and generic `/event` traffic.
Inspect structured inbound decisions and `shadow_tool_decision` events for:

- legitimate users resolving as unknown/unallowlisted or to the wrong canonical
  identity;
- expected tools classified `UNKNOWN`, unexpectedly admin-required, or
  resource-scoped without an evaluator;
- missing/malformed `AuthContext` on normal, MCP, delegated, or forked paths;
- trusted services failing canonical/trigger/ingress checks or lacking a
  capability, readable domain, or sink;
- same-channel requests resolving cross-channel;
- IFC blocks caused by unknown sinks, missing targets, mixed source channels, or
  protected prompt/tool data reaching an active sink.

Generic HTTP non-open denial and final harness egress checks are already hard
boundaries in this phase. Do not interpret their denials as evidence that global
enforcement is enabled.

### 3. Pass startup preflights

Change `MIMIR_MODEL_SPEC` from the default `claude-code:` provider to a tested
`anthropic:`, `openai:`, or `codex-plus:` model. In a staging process with the
same environment and policy files, set:

```dotenv
MIMIR_ACCESS_CONTROL_ENFORCED=true
MIMIR_MODEL_SPEC=anthropic:<tested-model>
```

Startup must complete without `ProviderEnforcementCompatibilityError` or
`CapabilityMatrixError`. Do not bypass either check. Exercise at least one
allowed and one denied request for each human tier, resource adapter, service
principal, and configured external tool.

### 4. Enable and monitor

Roll out with a fast rollback path to `MIMIR_ACCESS_CONTROL_ENFORCED=false`.
Watch denial reasons and workflow outcomes, especially:

- inbound `missing_author`, `unknown_author`, `user_not_allowlisted`, and
  `admin_required` decisions;
- tool `missing_auth_context`, `unknown_operation`, `admin_required`,
  `resource_scoped`, `cross_channel_scope`, and HTTP-ingress denials;
- MCP missing/tombstoned/unclassified provenance;
- capability-matrix startup failures and service capability/domain/sink denials;
- SAGA rows unexpectedly filtered or mutation scope rejecting required work;
- `missing_ifc_labels`, `unknown_sink_category`, `unknown_sink_destination`, and
  `ifc_label_blocked:*` outcomes;
- scheduled/poller/synthesis/upgrade jobs that run but lose required reads,
  writes, or egress.

Do not fix an operational denial by making a broad operation `OPEN`, assigning
`admin` to an external service, accepting a client identity field, or adding a
blanket service capability. Correct the identity mapping, catalog entry,
resource predicate, carrier propagation, or narrow capability and rerun shadow
and adversarial tests.

## Verification map

The primary executable specifications are:

- `tests/test_access_control.py`: ingress, frozen carrier, HTTP trust boundary,
  concurrency, and audits;
- `tests/test_tool_registry.py`: catalog, service matrix, startup/provider gate,
  shadow behavior, and service trust checks;
- `tests/test_channel_resource_adapter.py`: argument-aware channel scope;
- `tests/test_mcp_client.py`: MCP provenance, adapters, and drift;
- `tests/test_saga_read_authorization.py`,
  `tests/test_saga_mutation_authorization.py`, and
  `tests/test_saga_write_provenance.py`: ownership and SAGA defense in depth;
- `tests/test_information_flow.py`: source labels, propagation, declassification,
  and model/harness sink gates;
- `tests/test_config_env_bool.py` and `tests/test_config_docs_complete.py`:
  enforcement parsing/preflight and the exhaustive configuration-doc contract.
