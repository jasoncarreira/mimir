import type { ApiClient } from "./http";
import { withQuery } from "./http";

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

export interface OpsShellJobs {
  spawned: number;
  routed: number;
  no_channel: number;
  enqueue_failed: number;
  spawn_by_channel: Record<string, number>;
}

export interface OpsToolStat {
  tool: string;
  calls: number;
  errors: number;
  failure_rate: number;
  avg_duration_ms: number;
}

export interface OpsBacklogItem {
  id: string;
  title: string;
  status: string;
  blocker: string;
}

export interface ChainlinkIssueEnvelope {
  available: boolean;
  issues: Array<Record<string, unknown>>;
  error: string | null;
  truncated?: boolean;
  total_count?: number;
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
  tools: OpsToolStat[];
  failures_by_kind: Record<string, number>;
  timeseries: OpsTimeseriesPoint[];
  recent_failures: OpsFailure[];
  backlog: OpsBacklogItem[];
  chainlink_issues: ChainlinkIssueEnvelope;
  usage_history: Record<string, unknown>;
  token_usage_history: Record<string, unknown>;
}

export function createOpsClient(api: ApiClient) {
  return {
    getOps(days?: number): Promise<OpsPayload> {
      return api.requestJson<OpsPayload>(withQuery("/api/ops", { days }));
    }
  };
}
