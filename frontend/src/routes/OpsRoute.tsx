import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getOpsDashboard } from "../api";
import { drilldownHref } from "../routeState";
import {
  Button,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";
import type { OpsUsagePoint } from "../api/ops";
import type { OpsTokenUsageRow } from "./opsViewModel";
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

const usageProviderLabels: Record<string, string> = {
  anthropic: "Anthropic Max (OAuth)",
  minimax: "Minimax",
  codex_plus: "Codex Plus"
};

const usageWindowColors: Record<string, string> = {
  five_hour: "#6c8ef7",
  seven_day: "#fbbf24",
  seven_day_sonnet: "#10b981",
  seven_day_omelette: "#a78bfa",
  seven_day_opus: "#f472b6"
};

const tokenLayerColors: Record<string, string> = {
  input: "#6c8ef7",
  cacheCreation: "#fbbf24",
  cacheRead: "#10b981",
  output: "#f472b6"
};

function compactNumber(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return value.toLocaleString();
}

function formatDateLabel(value: string) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(parsed);
}

function formatTimestamp(value: string) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

// Smallest "nice" round number (1/2/2.5/5 × 10ⁿ) at or above the data max, so the
// token axis scales to the data instead of flooring at a fixed 50M.
function niceAxisMax(value: number) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const fraction = value / base;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return niceFraction * base;
}

// Split a window's samples into contiguous segments, breaking where the quota
// window resets (resets_at changes). Without this the line draws a vertical drop
// across a rollover (e.g. the seven-day series each week).
function splitOnReset(points: OpsUsagePoint[]): OpsUsagePoint[][] {
  const segments: OpsUsagePoint[][] = [];
  let current: OpsUsagePoint[] = [];
  let prevReset: number | null = null;
  for (const point of points) {
    const reset = typeof point.resets_at === "number" ? point.resets_at : null;
    if (current.length && reset != null && prevReset != null && reset !== prevReset) {
      segments.push(current);
      current = [];
    }
    current.push(point);
    if (reset != null) prevReset = reset;
  }
  if (current.length) segments.push(current);
  return segments;
}

function chartTicks(maxValue: number, steps = 4) {
  return Array.from({ length: steps + 1 }, (_, index) => maxValue - (maxValue / steps) * index);
}

function dateRangeLabel(values: string[]) {
  if (!values.length) return "";
  const first = formatDateLabel(values[0]);
  const last = formatDateLabel(values[values.length - 1]);
  return first === last ? first : `${first} → ${last}`;
}

function windowLabel(value: string) {
  return value.replaceAll("_", " ");
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

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value));
}

function QuotaTrendChart({ data }: { data: SafeOpsDashboardData }) {
  const providers = Object.entries(data.usage_history);
  if (!providers.length) {
    return (
      <EmptyState title="No subscription quota samples in this window">
        Quota charts populate from provider usage pollers and quota-mode callbacks. Token-volume charts below still work on any deployment with turn usage data.
      </EmptyState>
    );
  }

  return (
    <div className="ops-chart-grid">
      {providers.map(([provider, windows]) => {
        const series = Object.entries(windows).map(([window, points]) => ({ window, points }));
        const dates = Array.from(new Set(series.flatMap(({ points }) => points.map((point) => point.ts)))).sort();
        const latestRows = quotaRows({ [provider]: windows });
        const tsValues = dates.map((value) => Date.parse(value)).filter((value) => Number.isFinite(value));
        const minTs = tsValues.length ? Math.min(...tsValues) : 0;
        const maxTs = tsValues.length ? Math.max(...tsValues) : 0;
        const xForTs = (ts: string) => {
          const value = Date.parse(ts);
          if (!Number.isFinite(value) || maxTs <= minTs) return 50;
          return ((value - minTs) / (maxTs - minTs)) * 100;
        };
        return (
          <div className="ops-chart-card ops-chart-card--wide" key={provider}>
            <div className="ops-chart-card__header">
              <h3>{usageProviderLabels[provider] || provider}</h3>
              <span>{dateRangeLabel(dates)}</span>
            </div>
            <div className="ops-axis-chart ops-axis-chart--quota" role="img" aria-label={`${provider} quota utilization line chart with percent axis`}>
              <div className="ops-axis-chart__y" aria-hidden="true">
                {chartTicks(1).map((tick) => <span key={tick}>{formatPercent(tick)}</span>)}
              </div>
              <div className="ops-axis-chart__plot">
                <div className="ops-axis-chart__grid" aria-hidden="true">
                  {chartTicks(1).map((tick) => <span key={tick} />)}
                </div>
                <svg className="ops-quota-line-chart" preserveAspectRatio="none" viewBox="0 0 100 100" aria-hidden="true">
                  {series.map(({ window, points }) => {
                    const color = usageWindowColors[window] || "#9ca3af";
                    const valid = points.filter((point) => point.utilization != null);
                    const yFor = (point: OpsUsagePoint) => 100 - clamp01(point.utilization ?? 0) * 100;
                    return (
                      <g key={window}>
                        {splitOnReset(valid).map((segment, segmentIndex) => {
                          // A lone sample (e.g. between two resets) can't draw a
                          // line, so render a short dash to keep it visible.
                          const linePoints = segment.length === 1
                            ? `${xForTs(segment[0].ts) - 0.6},${yFor(segment[0])} ${xForTs(segment[0].ts) + 0.6},${yFor(segment[0])}`
                            : segment.map((point) => `${xForTs(point.ts)},${yFor(point)}`).join(" ");
                          return (
                            <polyline
                              className="ops-quota-line"
                              fill="none"
                              key={`${window}-${segmentIndex}`}
                              points={linePoints}
                              stroke={color}
                              vectorEffect="non-scaling-stroke"
                            />
                          );
                        })}
                      </g>
                    );
                  })}
                </svg>
                <div className="ops-quota-line-labels" aria-hidden="true">
                  {dates.length ? <span>{formatDateLabel(dates[0])}</span> : <span />}
                  {dates.length > 2 ? <span>{formatDateLabel(dates[Math.floor(dates.length / 2)])}</span> : null}
                  {dates.length > 1 ? <span>{formatDateLabel(dates[dates.length - 1])}</span> : <span />}
                </div>
              </div>
            </div>
            <div className="ops-chart-legend ops-chart-legend--cards">
              {latestRows.map((row) => (
                <span key={`${row.provider}-${row.window}`}>
                  <i style={{ backgroundColor: usageWindowColors[row.window] || "#9ca3af" }} />
                  {windowLabel(row.window)}: {formatPercent(row.latestUtilization)} · projected {formatPercent(row.latestProjection)} · {row.latestPressure}
                </span>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function TokenUsageChart({ rows }: { rows: OpsTokenUsageRow[] }) {
  if (!rows.length) {
    return <EmptyState title="No token usage rows in this window" />;
  }
  const maxTotal = Math.max(1, ...rows.map((row) => row.input + row.cacheCreation + row.cacheRead + row.output));
  const axisMax = niceAxisMax(maxTotal);
  const totalCost = rows.reduce((sum, row) => sum + (row.cost ?? 0), 0);
  const costDays = rows.filter((row) => row.cost !== null).length;
  return (
    <div className="ops-token-chart" role="img" aria-label="Daily token volume by token type with token-count axis">
      <div className="ops-axis-chart ops-axis-chart--tokens">
        <div className="ops-axis-chart__y" aria-hidden="true">
          {chartTicks(axisMax).map((tick) => <span key={tick}>{compactNumber(tick)}</span>)}
        </div>
        <div className="ops-axis-chart__plot">
          <div className="ops-axis-chart__grid" aria-hidden="true">
            {chartTicks(axisMax).map((tick) => <span key={tick} />)}
          </div>
          <div className="ops-token-chart__bars">
            {rows.map((row) => {
              const segments = [
                ["input", row.input],
                ["cacheCreation", row.cacheCreation],
                ["cacheRead", row.cacheRead],
                ["output", row.output]
              ] as const;
              const total = segments.reduce((sum, [, value]) => sum + value, 0);
              return (
                <div className="ops-token-day" key={row.date} title={`${row.date}: ${total.toLocaleString()} tokens · ${row.turns.toLocaleString()} turns`}>
                  <span className="ops-token-day__value">{compactNumber(total)}</span>
                  <div className="ops-token-day__stack" style={{ height: `${Math.max(8, (total / axisMax) * 100)}%` }}>
                    {segments.map(([key, value]) => value > 0 ? (
                      <span
                        className="ops-token-day__segment"
                        key={key}
                        style={{
                          backgroundColor: tokenLayerColors[key],
                          flexGrow: value,
                          flexBasis: 0
                        }}
                      />
                    ) : null)}
                  </div>
                  <span className="ops-token-day__label">{formatDateLabel(row.date)}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
      <div className="ops-chart-legend">
        <span><i style={{ backgroundColor: tokenLayerColors.input }} />Input</span>
        <span><i style={{ backgroundColor: tokenLayerColors.cacheCreation }} />Cache creation</span>
        <span><i style={{ backgroundColor: tokenLayerColors.cacheRead }} />Cache read</span>
        <span><i style={{ backgroundColor: tokenLayerColors.output }} />Output</span>
        {costDays > 0 ? <strong>Total cost: {formatCost(totalCost)}</strong> : null}
      </div>
    </div>
  );
}


function UsagePanel({ data }: { data: SafeOpsDashboardData }) {
  const tokens = tokenUsageRows(data.token_usage_history);

  return (
    <div className="ops-panel-stack">
      <Panel
        title="Quota Utilization"
        subtitle="Subscription-window utilization trends from the ops payload."
      >
        <QuotaTrendChart data={data} />
      </Panel>
      <Panel title="Token Usage" subtitle="Daily token/cost rollups from turns in the selected window.">
        <TokenUsageChart rows={tokens} />
        {tokens.length ? (
          <DataTable
            columns={[
              { key: "date", header: "Date" },
              { key: "turns", header: "Turns" },
              { key: "input", header: "Input" },
              { key: "cache", header: "Cache" },
              { key: "output", header: "Output" },
              { key: "total", header: "Total" },
              { key: "cost", header: "Cost" }
            ]}
            rows={tokens.map((row) => ({
              date: row.date,
              turns: row.turns.toLocaleString(),
              input: row.input.toLocaleString(),
              cache: (row.cacheCreation + row.cacheRead).toLocaleString(),
              output: row.output.toLocaleString(),
              total: compactNumber(row.input + row.cacheCreation + row.cacheRead + row.output),
              cost: formatCost(row.cost)
            }))}
          />
        ) : null}
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
    ["scheduler", "Scheduler"],
    ["async", "Async jobs"],
    ["health", "Health"],
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
          {activeTab === "scheduler" ? <SchedulerPanel data={data} /> : null}
          {activeTab === "async" ? <AsyncJobsPanel data={data} /> : null}
          {activeTab === "health" ? <HealthPanel data={data} /> : null}
          {activeTab === "raw" ? <RawPanel data={data} /> : null}
        </section>
      </div>
    </div>
  );
}


function UsageContent({ data: rawData }: { data: unknown }) {
  const data = safeOpsDashboardData(rawData);
  return (
    <div className="ops-route">
      <UsagePanel data={data} />
    </div>
  );
}

export function UsageRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const daysParam = searchParams.get("days") || "7";
  const days = Number.parseInt(daysParam, 10);
  const validDays = Number.isFinite(days) && days > 0 ? days : 7;
  const query = useQuery({
    queryKey: ["usage-dashboard", validDays],
    queryFn: async () => (await getOpsDashboard({ days: validDays }, { cache: "no-store" })).data
  });

  return (
    <>
      <div className="ops-header-row">
        <div>
          <p className="ui-eyebrow">Usage</p>
          <h1>Usage Dashboard</h1>
          <p className="app-copy">
            Window: {validDays} days{query.data ? ` | Generated ${formatTimestamp(query.data.generated_at)}` : ""}
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
      {query.isLoading ? <LoadingState label="Loading usage dashboard" /> : null}
      {query.isError ? (
        <ErrorState title="Usage endpoint failed">
          {query.error instanceof Error ? query.error.message : String(query.error)}
        </ErrorState>
      ) : null}
      {query.data ? <UsageContent data={query.data} /> : null}
    </>
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
          <p className="ui-eyebrow">Ops</p>
          <h1>Ops Dashboard</h1>
          <p className="app-copy">
            Window: {validDays} days{query.data ? ` | Generated ${formatTimestamp(query.data.generated_at)}` : ""}
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
