# Plugins (epic 1)

Mimir plugins are **trusted, in-process Python code**. Install a provider
distribution locally or from a private package index; there is no marketplace,
remote fetch, or sandbox. Only operators should install and enable providers.
Enabling a provider grants it execution at startup, not an exemption from
Mimir's tool authorization, resource scope, sink, or information-flow gates.

## Discovery and enablement

Providers advertise an entry point in the `mimir.plugins` group. The owning
distribution must contain exactly one `mimir-plugin.toml`, readable from package
metadata without importing provider code. Its entry-point name must match the
manifest `name` and match `[a-z][a-z0-9-]*`. Required TOML string keys are
`name`, `version` (PEP 440), `license` (SPDX expression), and `requires_mimir`
(PEP 440 specifier for the installed Mimir version). The reserved keys
`dockerfile`, `themes`, `frontend`, `defaults`, and `config` are ignored in
epic 1; other unknown top-level keys are rejected.

Export `MIMIR_PLUGINS=one,two` in the operator-owned **process environment**,
or `MIMIR_PLUGINS=all` to select every installed provider. Unset or empty
selects none; core tools are unaffected. Do not put this setting in the agent
home's `.env`: that file is agent-writable and cannot grant plugin execution.
Selection is fixed for the process lifetime; restart to change it. Disabled
providers' invalid or duplicate manifests are diagnostics only, while selected
missing, invalid, duplicate, or version-incompatible providers reject startup
by name. Disabled provider modules are not imported.

Use `mimir plugins list` for a human-readable inventory, or
`mimir plugins list --json` for an array of objects with `name`, `version`,
`license`, `distribution`, `enabled`, and `diagnostic` fields. Absent manifest
values are `null` in JSON; diagnostics describe invalid/duplicate manifests.
Listing reads only installed package metadata and process selection: it does
not import providers, load agent-home configuration, or start the runtime.

## Provider contract

Selected providers expose `register(registrar)` through their entry point.
They register at startup before the policy registry freezes; subsequent writes
are rejected. Each tool declares its flow direction, trigger tier, operation
class (`open`, `resource_scoped`, or `admin_required`), protected result domain
and origin, sink category and destination/target/payload extractors,
`writes_code_or_state`, authority-profile memberships, and read-backend result
membership. Resource-scoped plugin tools declare a channel or repo/PR scope;
repo/PR tools also need an existing typed action. Missing metadata fails boot.
Plugins can add their own tools to existing profiles or define new profiles,
capabilities, shell mappings and trusted service principals; they cannot
change core tool policy or remove core memberships. New plugin profiles and
static plugin principals can grant only that provider's own registered tools.
A plugin principal's canonical name and trigger must each use
`plugin:<owner>:<local>` (a nonempty local component without colons), and its
authority profile must belong to the same provider. Sink policies and capability
tier are derived from tool declarations: principals cannot supply raw
`sink_policies`, filesystem roots, tier overrides, or privileged core flags.
No per-tool grant is needed: profile eligibility remains subject to the existing
authorization checks.

## Lifecycle hooks

The object returned by the entry point may expose these optional **attributes**
alongside `register(registrar)`; there is no separate lifecycle registration API:

| Attribute / signature | Contract |
| --- | --- |
| `startup_preflight(bundle)` | Receives the published agent runtime bundle before bridges and background startup work begin. A raised exception or the literal result `False` rejects startup by plugin name and triggers compensating teardown. |
| `turn_preflight(event, auth_context)` | Receives the inbound `AgentEvent` and server-prepared authorization context before approval handling, prompt assembly, or the model/tool runner. A raised exception or the literal result `False` rejects the turn by plugin name. |
| `shutdown()` | Runs in enabled-provider order before the runtime bundle closes and the clean-shutdown marker is written. Called at most once, including startup compensation; failures are logged and do not skip later providers or core teardown. |
| `health()` | Returns a `dict` of identifier-style string field names to finite JSON-serializable numeric (`int`, `float`) or boolean values. Contributions appear under `/health`'s `plugins[plugin_name]`. Strings, nested objects, non-finite numbers, and other invalid results are rejected. |

Hooks may be synchronous or asynchronous; returned awaitables are awaited.
Preflights have no manager-level timeout. Only the literal `False` rejects a
preflight result (`None` is a normal successful return). Shutdown and health
calls have a **5-second timeout** per invocation/await: synchronous calls run
in a worker thread, and an awaitable returned by a synchronous call gets its
own 5-second bound. Timing out a worker does not forcibly stop its thread.
Health hook lookup, execution, timeout, or validation failures produce only
`{"error": "health contributor failed"}` in that provider's namespace, without
exposing exception details or taking down `/health`.

Coding remains in core in epic 1; other contribution points and moving coding
to Cultivate are later epics.
