# Spec: scheduler.yaml unification for non-LLM cron jobs

**Filed:** 2026-05-08
**Status:** **shipped** (2026-05). Implementation in `mimir/scheduler.py` via
the `register_callable` / `callable: <name>` registry; non-LLM crons
(saga-consolidate, introspection-report, oauth-usage-poll,
bind-mount-health-probe, identities-populate, viability-report) are now
operator-mutable from `scheduler.yaml` via `callable:` entries with no
container restart needed. This doc is retained as design rationale.

**Driver:** [chainlink #44 / PR #71 follow-up]; operator request after PR #71's
"Concern 2" review surfaced the inconsistency.

## Problem

mimir has two parallel scheduling mechanisms today:

1. **LLM-tick jobs** — declared in `state/scheduler.yaml`, hot-reloaded,
   mutable via the `add_schedule` / `remove_schedule` MCP tools. Operator
   has full control without restart. Each entry is a `prompt` / `prompt_file`
   + `cron` / `time_of_day` + optional `channel_id`.

2. **Non-LLM crons** — saga-consolidate, introspection-report,
   oauth-usage-poll, bind-mount-health-probe, identities-populate. Each
   registered by code in `mimir/server.py` via a per-job `add_*_job`
   method on `Scheduler`, with cron expression read from a per-job env
   var (`MIMIR_*_CRON`). Changing any cron requires editing the env var
   and restarting.

The non-LLM side has no operator-mutable surface. To delay
saga-consolidate by 2 hours because it's firing during your heaviest
activity hour, you stop the container, edit the env, restart. The
operator can't see all scheduled jobs in one place — yaml shows the
LLM ticks; non-LLM crons live in code. Adding a 6th non-LLM cron means
yet another env var.

## Design — Option B (named-callable registry)

Each non-LLM cron registers itself at startup as a *named callable*
with binding context (SagaClient handle, channel registry, etc.)
captured in the closure. The yaml grows a new entry shape:

```yaml
- name: saga-consolidate
  cron: "0 4 * * *"
  callable: saga-consolidate
- name: identities-populate
  cron: "0 6 * * *"
  callable: identities-populate
- name: morning-review                    # existing LLM-tick shape, unchanged
  cron: "0 8 * * *"
  prompt_file: morning-review.md
```

The yaml's `callable` field is a **name lookup**, not arbitrary symbol
invocation. Only callables registered by code at startup are
referenceable. The yaml selects which callable runs on which cron;
the callable itself, with its binding context, stays code-side. This
keeps the security surface tight — operator with `state/` write access
can change schedules but cannot invoke arbitrary Python.

### Resolution semantics

- **yaml override wins.** If yaml has an entry naming a registered
  callable, the yaml's cron is used.
- **env-var default.** If yaml has no entry for a registered callable,
  the env-var default cron is used (current behavior preserved).
- **explicit disable.** A yaml entry with empty cron disables the
  callable for this deployment, regardless of the env-var default.
- **unregistered callable.** A yaml entry naming a callable that no
  code-side registration has installed: warn at load time, skip the
  entry. Doesn't crash startup — could be a stale yaml after a
  refactor removed the callable.

### Backward compatibility

- Existing yaml entries (`prompt` / `prompt_file`) keep working. The
  new `callable` field is mutually exclusive with `prompt` /
  `prompt_file` — exactly one must be set.
- Existing `add_saga_consolidate_job` / `add_introspection_report_job`
  / `add_oauth_usage_poll_job` / `add_health_probe_job` /
  `add_identities_populate_job` method signatures preserved as thin
  wrappers around `register_callable`. Existing tests don't churn.
- The env-var-default path stays the operator's "set a default cron
  in code" lever; yaml is purely an override / disable layer.

### MCP tool surface

`add_schedule` (existing) gains an optional `callable` parameter,
mutually exclusive with `prompt` / `prompt_file`. Validation: the
named callable must be registered (otherwise the yaml entry would
be dead-on-arrival). `remove_schedule` works as today — drops the
yaml entry by name. After a yaml mutation, the registered
callables get re-resolved against the new yaml so a runtime change
to saga-consolidate's cron takes effect on the next tick.

## What this is NOT

- **Not arbitrary callable invocation.** The yaml's `callable` field
  is a name lookup, not a Python import path. Adding `callable:
  os.system` would warn-and-skip (no such name registered).
- **Not new job creation from yaml.** Operator can't register a new
  non-LLM cron via yaml alone — the binding context (SagaClient,
  channel registry) lives in code. yaml selects from already-
  registered callables.
- **Not a refactor of LLM-tick dispatch.** The `prompt` / `prompt_file`
  → `_fire` → `AgentEvent` path is unchanged. Only the callable side
  is new.

## Implementation

### Pieces

1. **`SchedulerJob.callable_name`** — new optional field. `from_yaml_entry`
   validates "exactly one of `prompt`, `prompt_file`, `callable`".
   `to_yaml_entry` serializes as `callable: <name>`.
2. **`Scheduler._callables` registry** — `dict[str, _CallableDef]`
   storing `(fn, default_cron, job_id)`. `register_callable(name, fn,
   default_cron, *, job_id=None)` adds a registration and immediately
   installs the APScheduler job at the resolved effective cron (yaml
   > default; empty disables).
3. **`Scheduler.reload()`** — drops scheduler:* prompt-style jobs AND
   callable jobs, then re-installs both. Callable re-installation
   re-resolves the effective cron against the new yaml. Prompt-style
   yaml entries with `callable_name` set are skipped (they're handled
   by callable registration).
4. **Migration of 5 add_*_job methods** — each becomes a thin wrapper
   that builds the closure (existing code) and calls
   `register_callable(...)` instead of `add_job` directly. Signatures
   preserved for backward-compat with tests.
5. **MCP `add_schedule`** — `callable` parameter added. Validation:
   warn-skip if the named callable isn't registered.

### Estimated size

- `scheduler.py`: +200 LOC (registry, refactor of 5 methods, validation).
- `server.py`: ~0 LOC (wrapper signatures preserved).
- `tests/test_scheduler.py`: +150 LOC (registry, yaml override, mutual
  exclusion, MCP-tool extension).
- Spec doc: this file.

Total: ~350-400 LOC + tests on top of the existing PR #71 work.

## Migration of in-flight identities-populate

PR #71's just-added `add_identities_populate_job` is the cleanest
migration target. Rewrites to:

```python
def add_identities_populate_job(
    self, home, cron_expr, channel_registry, *, job_id="identities-populate",
):
    async def _run():
        ...  # existing closure
    return self.register_callable(
        name=job_id,
        fn=_run,
        default_cron=cron_expr,
        job_id=job_id,
    )
```

The other 4 (saga-consolidate, introspection-report,
oauth-usage-poll, bind-mount-health-probe) follow the same pattern.

## Test plan

- `SchedulerJob.from_yaml_entry` accepts `callable` field; rejects
  combinations (prompt + callable, prompt_file + callable, empty).
- `register_callable` installs job at default cron when yaml is empty.
- `register_callable` installs job at yaml override cron when yaml
  has matching entry.
- `register_callable` does not install job when effective cron is
  empty (env-var disable + no yaml override; or yaml override
  explicit empty).
- `reload()` re-resolves callable jobs against new yaml — adding a
  yaml entry post-startup updates the cron.
- `reload()` warns-and-skips yaml entries whose callable isn't
  registered.
- `add_schedule` MCP tool with `callable` param + cron lands in yaml,
  triggers reload, callable runs at the new cron.
- Existing 5 add_*_job tests still pass (backward-compat).

## Out of scope

- Running arbitrary user-supplied callables from yaml. Future feature
  if it's ever asked for; gated on a separate security review.
- Cross-callable dependencies (don't run identities-populate until
  saga-consolidate finishes). APScheduler doesn't model this; if
  ordering matters, the closures themselves can sequence work.
- Per-callable runtime parameter overrides (e.g. yaml-side
  `dry_run: true`). Closures capture their parameters at registration;
  yaml only controls cron + presence/absence.
