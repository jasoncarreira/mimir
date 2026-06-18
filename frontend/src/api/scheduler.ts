import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  SchedulerDashboardData
} from "./generated/contracts";

export type SchedulerRunSurface = SchedulerDashboardData["schedules"][number];
export type CommitmentSurface = SchedulerDashboardData["commitments"][number];
export type SchedulerDashboardResponse = SchedulerDashboardData;

export function getSchedulerDashboard(
  params: { due_window?: string } = {},
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<SchedulerDashboardData>> {
  return apiFetchEnvelope<SchedulerDashboardData>(
    `/api/v1/scheduler${buildQuery(params)}`,
    options
  );
}
