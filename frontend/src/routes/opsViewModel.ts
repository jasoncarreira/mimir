import type { OpsDashboardResponse } from "../api/ops";

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
  latestProjection: number | null;
  latestPressure: string;
  latestResetAt: number | null;
};

export type OpsAlgedonicSignals = {
  title: string;
  windowHours: number;
  block: string;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function recordFrom(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function numericRecordFrom(value: unknown): Record<string, number> {
  return Object.fromEntries(
    Object.entries(recordFrom(value))
      .filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]))
  );
}

function recordArrayFrom(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function numberFrom(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function nullableNumberFrom(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringFrom(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function formatPercent(value: number | null) {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "n/a";
}

export function formatCost(value: number | null) {
  return typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(4)}` : "n/a";
}

export function buildOpsSummaryMetrics(summary: unknown): OpsMetric[] {
  return Object.entries(numericRecordFrom(summary)).map(([key, value]) => ({
    key,
    label: formatOpsLabel(key),
    value,
    tone: dangerousSummaryKeys.has(key) && value > 0 ? "danger" : "neutral"
  }));
}

export function mapToRows(source: unknown): OpsMapRow[] {
  return Object.entries(numericRecordFrom(source))
    .map(([key, value]) => ({ key, value }))
    .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
}

export function tokenUsageRows(
  points: unknown
): OpsTokenUsageRow[] {
  return (Array.isArray(points) ? points : [])
    .filter(isRecord)
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
  history: unknown
): OpsQuotaRow[] {
  return Object.entries(recordFrom(history)).flatMap(([provider, windows]) =>
    Object.entries(recordFrom(windows)).map(([window, rawPoints]) => {
      const points = Array.isArray(rawPoints) ? rawPoints : [];
      const latest = [...points].reverse().find(isRecord);
      return {
        provider,
        window,
        points: points.length,
        latestUtilization: latest ? nullableNumberFrom(latest.utilization) : null,
        latestProjection: latest ? nullableNumberFrom(latest.projection) : null,
        latestPressure: latest ? stringFrom(latest.pressure, "clear") : "clear",
        latestResetAt: latest ? nullableNumberFrom(latest.resets_at) : null
      };
    })
  );
}

export function schedulerEventRows(data: Pick<SafeOpsDashboardData, "by_event">): OpsMapRow[] {
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

export type SafeOpsDashboardData = Omit<OpsDashboardResponse,
  | "summary"
  | "by_event"
  | "queued_by_trigger"
  | "queued_by_channel"
  | "resolution_paths"
  | "shell_jobs"
  | "tools"
  | "failures_by_kind"
  | "timeseries"
  | "recent_failures"
  | "backlog"
  | "chainlink_issues"
  | "usage_history"
  | "token_usage_history"
  | "algedonic_signals"
> & {
  summary: Record<string, number>;
  by_event: Record<string, number>;
  queued_by_trigger: Record<string, number>;
  queued_by_channel: Record<string, number>;
  resolution_paths: Record<string, Record<string, number>>;
  shell_jobs: {
    spawned: number;
    routed: number;
    no_channel: number;
    enqueue_failed: number;
    spawn_by_channel: Record<string, number>;
  };
  tools: Array<{
    tool: string;
    calls: number;
    errors: number;
    failure_rate: number | null;
    avg_duration_ms: number;
  }>;
  failures_by_kind: Record<string, number>;
  timeseries: Array<{ day: string; events: number; queued: number }>;
  recent_failures: Array<{
    t: string;
    kind: string;
    channel_id?: string | null;
    trigger?: string | null;
    detail: string;
  }>;
  backlog: Array<{ id: string; title: string; status: string; blocker: string }>;
  chainlink_issues: {
    available: boolean;
    issues: Array<Record<string, unknown>>;
    error?: string | null;
    truncated?: boolean;
    total_count?: number;
  };
  usage_history: OpsDashboardResponse["usage_history"];
  token_usage_history: OpsDashboardResponse["token_usage_history"];
  algedonic_signals: OpsAlgedonicSignals;
};

export function safeOpsDashboardData(data: unknown): SafeOpsDashboardData {
  const source = recordFrom(data);
  const shellJobs = recordFrom(source.shell_jobs);
  const chainlinkIssues = recordFrom(source.chainlink_issues);
  const algedonicSignals = recordFrom(source.algedonic_signals);
  return {
    ...(source as unknown as OpsDashboardResponse),
    generated_at: stringFrom(source.generated_at),
    window_days: numberFrom(source.window_days),
    summary: numericRecordFrom(source.summary),
    by_event: numericRecordFrom(source.by_event),
    queued_by_trigger: numericRecordFrom(source.queued_by_trigger),
    queued_by_channel: numericRecordFrom(source.queued_by_channel),
    resolution_paths: Object.fromEntries(
      Object.entries(recordFrom(source.resolution_paths)).map(([kind, paths]) => [kind, numericRecordFrom(paths)])
    ),
    shell_jobs: {
      spawned: numberFrom(shellJobs.spawned),
      routed: numberFrom(shellJobs.routed),
      no_channel: numberFrom(shellJobs.no_channel),
      enqueue_failed: numberFrom(shellJobs.enqueue_failed),
      spawn_by_channel: numericRecordFrom(shellJobs.spawn_by_channel)
    },
    tools: recordArrayFrom(source.tools).map((tool) => ({
      tool: stringFrom(tool.tool, "unknown"),
      calls: numberFrom(tool.calls),
      errors: numberFrom(tool.errors),
      failure_rate: nullableNumberFrom(tool.failure_rate),
      avg_duration_ms: numberFrom(tool.avg_duration_ms)
    })),
    failures_by_kind: numericRecordFrom(source.failures_by_kind),
    timeseries: recordArrayFrom(source.timeseries).map((point) => ({
      day: stringFrom(point.day),
      events: numberFrom(point.events),
      queued: numberFrom(point.queued)
    })).filter((point) => point.day),
    recent_failures: recordArrayFrom(source.recent_failures).map((failure, index) => ({
      t: stringFrom(failure.t),
      kind: stringFrom(failure.kind, "unknown"),
      channel_id: typeof failure.channel_id === "string" ? failure.channel_id : null,
      trigger: typeof failure.trigger === "string" ? failure.trigger : null,
      detail: stringFrom(failure.detail, `Failure ${index + 1}`)
    })),
    backlog: recordArrayFrom(source.backlog).map((item, index) => ({
      id: stringFrom(item.id, `backlog-${index}`),
      title: stringFrom(item.title, "Untitled backlog item"),
      status: stringFrom(item.status, "Unknown"),
      blocker: stringFrom(item.blocker)
    })),
    chainlink_issues: {
      available: typeof chainlinkIssues.available === "boolean" ? chainlinkIssues.available : false,
      issues: recordArrayFrom(chainlinkIssues.issues),
      error: typeof chainlinkIssues.error === "string" ? chainlinkIssues.error : null,
      truncated: typeof chainlinkIssues.truncated === "boolean" ? chainlinkIssues.truncated : false,
      total_count: typeof chainlinkIssues.total_count === "number" ? chainlinkIssues.total_count : undefined
    },
    usage_history: Object.fromEntries(
      Object.entries(recordFrom(source.usage_history)).map(([provider, windows]) => [provider, recordFrom(windows)])
    ) as OpsDashboardResponse["usage_history"],
    token_usage_history: Array.isArray(source.token_usage_history)
      ? source.token_usage_history as OpsDashboardResponse["token_usage_history"]
      : [],
    algedonic_signals: {
      title: stringFrom(algedonicSignals.title, "Recent feedback signals"),
      windowHours: numberFrom(algedonicSignals.window_hours) || 24,
      block: stringFrom(algedonicSignals.block)
    }
  };
}
