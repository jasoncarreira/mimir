# Plugins

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
(PEP 440 specifier for the installed Mimir version). The optional keys
`dockerfile`, `themes`, `frontend`, `defaults`, `optional`, and `config` describe the
contributions below; other unknown top-level keys are rejected.

Export `MIMIR_PLUGINS=one,two` in the operator-owned **process environment**,
or `MIMIR_PLUGINS=all` to select every installed provider. Unset or empty
selects none; core tools are unaffected. Do not put this setting in the agent
home's `.env`: that file is agent-writable and cannot grant plugin execution.
Selection is fixed for the process lifetime; restart to change it. Disabled
providers' invalid or duplicate manifests are diagnostics only, while selected
missing, invalid, duplicate, or version-incompatible providers reject startup
by name. Disabled provider modules are not imported.

### Cultivate (optional coding provider)

Coding is supplied by the separately packaged `mimir-cultivate` distribution
(`BUSL-1.1`), not by an extra on the MIT `mimir-agent` distribution. It is
**not published to a package index**: it is installed from the in-repo
`plugins/cultivate` workspace member at the same commit as core. From a source
checkout, `uv sync --all-packages` (plus your usual `--extra` flags) installs
both; a plain `uv sync` or `uv sync --extra …` removes it again. Images build it
from the pinned source commit when `MIMIR_PLUGIN_SELECTION=cultivate` (see
[Docker image assembly](#docker-image-assembly-and-services)). Then set
`MIMIR_PLUGINS=cultivate` in the operator-owned process environment and restart.
Installing without selecting does not activate or import the provider.
Selecting without installing it fails startup. The retired `MIMIR_CODING_ENABLED`
flag cannot enable coding. Core alone keeps the chainlink tracker, ACP, hands,
proposal PR lane and generic shell.

#### Upgrading a coding deployment

Before this split, coding was core functionality behind `MIMIR_CODING_ENABLED`.
Upgrading such a deployment without the steps below boots a core-only agent:
it keeps running, but Worklink, the GitHub and CI pollers, the Forge/PR tools and
OpenCode are gone. Boot logs a warning when that happens, for each of
`MIMIR_CODING_ENABLED` and a home `worklink.yaml` or `repositories.yaml` that no
selected plugin owns. Startup does not fail.

1. **Install Cultivate from the same source as core.**
   - Source checkout (systemd, `uv run`, or the workspace scaffold):
     `uv sync --all-packages --extra <your extras>`. Keep `--all-packages` on
     every later `uv sync`, including any wrapper script. The workspace-mode
     `start.sh` that `mimir scaffold-docker` generates does this when Cultivate is
     selected at scaffold time.
   - Root `Dockerfile`: rebuild with
     `--build-arg MIMIR_PLUGIN_SELECTION=cultivate` (and
     `--build-arg MIMIR_ENABLE_OPENCODE=1` for the OpenCode CLI the coding tools
     require). Do not list `mimir-cultivate` in `MIMIR_PLUGIN_PACKAGES`; the
     build refuses it.
   - PyPI-mode scaffold: re-run `mimir scaffold-docker` with
     `MIMIR_PLUGINS=cultivate` exported, then rebuild. The image installs core
     and Cultivate from `MIMIR_CONTROLLER_COMMIT`.
2. **Select it in the process environment.** Set `MIMIR_PLUGINS=cultivate`
   where the supervisor starts Mimir: shell export, systemd `Environment=`, or
   compose `environment:`/`env_file`. The agent home's `.env` cannot select
   plugins; a value there is ignored with a warning.
3. **Remove `MIMIR_CODING_ENABLED`** from the home `.env` and the deployment.
   It is ignored.
4. **Restart and check.** `mimir plugins list` shows `cultivate` enabled, and
   boot logs no `CODING IS DISABLED` or `no selected plugin owns it` warning.
   Selected coding also needs the OpenCode CLI on `PATH` and an absolute
   `MIMIR_PR_CHECKOUT_LEASE_ROOT`; startup names any that are missing.

Your `worklink.yaml`, `repositories.yaml` and existing Worklink state are read
unchanged once Cultivate is selected.

Cultivate supplies Worklink, Forge/repository/CI tools, OpenCode execution,
coding defaults, the Factory Runs and PR-board pages and the `squashmerge`
default skin. The Coding status prompt block appears only for operator turns
and includes only server-owned in-flight builds and PRs under remediation.
Factory Runs and PR-board APIs are registered under
`/api/v1/plugins/cultivate/`; administrative actions remain server-side
admin-gated. The brand logo is used unchanged on Cultivate overview/about only;
it is not a core asset or a navigation icon. Brand assets have their own
`brand/LICENSE`, separate from both the MIT core and Cultivate's BSL license.

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

## Registration and runtime contributions

Registration uses the selected provider's `register(registrar)` once, before
the frozen policy is published. The registrar is owner-bound; duplicate IDs,
names, job IDs, CLI commands, dashboard entries and routes fail rather than
replace another contribution. For example, the installed test-only provider in
`tests/fixtures/plugins/example/` registers:

```python
registrar.register_poller_hook("example-hooks", _hooks)
registrar.register_scheduler_job("example-tick", "example-tick", self.tick)
registrar.register_cli_command("example-hello", _configure_cli, _dispatch_cli)
registrar.register_route("GET", "/api/v1/plugins/example/status", _status)
registrar.register_dashboard_entry("example-page", DashboardExtensionManifest(
    id="example-page", route_path="/example", label="Example", bundle="example.v1.js",
))
registrar.register_prompt_provider("example_memory", "channel_memory", 4096, _applies, _render)
registrar.register_state_slot(ExampleState)  # a frozen dataclass type
```

The hook callable returns `PollerHooks` (for example, a
`recovery_relevance` factory). Plugin skill `pollers.json` is packaged under
the declared skill defaults; hook code comes from the selected provider module,
not from importing a script inside a skill directory. Core poller identities
remain reserved, and poller properties (`github_activity`, `github_ci_watch`,
`worklink_retained_checkout`) and the retained checkout operation are accepted
only from core or the selected first-party Cultivate owner; any other owner is
refused at registration. Poller service authority still requires
registered capabilities/profiles and `build_trigger_service_principal` checks.
Scheduler declarations are attached via `Scheduler.register_callable` after
core jobs and before reload/start; both name and job-ID conflicts reject boot.
CLI commands contribute an argparse `configure(subparsers)` function and a
`dispatch(args)` handler; core commands keep their parsers/behavior, including
the default `run`. `mimir plugins list` remains metadata-only even when plugins
are enabled. Backend route handlers are async aiohttp request handlers: use
`register_route(method, path, handler, role="user")`, with `role="admin"` for
admin-only routes. Routes must be under `/api/v1/plugins/<owner>/...` and are
authenticated; admin routes receive a separate server-side role check (nav
visibility is not authorization). Dashboard entries use the trusted-first-party
`DashboardExtensionManifest` with a unique `id` and in-app `route_path`;
enabled entries are shown in the existing dashboard shell.

### Turn prompt blocks and state

`register_prompt_provider(name, section, max_bytes, applies, render, static=False)`
requires a globally unique flat IFC-domain name without `:`, a positive byte
budget and one of `channel_memory`, `feedback`, `core_proposals`,
`session_summaries`, `usage`, `upcoming`, `commitments`, `self_state`. Providers
are sorted by name within a section. `applies(auth, event)` and
`render(auth, event, reader)` run per turn in worker threads; exceptions omit
that block rather than failing the turn. `reader(path)` reads only
server-owned files or home files accepted by `_prompt_file_is_trusted`. A
refused read omits the entire block and emits a content-free event; only
actually served resources receive host-authored `TRUSTED`/`INFORMATIONAL`
labels. Output is UTF-8 capped at a codepoint boundary with a visible suffix
within `max_bytes`, with one over-cap event for that provider/turn. Blocks are
inserted through the turn prompt's `use()` path, not graph or cached system
prompt reconstruction; synthesis-only and untrusted active-ingest paths do not
receive them. `static=True` disallows reader calls and caches the first rendered
text for the process lifetime; authors must supply genuinely byte-stable text.

`register_state_slot(FrozenDataclassType)` declares one typed slot per owner
before freeze. Host-mediated `auth.plugin_state(owner)` and
`auth.with_plugin_state(registrar, value)`, or `turn.plugin_state(owner)` and
`turn.set_plugin_state(registrar, value)`, validate the registrar's owner and
the declared type. Auth state travels with live subagent, shell-spawn and ACP
continuation carriers; turn state is live per turn and child snapshots. Poller
durable recovery does not serialize continuation state: it reconstructs live
authority instead. These checks protect host APIs, **not** against arbitrary
operations by trusted in-process Python.

## Package manifest contributions

Paths in the following keys are relative to the package containing the one
`mimir-plugin.toml`, not to the agent home. Package inventory and containment
are validated without importing a disabled provider. An enabled provider with
missing/escaping package resources fails validation. Use the
`tests/fixtures/plugins/example/mimir_example_plugin/` manifest and assets as
an executable reference (it is a test fixture, not a published Mimir plugin).

| Key | Shape |
| --- | --- |
| `themes` | Array of package-relative JSON skin paths, e.g. `["skins/example.json"]`. |
| `[frontend]` | `api_major` positive integer, required `module` ES-module resource, optional `css` resource and `assets` array of package-relative files. |
| `[dockerfile]` | `system_packages: string[]`, `build_args: {NAME: "default"}`, `users: string[]`, `directories: string[]` (absolute), `root_run: string[]`, `s6_services: string[]` (package-relative service directories). |
| `[defaults]` | Required string `version`, and optional arrays `skills` (directories), `prompts` (files), `scheduler_jobs` (files). |
| `[optional]` | `skills` is an array of tables with required package-relative directory `path` and optional `forge` tag (lowercase name, e.g. `{ path = "defaults/skills/github-poller", forge = "github" }`). Names must be unique across default and optional skills; unknown fields are rejected. |
| `[config.MIMIR_PLUGIN_<NAME>_<KEY>]` | Each declared key has `type = "string" | "integer" | "boolean"`, optional `description`, `secret` and `security` booleans. Hyphens in the plugin name become underscores in the prefix; normalized-name collisions are rejected. |

### Skins and frontend bundles

Each skin JSON needs `id`, `label`, `version`, `chrome`, `panel`,
`characterRenderer` and a **complete** set of the 66 `--mimir-*` token names
defined by `frontend/src/skins/types.ts` (including the eight trigger-color
tokens). The host maps `label` to the frontend `name` and token names to their
camel-cased bootstrap representation. `characterRenderer` declares a kind,
variant, `componentSlot: "agent-character"`, assets, state map, fallback state
and expression/motion capabilities; consult `skins/example.json` for the exact
structure. Optional `assets` and renderer asset `href` paths must be local
package files. Optional `fonts` provide a family and local `src` entries with
`url` and `format` (`woff2`, `woff`, `truetype`); only declare fonts actually
shipped in the distribution. Token values cannot contain CSS `url()` or
`@import`/external URLs; frontend CSS rejects external `@import` or
`url(http:|https:|//|data:)`. No third-party font or asset fetch is part of
this contract. Enabled files are served at
`/plugins/<name>/assets/<package-relative-path>?v=<manifest-version>`;
version-matching requests have immutable caching, others no-store. Path
containment and inventoried assets are checked on serve. **This repository
does not set a CSP header**: same-origin URL validation and serving are not a
sandbox or a deployment-wide CSP policy. Operators needing a CSP must configure
it separately.

One skin per provider can set `"default": true`. Effective default is the
process-owned `MIMIR_WEB_DEFAULT_SKIN` if set, else the unique enabled plugin
default, else `neon-terminal`. An unknown operator skin ID or multiple plugin
defaults without an override reject boot with a diagnostic. An explicit
`?skin=` preview and saved user skin preference take precedence over the
effective default. `/api/v1/web/bootstrap` supplies `skins.plugins`,
`ui.default_skin`, `dashboard_extensions` and `plugin_bundles` entries of shape
`{plugin, apiMajor, moduleUrl, cssUrl}` (`cssUrl` may be null). The auth gate
returns only public bootstrap fields before authentication.

For API major 1, an enabled compatible ES module runs with
`window.mimirPluginHost`: `apiMajor`, shared `react`, `reactDOM`,
`reactDOMClient`, `uiKit`, `themeTokens`, `authenticatedFetch`, `registerRoute` and
`registerNavEntry`. The example bundle registers
`{plugin: "example", id: "example-page", path: "/example", component: ExamplePage}`
and a matching `{plugin, id, label}` nav entry. The route must match that
owner's enabled dashboard manifest; registrations are owner-checked. Use the
host's authenticated same-origin fetch for backend APIs, not an external URL.
An incompatible `apiMajor` skips that bundle, shows an admin notice and leaves
core and other bundles running. A failed bundle is likewise isolated. Browser
plugins are trusted code in the existing page, not an isolated frame.

### Docker image assembly and services

`mimir plugins dockerfile [--stage base|build|runtime|services] [--all-installed]`
prints contributions in deterministic plugin-name order and stage order
base, build, runtime, services; it reads metadata, not provider modules.
Without `--all-installed`, only process-enabled providers contribute;
`--all-installed` explicitly includes disabled installed providers. The
`runtime` stage currently has no plugin-specific instructions. The existing
bundled skill fragments remain separate. `mimir scaffold-docker --all-installed`
uses the same explicit selection switch; otherwise the scaffold uses enabled
plugins and pins selected distribution versions. PyPI scaffolds install the
selected third-party pins after `mimir-agent`; provide private/local wheels to
the image build. Workspace scaffolds include/verify selected third-party pins at
startup. Cultivate is never installed from an index: PyPI scaffolds build core
and `plugins/cultivate` from `MIMIR_CONTROLLER_COMMIT`, and workspace scaffolds
sync the boot clone with `uv sync --all-packages`.
Generated service scripts are copied from installed package data into the
scaffold-owned `.mimir-scaffold/plugin-services/` build context; regeneration
removes stale service entries, rather than running mutable workspace scripts
as root at runtime.

Each `s6_services` directory must contain **only** executable `run` and
`type` containing `longrun`; symlinks, `finish`, dependencies, notification
files and escaping resources are rejected. Image assembly installs the script
under `/usr/local/libexec/mimir-plugin-services/<plugin>/<service>/run`, creates
root-owned `/etc/s6-overlay/s6-rc.d/<plugin>-<service>/{type,run}` and adds
the longrun to `user/contents.d`. The wrapper uses `s6-setuidgid mimir` to run
the provider script unprivileged. A scaffold with no selected services keeps
its tini entrypoint, `USER mimir` and existing `start.sh`. A service-bearing
scaffold installs s6-overlay 3.2.0.2 (noarch plus `TARGETARCH` archive),
supervises `mimir` and the selected longruns via root `/init`, with the Mimir
wrapper executing `s6-setuidgid mimir /usr/local/bin/start.sh`. `start.sh`
still ends by execing `mimir run`; it does not background plugin services.
Unsupported architecture fails the image build.

The repository-root production `Dockerfile` has s6 and the core `mimir` and
`watchdog` services. With `MIMIR_PLUGIN_SELECTION=cultivate` (or `all`) at
build time it also assembles the trusted first-party fragment: pinned OpenCode
and factory adapters, UID 1002 Worklink user, data directories,
`opencode-bootstrap` and the exact `worklink-execd` s6 service (not a generic
prefixed plugin service). Both venvs run Cultivate built from the verified
source commit, never from an index: the root executor venv overlays core and
`plugins/cultivate` from `MIMIR_EXECUTOR_COMMIT` with `--no-deps` (so the
plugin's exact `mimir-agent==` pin cannot replace the overlaid core), and the
user-owned controller venv installs core and `plugins/cultivate` together from
`MIMIR_CONTROLLER_COMMIT`, which the build requires to equal the executor commit.
Without the selection the image is core-only: no executor venv, no `worklink`
user and no `worklink-execd` service. Third-party plugins are installed at
**build time** too: set `MIMIR_PLUGIN_PACKAGES` to comma-separated exact
`distribution==version` pins (never `mimir-cultivate`, which the build refuses),
`MIMIR_PLUGIN_SELECTION` to the selected names, and optionally
`MIMIR_PLUGIN_ALL_INSTALLED=1` to include disabled installed packages. The build
verifies installed versions/selection and assembles services; a non-installable
selected provider fails the build. Runtime `MIMIR_PLUGINS` alone neither installs
packages nor changes s6 definitions. Rebuild the image when adding services.
The PyPI self-update paths (the approved pending-update flag,
`request_mimir_update` and `mimir update --apply`) refuse wherever Cultivate is
installed from source, which is detected from `<venv>/controller-source-commit` or
a `file://` install of `mimir-cultivate`. A refused startup update consumes the
flag and boots the current version, the same as a failed install. To update,
rebuild the image or re-sync the checkout at the new commit, so core and the
plugin stay paired.

### Defaults and configuration

At setup and startup, selected plugins seed missing package defaults into
`skills/<plugin>-<skill>/`, `prompts/<plugin>/` and namespaced scheduler jobs;
existing operator files are not overwritten. Subsequent package versions
surface per-owner versioned defaults-upgrade proposals rather than refreshing
plugin files through the unconditional core built-in-skill path. Keep plugin
defaults separate from core-shipped defaults.
Optional skills are installed only on request. `mimir skills list-optional`
shows selected plugins' defaults and optional skills with their forge and install
state. Use `mimir skills install <plugin>:<skill>` or
`mimir skills install --forge github`; `--restore` backs up a modified plugin
skill to a timestamped directory under `<home>/.mimir/plugin-skill-backups/`
(outside skill-loader roots) before replacing it. Previously seeded skills
remain installed. An unselected plugin's skills are unavailable even if its
distribution is installed or a prior seed remains in the home.

`plugin_config_rows(owner, manifests)` reads only declared keys of an enabled
owner, with secret/security values redacted and `mutable: false`;
`validate_plugin_config_write(owner, name, manifests)` rejects foreign, core,
undeclared or security-sensitive keys on host-mediated writes. The admin web
config view is read-only, server-side admin-gated and secret-redacted: it does
not provide an environment write endpoint. This API does not prevent trusted
provider Python from accessing the process environment directly.

## Testing a provider

The example under `tests/fixtures/plugins/example/` is installable for tests
only and excluded from the published `mimir-agent` wheel. It exercises tool,
poller/hook, scheduler, CLI, route/dashboard, prompt, skin/frontend, Docker/s6,
defaults, config and typed state contributions; disabled-provider nonimport is
checked in a child process. For repository CI, run
`env -u MIMIR_PLUGINS uv run pytest -q` for core only and
`MIMIR_PLUGINS=cultivate uv run --all-packages --extra dev --extra bench
pytest -q tests plugins/cultivate/tests` for the selected provider. Run both
with access control unset and enforced. Run each test-bearing directory under
`mimir/optional-skills/` and
`plugins/cultivate/mimir_cultivate/defaults/skills/` in its own pytest process
with `--all-packages`, then `npm ci`, `npm run test` and `npm run build` for
the frontend. `sh scripts/test_full_access_control.sh` runs this matrix.
For changes to a test file also run that file alone before the full suite.
None of these tests makes installed provider code untrusted or relaxes
no-plugin core policy parity.
