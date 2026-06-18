import React from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { AuthPanel } from "./auth";
import { shellRoutes } from "./routeConfig";
import { useUiStore } from "./uiStore";
import type { AppSearch, ShellRoute } from "./types";

const routeTabs = ["overview", "activity", "filters"];

function nextSearch(search: AppSearch, patch: Partial<AppSearch>): AppSearch {
  return {
    turn: Object.hasOwn(patch, "turn") ? patch.turn : search.turn,
    tab: Object.hasOwn(patch, "tab") ? patch.tab : search.tab,
    filter: Object.hasOwn(patch, "filter") ? patch.filter : search.filter,
    detail: Object.hasOwn(patch, "detail") ? patch.detail : search.detail
  };
}

function LinkWithState({
  route,
  search,
  selected
}: {
  route: ShellRoute;
  search: AppSearch;
  selected: boolean;
}) {
  return (
    <Link
      to={route.path}
      search={search}
      className="nav-tab"
      role="tab"
      id={`nav-${route.id}`}
      aria-selected={selected}
      aria-controls="main-panel"
    >
      {route.label}
    </Link>
  );
}

function Collapsible({
  id,
  title,
  children
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  const collapsed = useUiStore((state) => Boolean(state.collapsedRegions[id]));
  const toggleRegion = useUiStore((state) => state.toggleRegion);
  const panelId = `${id}-panel`;

  return (
    <section className="collapsible">
      <button
        className="collapse-trigger"
        type="button"
        aria-expanded={!collapsed}
        aria-controls={panelId}
        onClick={() => toggleRegion(id)}
      >
        <span>{title}</span>
        <span aria-hidden="true">{collapsed ? "+" : "-"}</span>
      </button>
      <div id={panelId} hidden={collapsed}>
        {children}
      </div>
    </section>
  );
}

function RouteStateControls({ search }: { search: AppSearch }) {
  const navigate = useNavigate();
  const activeTab = search.tab || "overview";
  const [turnEntry, setTurnEntry] = React.useState(search.turn || "");
  const [filterEntry, setFilterEntry] = React.useState(search.filter || "");
  const [detailEntry, setDetailEntry] = React.useState(search.detail || "");

  React.useEffect(() => setTurnEntry(search.turn || ""), [search.turn]);
  React.useEffect(() => setFilterEntry(search.filter || ""), [search.filter]);
  React.useEffect(() => setDetailEntry(search.detail || ""), [search.detail]);

  const applySearch = React.useCallback(
    (patch: Partial<AppSearch>) => {
      void navigate({
        to: ".",
        search: (current: AppSearch) => nextSearch(current, patch),
        replace: false
      });
    },
    [navigate]
  );

  return (
    <Collapsible id="route-state" title="Route state">
      <div className="route-state-grid">
        <div className="route-tabs" role="tablist" aria-label="Route subviews">
          {routeTabs.map((tab) => (
            <button
              key={tab}
              type="button"
              role="tab"
              id={`route-tab-${tab}`}
              aria-selected={activeTab === tab}
              aria-controls="route-tabpanel"
              className={activeTab === tab ? "route-tab is-active" : "route-tab"}
              onClick={() => applySearch({ tab })}
            >
              {tab}
            </button>
          ))}
        </div>

        <form
          className="state-form"
          onSubmit={(event) => {
            event.preventDefault();
            applySearch({
              turn: turnEntry.trim() || undefined,
              filter: filterEntry.trim() || undefined,
              detail: detailEntry.trim() || undefined
            });
          }}
        >
          <label>
            <span>Selected turn</span>
            <input
              value={turnEntry}
              onChange={(event) => setTurnEntry(event.target.value)}
              placeholder="turn id"
            />
          </label>
          <label>
            <span>Filter</span>
            <input
              value={filterEntry}
              onChange={(event) => setFilterEntry(event.target.value)}
              placeholder="status:type"
            />
          </label>
          <label>
            <span>Drilldown</span>
            <input
              value={detailEntry}
              onChange={(event) => setDetailEntry(event.target.value)}
              placeholder="detail target"
            />
          </label>
          <button type="submit">Apply</button>
        </form>
      </div>
    </Collapsible>
  );
}

function MainPanel({ route, search }: { route: ShellRoute; search: AppSearch }) {
  const activeTab = search.tab || "overview";

  return (
    <section
      id="main-panel"
      className="main-panel"
      role="tabpanel"
      aria-labelledby={`nav-${route.id}`}
      tabIndex={0}
    >
      <div className="panel-heading">
        <div>
          <p className="eyebrow">App route</p>
          <h1>{route.label}</h1>
          <p>{route.summary}</p>
        </div>
        {route.legacyHref ? <a className="legacy-link" href={route.legacyHref}>Open legacy page</a> : null}
      </div>

      <RouteStateControls search={search} />

      <section
        id="route-tabpanel"
        className="placeholder-panel"
        role="tabpanel"
        aria-labelledby={`route-tab-${activeTab}`}
      >
        <dl className="state-summary">
          <div><dt>Subview</dt><dd>{activeTab}</dd></div>
          <div><dt>Turn</dt><dd>{search.turn || "not selected"}</dd></div>
          <div><dt>Filter</dt><dd>{search.filter || "none"}</dd></div>
          <div><dt>Drilldown</dt><dd>{search.detail || "none"}</dd></div>
        </dl>
      </section>
    </section>
  );
}

function DetailsHost({ search }: { search: AppSearch }) {
  const detailsPanelOpen = useUiStore((state) => state.detailsPanelOpen);
  const setDetailsPanelOpen = useUiStore((state) => state.setDetailsPanelOpen);

  return (
    <aside className="details-host" aria-labelledby="details-title">
      <button
        className="details-toggle"
        type="button"
        aria-expanded={detailsPanelOpen}
        aria-controls="details-panel"
        onClick={() => setDetailsPanelOpen(!detailsPanelOpen)}
      >
        <span>Details</span>
        <span aria-hidden="true">{detailsPanelOpen ? "-" : "+"}</span>
      </button>
      <div id="details-panel" hidden={!detailsPanelOpen}>
        <h2 id="details-title">Details host</h2>
        <p>{search.detail ? `Target: ${search.detail}` : "No drilldown target selected."}</p>
      </div>
    </aside>
  );
}

function StatusBar({ search }: { search: AppSearch }) {
  const skin = useUiStore((state) => state.skin);
  const setSkin = useUiStore((state) => state.setSkin);

  return (
    <footer className="status-bar" aria-label="Application status">
      <span>Route state: synced</span>
      <label>
        <span>Skin</span>
        <select value={skin} onChange={(event) => setSkin(event.target.value as "system" | "light" | "dark")}>
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
      <span>Turn: {search.turn || "none"}</span>
    </footer>
  );
}

export function AppShell({ route }: { route: ShellRoute }) {
  const search = useSearch({ strict: false }) as AppSearch;
  const navCollapsed = useUiStore((state) => state.navCollapsed);
  const toggleNavCollapsed = useUiStore((state) => state.toggleNavCollapsed);
  const activeRoute = shellRoutes.find((item) => item.path === route.path) || route;

  return (
    <div className={navCollapsed ? "app-frame nav-is-collapsed" : "app-frame"}>
      <header className="top-chrome">
        <div>
          <p className="eyebrow">React app</p>
          <strong>Mimir App</strong>
        </div>
        <button type="button" onClick={toggleNavCollapsed} aria-expanded={!navCollapsed} aria-controls="primary-nav">
          Navigation
        </button>
      </header>

      <nav id="primary-nav" className="primary-nav" aria-label="Primary" hidden={navCollapsed}>
        <div role="tablist" aria-label="App sections">
          {shellRoutes.map((item) => (
            <LinkWithState
              key={item.id}
              route={item}
              search={search}
              selected={activeRoute.id === item.id}
            />
          ))}
        </div>
      </nav>

      <main className="content-region">
        <MainPanel route={activeRoute} search={search} />
        <AuthPanel />
      </main>

      <DetailsHost search={search} />
      <StatusBar search={search} />
    </div>
  );
}
