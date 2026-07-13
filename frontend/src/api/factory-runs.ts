import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  FactoryRunsData,
  FactoryRunDetail,
} from "./generated/contracts";

export type { FactoryRunsData, FactoryRunSummary, FactoryRunDetail } from "./generated/contracts";

export function getFactoryRuns(
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<FactoryRunsData>> {
  return apiFetchEnvelope<FactoryRunsData>(
    "/api/v1/factory-runs",
    options
  );
}

export function getFactoryRun(
  runId: string,
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<FactoryRunDetail>> {
  return apiFetchEnvelope<FactoryRunDetail>(
    `/api/v1/factory-runs/${encodeURIComponent(runId)}`,
    options
  );
}
