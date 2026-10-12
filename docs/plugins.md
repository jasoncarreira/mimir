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
change core tool policy or remove core memberships. No per-tool grant is needed:
profile eligibility remains subject to the existing authorization checks.

Startup preflights fail closed by plugin name; per-turn preflights run before
prompt and tools and also fail closed. Shutdown hooks run in order, isolated
from one another, before the clean-shutdown marker. Health contributions are
namespaced and cannot take down `/health`. Coding remains in core in epic 1;
other contribution points and moving coding to Cultivate are later epics.
