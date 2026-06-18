import { apiFetchJson, buildQuery, type ApiClientOptions } from "./http";

export interface OpsSummary {
  total_events: number;
  events_queued: number;
  messages_sent: number;
  subagents_started: number;
  subagents_completed: number;
  shell_jobs_spawned: number;
  shell_jobs_routed: number;
  failures: number;
  high_water_events: number;
  client_pool_drains: number;
  tool_calls: number;
  tool_errors: number;
  [key: string]: number;
}

export interface OpsToolStats {
  tool: string;
  calls: number;
  errors: number;
  failure_rate: number;
  avg_duration_ms: number;
}

export interface OpsShellJobs {
  spawned: number;
  routed: number;
  no_channel: number;
  enqueue_failed: number;
  spawn_by_channel: Record<string, number>;
}

export interface OpsTimeseriesPoint {
  day: string;
  events: number;
  queued: number;
}

export interface OpsRecentFailure {
  t: string;
  kind: string;
  channel_id?: string | null;
  trigger?: string | null;
  detail: string;
}

export interface OpsBacklogItem {
  id: string;
  title: string;
  status: string;
  blocker: string;
}

export interface OpsUsagePoint {
  ts: string;
  utilization: number | null;
  [key: string]: unknown;
}

export type OpsUsageHistory = Record<string, Record<string, OpsUsagePoint[]>>;

export interface OpsTokenUsagePoint {
  date: string;
  turn_count: number;
  input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
  total_cost_usd: number | null;
}

export interface ChainlinkIssue {
  id?: number | string;
  title?: string;
  status?: string;
  priority?: string;
  parent_id?: number | string | null;
  updated_at?: string;
  [key: string]: unknown;
}

export interface ChainlinkIssuesEnvelope {
  available: boolean;
  issues: ChainlinkIssue[];
  error?: string | null;
  truncated?: boolean;
  total_count?: number;
}

export interface OpsDashboardResponse {
  generated_at: string;
  window_days: number;
  summary: OpsSummary;
  by_event: Record<string, number>;
  queued_by_trigger: Record<string, number>;
  queued_by_channel: Record<string, number>;
  resolution_paths: Record<string, Record<string, number>>;
  shell_jobs: OpsShellJobs;
  tools: OpsToolStats[];
  failures_by_kind: Record<string, number>;
  timeseries: OpsTimeseriesPoint[];
  recent_failures: OpsRecentFailure[];
  backlog: OpsBacklogItem[];
  chainlink_issues: ChainlinkIssuesEnvelope;
  usage_history: OpsUsageHistory;
  token_usage_history: OpsTokenUsagePoint[];
}

export function getOpsDashboard(
  params: { days?: number } = {},
  options?: ApiClientOptions
): Promise<OpsDashboardResponse> {
  return apiFetchJson<OpsDashboardResponse>(
    `/api/ops${buildQuery(params)}`,
    options
  );
}
