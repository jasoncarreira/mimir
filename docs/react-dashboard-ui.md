# React Dashboard UI

The React dashboard UI kit lives in `frontend/src/ui`. Dashboard pages should
compose these primitives instead of adding page-local chrome, table, badge,
modal, or loading-state CSS.

## Theme Tokens

`SkinProvider` converts the active skin manifest into `--mimir-*` CSS variables
on `.skin-root`. Components consume those variables only; they should not branch
on `default-retro` or any future skin id.

The default retro skin defines tokens for:

- color surfaces, text, status tones, code blocks, focus rings, and chrome
- typography family, mono family, sizes, weights, and line heights
- spacing scale and shell padding
- panel/control radii, border widths, elevation, interaction states, and motion

Future skins should implement the same `SkinTokens` contract in
`frontend/src/skins/types.ts`.

## Page Structure

Use `DashboardShell` once per React dashboard surface, then add
`DashboardHeader` and page sections built from `Panel` or `Card`.

```tsx
import { DashboardHeader, DashboardShell, Panel } from "./ui";

export function ExampleDashboard() {
  return (
    <DashboardShell>
      <DashboardHeader eyebrow="Ops" title="Operations" />
      <Panel title="Health" subtitle="Current worker status">
        ...
      </Panel>
    </DashboardShell>
  );
}
```

## Primitives

Core primitives exported from `frontend/src/ui`:

- `Tabs` and `NavList` for keyboardable tab sets and dashboard navigation
- `Panel` and `Card` for dashboard surfaces
- `DataTable` for accessible tabular data
- `Drawer` and `Dialog` for modal side panels and dialogs with focus trapping
- `CodeBlock` and `LogBlock` for commands, snippets, and logs
- `Badge` for status pills
- `ToastRegion` for live feedback
- `EmptyState`, `ErrorState`, and `LoadingState` for common async states
- `Timeline` for ordered activity streams

`frontend/src/ui/examples.tsx` is the Storybook-style catalog. Add examples
there when introducing a primitive or a meaningful variant. Keep the catalog
out of production routes; import it only from development-only tooling or
manual local previews.

## Accessibility Rules

- Give every `Tabs` instance a concise `label`; arrow keys, Home, and End are
  already handled by the primitive.
- Dialogs and drawers require an `open` flag and `onClose`; Escape closes them,
  focus is trapped while open, and focus returns to the opener.
- Use `ErrorState` for request failures so the message is announced via
  `role="alert"`.
- Use `ToastRegion` for transient feedback; it uses a polite live region.
- Avoid custom animations unless they respect `prefers-reduced-motion`.

## Migration Guidance

When migrating a legacy route into React, keep route registration and data
contracts in the existing backend layer. The React route component should only
compose primitives, call the typed API client, and render page-specific data.
If a route needs a visual pattern missing from the UI kit, add the reusable
primitive and an example before using it in the route.
