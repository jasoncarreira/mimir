import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  FactoryRunArchiveData,
  FactoryRunArchiveRequest,
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

export function archiveFactoryRun(
  runId: string,
  input: FactoryRunArchiveRequest,
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<FactoryRunArchiveData>> {
  return apiFetchEnvelope<FactoryRunArchiveData>(
    `/api/v1/factory-runs/${encodeURIComponent(runId)}/archive`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
      ...options
    }
  );
}
