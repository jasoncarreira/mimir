import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ListMeta,
  SagaActivationHistData,
  SagaAtomDetailData,
  SagaAtomSummary,
  SagaClustersData,
  SagaRecentData,
  SagaSearchData,
  SagaSqlCell,
  SagaSqlData,
  SagaStatsData
} from "./generated/contracts";

export type { SagaAtomSummary, SagaSqlCell };

export type SagaRecentResponse = SagaRecentData & {
  total: number;
  limit: number;
  error?: string;
};
export type SagaStatsResponse = SagaStatsData & { error?: string };
export type SagaEmbedding = {
  provider: string;
  model: string;
  dim: number;
  embedded_at?: string | null;
};
export type SagaRelation = {
  relation_type: string;
  target_id: string;
  confidence?: number | null;
  target_preview?: string | null;
};
export type SagaAtomDetail = SagaAtomDetailData & {
  error?: string;
  embedding?: SagaEmbedding | null;
  relations_out?: SagaRelation[];
};
export type SagaSearchResponse = SagaSearchData & {
  total_matched: number;
  limit: number;
  error?: string;
};
export type SagaActivationBucket = SagaActivationHistData["buckets"][number];
export type SagaActivationHistResponse = SagaActivationHistData & {
  total: number;
  error?: string;
};
export type SagaCluster = SagaClustersData["clusters"][number];
export type SagaClustersResponse = SagaClustersData & {
  total_clusters?: number;
  total_atoms?: number;
  error?: string;
};
export type SagaSqlResponse = SagaSqlData & {
  truncated?: boolean;
  error?: string;
};

export function getSagaStats(
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaStatsData>> {
  return apiFetchEnvelope<SagaStatsData>("/api/v1/saga?view=stats", options);
}

export function listSagaAtoms(
  params: { channel?: string; limit?: number } = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaRecentData, ListMeta>> {
  return apiFetchEnvelope<SagaRecentData, ListMeta>(
    `/api/v1/saga${buildQuery({ view: "recent", ...params })}`,
    options
  );
}

export function getSagaAtom(
  id: string,
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaAtomDetailData>> {
  return apiFetchEnvelope<SagaAtomDetailData>(
    `/api/v1/saga${buildQuery({ view: "atom", id })}`,
    options
  );
}

export function searchSagaAtoms(
  params: { q: string; channel?: string; limit?: number },
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaSearchData, ListMeta>> {
  return apiFetchEnvelope<SagaSearchData, ListMeta>(
    `/api/v1/saga${buildQuery({ view: "search", ...params })}`,
    options
  );
}

export function getSagaActivationHistogram(
  params: { days?: number } = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaActivationHistData, ListMeta>> {
  return apiFetchEnvelope<SagaActivationHistData, ListMeta>(
    `/api/v1/saga${buildQuery({ view: "activation_hist", ...params })}`,
    options
  );
}

export function getSagaClusters(
  params: { sample_size?: number } = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaClustersData, ListMeta>> {
  return apiFetchEnvelope<SagaClustersData, ListMeta>(
    `/api/v1/saga${buildQuery({ view: "clusters", ...params })}`,
    options
  );
}

export function runSagaSql(
  sql: string,
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaSqlData, ListMeta>> {
  const headers = new Headers(options?.headers);
  headers.set("Content-Type", "application/json");
  return apiFetchEnvelope<SagaSqlData, ListMeta>("/api/v1/saga/sql", {
    ...options,
    method: "POST",
    headers,
    body: JSON.stringify({ sql })
  });
}
