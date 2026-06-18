import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  OpsDashboardData
} from "./generated/contracts";

export type OpsSummary = OpsDashboardData["summary"];
export type OpsToolStats = OpsDashboardData["tools"][number];
export type OpsShellJobs = OpsDashboardData["shell_jobs"];
export type OpsTimeseriesPoint = OpsDashboardData["timeseries"][number];
export type OpsRecentFailure = OpsDashboardData["recent_failures"][number];
export type OpsBacklogItem = OpsDashboardData["backlog"][number];
export type OpsUsagePoint = { ts: string; utilization: number | null; [key: string]: unknown };
export type OpsUsageHistory = OpsDashboardData["usage_history"];
export type OpsTokenUsagePoint = {
  date: string;
  turn_count: number;
  input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
  total_cost_usd: number | null;
};
export type ChainlinkIssue = Record<string, unknown>;
export type ChainlinkIssuesEnvelope = OpsDashboardData["chainlink_issues"];
export type OpsDashboardResponse = OpsDashboardData;

export function getOpsDashboard(
  params: { days?: number } = {},
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<OpsDashboardData>> {
  return apiFetchEnvelope<OpsDashboardData>(
    `/api/v1/ops${buildQuery(params)}`,
    options
  );
}
