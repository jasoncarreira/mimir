import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getOpsDashboard } from "../api";
import type { ChainlinkIssue } from "../api/ops";
import { drilldownHref } from "../routeState";
import {
  Badge,
  Button,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";
import {
  buildOpsSummaryMetrics,
  formatCost,
  formatPercent,
  mapToRows,
  quotaRows,
  safeOpsDashboardData,
  schedulerEventRows,
  tokenUsageRows,
  type SafeOpsDashboardData
} from "./opsViewModel";

function asDisplay(value: unknown) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function SummaryGrid({ data }: { data: SafeOpsDashboardData }) {
  const metrics = buildOpsSummaryMetrics(data.summary);
  return (
    <section aria-label="Ops summary" className="ops-summary-grid">
      {metrics.map((metric) => (
        <div className={`ops-stat ops-stat--${metric.tone}`} key={metric.key}>
          <strong>{metric.value.toLocaleString()}</strong>
          <span>{metric.label}</span>
        </div>
      ))}
    </section>
  );
}

function RowsTable({
  caption,
  empty,
  rows
}: {
  caption: string;
  empty: string;
  rows: Array<{ key: string; value: number }>;
}) {
  if (!rows.length) return <EmptyState title={empty} />;
  return (
    <DataTable
      caption={caption}
      columns={[
        { key: "key", header: "Signal" },
        { key: "value", header: "Count" }
      ]}
      rows={rows.map((row) => ({
        key: <code>{row.key}</code>,
        value: row.value.toLocaleString()
      }))}
    />
  );
}

function ResourceQuotaPanel({ data }: { data: SafeOpsDashboardData }) {
  const quotas = quotaRows(data.usage_history);
  const tokens = tokenUsageRows(data.token_usage_history);
  const resourceRows = mapToRows(
    Object.fromEntries(
      Object.entries(data.by_event).filter(([key]) =>
        key.includes("resource") || key.includes("quota") || key.includes("usage")
      )
    )
  );

  return (
    <div className="ops-panel-stack">
      <Panel title="Quota Utilization" subtitle="Subscription-window utilization from the ops payload.">
        {quotas.length ? (
          <DataTable
            columns={[
              { key: "provider", header: "Provider" },
              { key: "window", header: "Window" },
              { key: "points", header: "Points" },
              { key: "latest", header: "Latest" }
            ]}
            rows={quotas.map((row) => ({
              provider: row.provider,
              window: row.window,
              points: row.points.toLocaleString(),
              latest: formatPercent(row.latestUtilization)
            }))}
          />
        ) : (
          <EmptyState title="No quota samples in this window" />
        )}
      </Panel>
      <Panel title="Token Usage" subtitle="Daily token/cost rollups from turns in the selected window.">
        {tokens.length ? (
          <DataTable
            columns={[
              { key: "date", header: "Date" },
              { key: "turns", header: "Turns" },
              { key: "input", header: "Input" },
              { key: "cache", header: "Cache" },
              { key: "output", header: "Output" },
              { key: "cost", header: "Cost" }
            ]}
            rows={tokens.map((row) => ({
              date: row.date,
              turns: row.turns.toLocaleString(),
              input: row.input.toLocaleString(),
              cache: (row.cacheCreation + row.cacheRead).toLocaleString(),
              output: row.output.toLocaleString(),
              cost: formatCost(row.cost)
            }))}
          />
        ) : (
          <EmptyState title="No token usage rows in this window" />
        )}
      </Panel>
      <Panel title="Resource Signals">
        <RowsTable caption="Resource and quota event counts" empty="No resource events in this window" rows={resourceRows} />
      </Panel>
    </div>
  );
}

function SchedulerPanel({ data }: { data: SafeOpsDashboardData }) {
  return (
    <div className="ops-panel-stack">
      <Panel title="Scheduler, Poller, and Job Signals">
        {schedulerEventRows(data).length ? (
          <DataTable
            caption="Scheduler-like event counts"
            columns={[
              { key: "key", header: "Signal" },
              { key: "value", header: "Count" },
              { key: "trace", header: "Trace" }
            ]}
            rows={schedulerEventRows(data).map((row) => ({
              key: <code>{row.key}</code>,
              value: row.value.toLocaleString(),
              trace: <Link to={drilldownHref("/turns", { filter: row.key, event: row.key })}>Turns</Link>
            }))}
          />
        ) : (
          <EmptyState title="No scheduler, poller, job, resource, or queue signals in this window" />
        )}
      </Panel>
      <Panel title="Queued by Trigger">
        <RowsTable caption="Queued events by trigger" empty="No queued trigger data" rows={mapToRows(data.queued_by_trigger)} />
      </Panel>
      <Panel title="Queued by Channel">
        <RowsTable caption="Queued events by channel" empty="No queued channel data" rows={mapToRows(data.queued_by_channel)} />
      </Panel>
      <Panel title="Resolution Paths" subtitle="Context-resolution histograms for tool calls.">
        {Object.keys(data.resolution_paths).length ? (
          <div className="ops-resolution-grid">
            {Object.entries(data.resolution_paths).map(([kind, paths]) => (
              <div className="ops-resolution" key={kind}>
                <h3>{kind}</h3>
                <p>{Object.entries(paths).map(([path, count]) => `${path}: ${count}`).join(" | ")}</p>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="No resolution-path events in this window" />
        )}
      </Panel>
    </div>
  );
}

function AsyncJobsPanel({ data }: { data: SafeOpsDashboardData }) {
  const shell = data.shell_jobs;
  return (
    <div className="ops-panel-stack">
      <section aria-label="Shell job summary" className="ops-summary-grid ops-summary-grid--compact">
        {[
          ["spawned", shell.spawned],
          ["routed", shell.routed],
          ["no_channel", shell.no_channel],
          ["enqueue_failed", shell.enqueue_failed]
        ].map(([key, value]) => (
          <div className={`ops-stat ${Number(value) > 0 && String(key) !== "spawned" && String(key) !== "routed" ? "ops-stat--danger" : ""}`} key={String(key)}>
            <strong>{Number(value).toLocaleString()}</strong>
            <span>{String(key).replaceAll("_", " ")}</span>
          </div>
        ))}
      </section>
      <Panel title="Spawned by Channel">
        <RowsTable caption="Shell jobs spawned by channel" empty="No async shell jobs spawned in this window" rows={mapToRows(shell.spawn_by_channel)} />
      </Panel>
      <Panel title="Tool Calls">
        {data.tools.length ? (
          <DataTable
            columns={[
              { key: "tool", header: "Tool" },
              { key: "calls", header: "Calls" },
              { key: "errors", header: "Errors" },
              { key: "rate", header: "Failure rate" },
              { key: "avg", header: "Avg ms" }
            ]}
            rows={data.tools.map((tool) => ({
              tool: <code>{tool.tool || "unknown"}</code>,
              calls: tool.calls.toLocaleString(),
              errors: tool.errors.toLocaleString(),
              rate: formatPercent(tool.failure_rate),
              avg: Math.round(tool.avg_duration_ms).toLocaleString()
            }))}
          />
        ) : (
          <EmptyState title="No tool calls in this window" />
        )}
      </Panel>
    </div>
  );
}

function HealthPanel({ data }: { data: SafeOpsDashboardData }) {
  return (
    <div className="ops-panel-stack">
      <Panel title="Failures by Kind">
        <RowsTable caption="Failure-shaped events by kind" empty="No failures in this window" rows={mapToRows(data.failures_by_kind)} />
      </Panel>
      <Panel title="Recent Failures">
        {data.recent_failures.length ? (
          <DataTable
            columns={[
              { key: "t", header: "Time" },
              { key: "kind", header: "Kind" },
              { key: "channel", header: "Channel" },
              { key: "detail", header: "Detail" }
            ]}
            rows={data.recent_failures.map((failure) => ({
              t: failure.t,
              kind: <code>{failure.kind}</code>,
              channel: failure.channel_id ?? "",
              detail: (
                <>
                  <span>{failure.detail}</span>{" "}
                  <Link to={drilldownHref("/turns", {
                    filter: "failure",
                    event: failure.kind,
                    channel: failure.channel_id || undefined,
                    q: failure.detail || failure.kind
                  })}>Turns</Link>
                </>
              )
            }))}
          />
        ) : (
          <EmptyState title="No recent failures in this window" />
        )}
      </Panel>
    </div>
  );
}

function ChainlinkPanel({ data }: { data: SafeOpsDashboardData }) {
  const chainlink = data.chainlink_issues;
  if (!chainlink.available) {
    return <ErrorState title="Chainlink unavailable">{chainlink.error || "No Chainlink issue data for this home."}</ErrorState>;
  }
  return (
    <div className="ops-panel-stack">
      <Panel
        actions={<Badge tone={chainlink.truncated ? "warning" : "info"}>{chainlink.issues.length} open</Badge>}
        title="Chainlink"
        subtitle={chainlink.truncated ? `Showing ${chainlink.issues.length} of ${chainlink.total_count ?? "unknown"}.` : "Open issues from this home."}
      >
        {chainlink.issues.length ? (
          <DataTable
            columns={[
              { key: "id", header: "#" },
              { key: "title", header: "Title" },
              { key: "status", header: "Status" },
              { key: "priority", header: "Priority" },
              { key: "parent", header: "Parent" },
              { key: "updated", header: "Updated" }
            ]}
            rows={(chainlink.issues as ChainlinkIssue[]).map((issue) => ({
              id: asDisplay(issue.id),
              title: asDisplay(issue.title),
              status: asDisplay(issue.status),
              priority: asDisplay(issue.priority),
              parent: issue.parent_id ? `#${asDisplay(issue.parent_id)}` : "",
              updated: asDisplay(issue.updated_at).slice(0, 19).replace("T", " ")
            }))}
          />
        ) : (
          <EmptyState title="No open Chainlink issues" />
        )}
      </Panel>
      <Panel title="Backlog" subtitle="Instrumentation gaps tracked by the current ops payload.">
        {data.backlog.length ? (
          <div className="ops-backlog-list">
            {data.backlog.map((item) => (
              <article className="ops-backlog" key={item.id}>
                <h3>{item.title}</h3>
                <Badge tone="warning">{item.status}</Badge>
                <p>{item.blocker}</p>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState title="No backlog items in payload" />
        )}
      </Panel>
    </div>
  );
}

function RawPanel({ data }: { data: SafeOpsDashboardData }) {
  return (
    <div className="ops-panel-stack">
      <Panel title="All Event Types">
        <RowsTable caption="All event counts" empty="No events in this window" rows={mapToRows(data.by_event)} />
      </Panel>
      <Panel title="Recent Failure JSON">
        <CodeBlock code={data.recent_failures.length ? JSON.stringify(data.recent_failures, null, 2) : "(none)"} language="json" />
      </Panel>
    </div>
  );
}

function OpsContent({ data: rawData }: { data: unknown }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const data = safeOpsDashboardData(rawData);
  const tabs = [
    ["overview", "Overview"],
    ["resources", "Resources"],
    ["scheduler", "Scheduler"],
    ["async", "Async jobs"],
    ["health", "Health"],
    ["chainlink", "Chainlink"],
    ["raw", "Raw"]
  ];
  const activeTab = tabs.some(([id]) => id === searchParams.get("tab")) ? searchParams.get("tab") || "overview" : "overview";

  function setActiveTab(tab: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", tab);
    setSearchParams(params);
  }

  return (
    <div className="ops-route">
      <SummaryGrid data={data} />
      <div className="ui-tabs">
        <div aria-label="Ops tabs" className="ui-tabs__list" role="tablist">
          {tabs.map(([id, label]) => (
            <button
              aria-controls={`ops-${id}-panel`}
              aria-selected={activeTab === id}
              className="ui-tabs__tab"
              id={`ops-${id}-tab`}
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
        <section aria-labelledby={`ops-${activeTab}-tab`} className="ui-tabs__panel" id={`ops-${activeTab}-panel`} role="tabpanel">
          {activeTab === "overview" ? (
            <div className="ops-panel-grid">
              <Panel title="Event Mix">
                <RowsTable caption="Top event types" empty="No events in this window" rows={mapToRows(data.by_event).slice(0, 12)} />
              </Panel>
              <Panel title="Events vs Queued">
                {data.timeseries.length ? (
                  <DataTable
                    columns={[
                      { key: "day", header: "Day" },
                      { key: "events", header: "Events" },
                      { key: "queued", header: "Queued" }
                    ]}
                    rows={data.timeseries.map((point) => ({
                      day: point.day,
                      events: point.events.toLocaleString(),
                      queued: point.queued.toLocaleString()
                    }))}
                  />
                ) : (
                  <EmptyState title="No time-series points in this window" />
                )}
              </Panel>
            </div>
          ) : null}
          {activeTab === "resources" ? <ResourceQuotaPanel data={data} /> : null}
          {activeTab === "scheduler" ? <SchedulerPanel data={data} /> : null}
          {activeTab === "async" ? <AsyncJobsPanel data={data} /> : null}
          {activeTab === "health" ? <HealthPanel data={data} /> : null}
          {activeTab === "chainlink" ? <ChainlinkPanel data={data} /> : null}
          {activeTab === "raw" ? <RawPanel data={data} /> : null}
        </section>
      </div>
    </div>
  );
}

export function OpsRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const daysParam = searchParams.get("days") || "7";
  const days = Number.parseInt(daysParam, 10);
  const validDays = Number.isFinite(days) && days > 0 ? days : 7;
  const query = useQuery({
    queryKey: ["ops-dashboard", validDays],
    queryFn: async () => (await getOpsDashboard({ days: validDays }, { cache: "no-store" })).data
  });

  return (
    <>
      <div className="ops-header-row">
        <div>
          <p className="ui-eyebrow">Operations</p>
          <h1>Ops Dashboard</h1>
          <p className="app-copy">
            Window: {validDays} days{query.data ? ` | Generated ${query.data.generated_at}` : ""}
          </p>
        </div>
        <form
          className="ops-controls"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const nextDays = String(form.get("days") || "7");
            const params = new URLSearchParams(searchParams);
            params.set("days", nextDays);
            setSearchParams(params, { replace: false });
          }}
        >
          <label>
            <span>Days</span>
            <TextInput defaultValue={String(validDays)} inputMode="numeric" min={1} name="days" type="number" />
          </label>
          <Button type="submit" variant="primary">Apply</Button>
          <Button disabled={query.isFetching} onClick={() => void query.refetch()} type="button">
            {query.isFetching ? "Refreshing" : "Refresh"}
          </Button>
          <a className="ui-button ui-button--secondary ops-json-link" href={`/api/ops?days=${validDays}`}>JSON</a>
        </form>
      </div>
      {query.isLoading ? <LoadingState label="Loading ops dashboard" /> : null}
      {query.isError ? (
        <ErrorState title="Ops endpoint failed">
          {query.error instanceof Error ? query.error.message : String(query.error)}
        </ErrorState>
      ) : null}
      {query.data ? <OpsContent data={query.data} /> : null}
    </>
  );
}
