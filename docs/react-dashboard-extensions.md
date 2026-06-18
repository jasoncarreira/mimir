# React Dashboard Extensions

Mimir's React dashboard extension contract is a small first-party registry for
tabs inside the `/app` shell. It is not a marketplace, remote plugin loader, or
security sandbox.

## Trust Boundary

Dashboard extension v1 only accepts trusted first-party code shipped with mimir.
Manifests must set `trusted_first_party: true` in the generated TypeScript
contract and `trusted_first_party=True` in Python. Arbitrary remote bundles, operator-installed
marketplace plugins, privilege separation, and browser sandboxing are out of
scope for this contract.

## Manifest Shape

Backend manifests live in `mimir/dashboard_extensions.py` and are exposed to
React through the generated `DashboardExtensionManifest` contract in
`frontend/src/api/generated/contracts.ts`. `frontend/src/dashboardExtensions.ts`
adds shell-only metadata (placeholder tabs, details copy, filter labels) but
reuses the generated manifest shape instead of declaring a second interface.

```ts
interface DashboardExtensionManifest {
  id: string;
  route_path: string;
  label: string;
  icon: string | null;
  nav_position: number;
  enabled: boolean;
  bundle: string | null;
  css: string[];
  api_namespace: string | null;
  trusted_first_party: true;
}
```

Python uses the same fields:

```py
DashboardExtensionManifest(
    id="ops",
    route_path="/ops",
    label="Ops",
    icon="activity",
    nav_position=30,
    enabled=True,
    api_namespace="ops",
    trusted_first_party=True,
)
```

`route_path` is the React route under the `/app` basename. `nav_position`
controls tab order; lower values render first. `enabled=false` hides the tab and
prevents its optional backend namespace hook from registering routes. `bundle`
and `css` are reserved for packaged first-party assets; v1 does not load
arbitrary remote URLs.

## Registry And Navigation

The app shell reads `dashboard_extensions` from `/api/v1/web/bootstrap` and
passes that generated-contract payload into `getDashboardSurfaces()` in
`frontend/src/dashboardExtensions.ts`. To hide, show, or reorder tabs, change
the backend manifest data or pass overrides to `getDashboardSurfaces()`; the
shell maps the enabled, sorted registry into both navigation links and `<Route>`
entries.

At least one dashboard tab, `chat`, is registered only through this registry
path. The other first-party shell routes use the same path so route edits are not
needed for tab ordering or visibility changes.

## Backend API Namespaces

`api_namespace` is optional. When present, it is a key into trusted backend
hooks provided by mimir, not a dynamic import path. `web_ui.register_routes()`
wires enabled manifests through `add_backend_namespace_routes()`.

The first registered namespace is `ops`, which owns:

- `GET /api/ops`
- `GET /api/v1/ops`

If the `ops` manifest is disabled, those API routes are not registered through
the hook. Unknown namespaces are ignored, which lets a manifest be frontend-only.

`GET /api/v1/web/bootstrap` includes the enabled manifest payload under
`dashboard_extensions` for inspection by React clients and tests. This payload is
metadata only; the browser does not execute code from it.
