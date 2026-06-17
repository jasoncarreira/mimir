import { requestJson, type RequestJsonOptions } from "./http";

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
}

export interface OpsTimeseriesPoint {
  day: string;
  events: number;
  queued: number;
}

export interface OpsFailure {
  t: string;
  kind: string;
  channel_id?: string | null;
  trigger?: string | null;
  detail: string;
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

export interface OpsBacklogItem {
  id: string;
  title: string;
  status: string;
  blocker: string;
}

export interface ChainlinkIssue extends Record<string, unknown> {
  id?: number | string;
  number?: number | string;
  title?: string;
  status?: string;
  priority?: string;
  parent?: number | string | null;
  updated_at?: string;
}

export interface ChainlinkIssuesEnvelope {
  available: boolean;
  issues: ChainlinkIssue[];
  error: string | null;
  truncated?: boolean;
  total_count?: number;
}

export interface UsageHistoryPoint extends Record<string, unknown> {
  timestamp?: string;
  used_percent?: number;
  remaining_percent?: number;
}

export interface TokenUsageHistoryPoint extends Record<string, unknown> {
  day?: string;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
}

export interface OpsPayload {
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
  recent_failures: OpsFailure[];
  backlog: OpsBacklogItem[];
  chainlink_issues: ChainlinkIssuesEnvelope;
  usage_history: Record<string, UsageHistoryPoint[]>;
  token_usage_history: TokenUsageHistoryPoint[];
}

export interface GetOpsParams {
  days?: number;
}

export function getOpsSummary(
  params: GetOpsParams = {},
  options: Pick<RequestJsonOptions, "apiKey" | "signal"> = {}
): Promise<OpsPayload> {
  return requestJson<OpsPayload>("/api/ops", {
    ...options,
    query: { days: params.days }
  });
}
