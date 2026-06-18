# React Dashboard Extensions

Mimir's React dashboard extension contract is a small first-party registry for
tabs inside the `/app` shell. It is not a marketplace, remote plugin loader, or
security sandbox.

## Trust Boundary

Dashboard extension v1 only accepts trusted first-party code shipped with mimir.
Manifests must set `trustedFirstParty: true` in TypeScript and
`trusted_first_party=True` in Python. Arbitrary remote bundles, operator-installed
marketplace plugins, privilege separation, and browser sandboxing are out of
scope for this contract.

## Manifest Shape

Frontend manifests live in `frontend/src/dashboardExtensions.ts`; backend
manifests live in `mimir/dashboard_extensions.py`.

```ts
interface DashboardExtensionManifest {
  id: string;
  routePath: `/${string}`;
  label: string;
  icon?: string;
  navPosition: number;
  enabled: boolean;
  bundle?: string;
  css?: string[];
  apiNamespace?: string;
  trustedFirstParty: true;
}
```

Python uses the same fields in snake_case:

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

`routePath` is the React route under the `/app` basename. `navPosition` controls
tab order; lower values render first. `enabled=false` hides the tab and prevents
its optional backend namespace hook from registering routes. `bundle` and `css`
are reserved for packaged first-party assets; v1 does not load arbitrary remote
URLs.

## Registry And Navigation

The app shell imports `dashboardSurfaces` from
`frontend/src/dashboardExtensions.ts`. To hide, show, or reorder tabs, change
the manifest data or pass overrides to `getDashboardSurfaces()`; the shell maps
the enabled, sorted registry into both navigation links and `<Route>` entries.

At least one dashboard tab, `chat`, is registered only through this registry
path. The other first-party shell routes use the same path so route edits are not
needed for tab ordering or visibility changes.

## Backend API Namespaces

`apiNamespace` is optional. When present, it is a key into trusted backend hooks
provided by mimir, not a dynamic import path. `web_ui.register_routes()` wires
enabled manifests through `add_backend_namespace_routes()`.

The first registered namespace is `ops`, which owns:

- `GET /api/ops`
- `GET /api/v1/ops`

If the `ops` manifest is disabled, those API routes are not registered through
the hook. Unknown namespaces are ignored, which lets a manifest be frontend-only.

`GET /api/v1/web/bootstrap` includes the enabled manifest payload under
`dashboard_extensions` for inspection by React clients and tests. This payload is
metadata only; the browser does not execute code from it.
