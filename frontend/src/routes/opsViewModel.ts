import type { OpsDashboardData } from "../api/generated/contracts";

export type OpsMetric = {
  key: string;
  label: string;
  value: number;
  tone: "neutral" | "danger";
};

export type OpsMapRow = {
  key: string;
  value: number;
};

export type OpsTokenUsageRow = {
  date: string;
  turns: number;
  input: number;
  cacheCreation: number;
  cacheRead: number;
  output: number;
  cost: number | null;
};

export type OpsQuotaRow = {
  provider: string;
  window: string;
  points: number;
  latestUtilization: number | null;
};

const summaryLabels: Record<string, string> = {
  total_events: "Total events",
  events_queued: "Events queued",
  messages_sent: "Messages sent",
  subagents_started: "Subagents started",
  subagents_completed: "Subagents completed",
  shell_jobs_spawned: "Shell jobs spawned",
  shell_jobs_routed: "Shell jobs routed",
  failures: "Failures",
  high_water_events: "Queue high-water hits",
  client_pool_drains: "Pool drains",
  tool_calls: "Tool calls",
  tool_errors: "Tool errors"
};

const dangerousSummaryKeys = new Set(["failures", "high_water_events", "tool_errors"]);

export function formatOpsLabel(key: string): string {
  return summaryLabels[key] ?? key.replaceAll("_", " ");
}

export function buildOpsSummaryMetrics(summary: OpsDashboardData["summary"]): OpsMetric[] {
  return Object.entries(summary).map(([key, value]) => ({
    key,
    label: formatOpsLabel(key),
    value,
    tone: dangerousSummaryKeys.has(key) && value > 0 ? "danger" : "neutral"
  }));
}

export function mapToRows(source: Record<string, number>): OpsMapRow[] {
  return Object.entries(source)
    .map(([key, value]) => ({ key, value }))
    .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
}

function numberFrom(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function nullableNumberFrom(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function tokenUsageRows(
  points: OpsDashboardData["token_usage_history"]
): OpsTokenUsageRow[] {
  return points
    .filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
    .map((point) => ({
      date: String(point.date ?? ""),
      turns: numberFrom(point.turn_count),
      input: numberFrom(point.input_tokens),
      cacheCreation: numberFrom(point.cache_creation_input_tokens),
      cacheRead: numberFrom(point.cache_read_input_tokens),
      output: numberFrom(point.output_tokens),
      cost: nullableNumberFrom(point.total_cost_usd)
    }))
    .filter((row) => row.date);
}

export function quotaRows(
  history: OpsDashboardData["usage_history"]
): OpsQuotaRow[] {
  return Object.entries(history).flatMap(([provider, windows]) =>
    Object.entries(windows).map(([window, rawPoints]) => {
      const points = Array.isArray(rawPoints) ? rawPoints : [];
      const latest = [...points]
        .reverse()
        .find((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object");
      return {
        provider,
        window,
        points: points.length,
        latestUtilization: latest ? nullableNumberFrom(latest.utilization) : null
      };
    })
  );
}

export function schedulerEventRows(data: OpsDashboardData): OpsMapRow[] {
  const schedulerLike = Object.fromEntries(
    Object.entries(data.by_event).filter(([key]) =>
      key.includes("scheduler")
      || key.includes("poller")
      || key.includes("job")
      || key.includes("queued")
      || key.includes("resource")
      || key.includes("quota")
    )
  );
  return mapToRows(schedulerLike);
}
