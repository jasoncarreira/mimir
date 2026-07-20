import React from "react";
import { useSearchParams } from "react-router-dom";

import { DashboardHeader } from "../ui";
import { AdminConfigView } from "./AdminConfigRoute";
import { McpServersView } from "./McpServersRoute";
import { UsersView } from "./UsersRoute";

// Consolidated Admin surface (github #563 / #855 follow-up): config, users, and
// MCP servers as sub-tabs of a single admin-only top-level nav entry, mirroring
// the Ops sub-tab pattern. admin-users / admin-mcp remain nav_hidden manifests
// so their backend routes still register; this surface renders their views.
const TABS: Array<[string, string]> = [
  ["config", "Config"],
  ["users", "Users"],
  ["mcp", "MCP Servers"]
];

const TAB_IDS = TABS.map(([id]) => id);

export function AdminRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const activeTab = requested && TAB_IDS.includes(requested) ? requested : "config";

  function setActiveTab(tab: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", tab);
    setSearchParams(params, { replace: true });
  }

  return (
    <div className="admin-route">
      <DashboardHeader eyebrow="Admin" title="Admin">
        <p className="app-copy">Runtime config, per-user keys, and MCP server authorization — admin only.</p>
      </DashboardHeader>
      <div className="ui-tabs">
        <div aria-label="Admin tabs" className="ui-tabs__list" role="tablist">
          {TABS.map(([id, label]) => (
            <button
              aria-controls={`admin-${id}-panel`}
              aria-selected={activeTab === id}
              className="ui-tabs__tab"
              id={`admin-${id}-tab`}
              key={id}
              onClick={() => setActiveTab(id)}
              role="tab"
              tabIndex={activeTab === id ? 0 : -1}
              type="button"
            >
              {label}
            </button>
          ))}
        </div>
        <section
          aria-labelledby={`admin-${activeTab}-tab`}
          className="ui-tabs__panel"
          id={`admin-${activeTab}-panel`}
          role="tabpanel"
        >
          {activeTab === "config" ? <AdminConfigView /> : null}
          {activeTab === "users" ? <UsersView /> : null}
          {activeTab === "mcp" ? <McpServersView /> : null}
        </section>
      </div>
    </div>
  );
}
