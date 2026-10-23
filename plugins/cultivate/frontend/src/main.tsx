import React from "./react";
import { FactoryRuns } from "./runs";
import { PullRequests } from "./prs";
import "./style.css";

const host = window.mimirPluginHost;
if (!host || host.apiMajor !== 1 || host.uiKit.version !== 1) {
  throw new Error("Cultivate requires dashboard host API and UI kit version 1");
}

host.registerRoute({ plugin: "cultivate", id: "cultivate-factory-runs", path: "/cultivate/factory-runs", component: FactoryRuns });
host.registerNavEntry({ plugin: "cultivate", id: "cultivate-factory-runs", label: "Factory Runs", detail: "Feature factory runs and diagnostics" });

// Ops mounts this panel only after the selected bundle has loaded.
window.mimirCultivate = Object.freeze({ PullRequests });
