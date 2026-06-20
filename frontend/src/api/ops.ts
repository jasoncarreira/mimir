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
export type OpsUsagePoint = OpsDashboardData["usage_history"][string][string][number];
export type OpsUsageHistory = OpsDashboardData["usage_history"];
export type OpsTokenUsagePoint = OpsDashboardData["token_usage_history"][number];
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
