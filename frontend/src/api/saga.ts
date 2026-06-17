import { apiFetchJson, buildQuery, type ApiClientOptions } from "./http";

export interface SagaAtomSummary {
  id: string;
  content_preview: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  topics?: string[];
  arousal?: number | null;
  valence?: number | null;
  encoding_confidence?: number | null;
  is_pinned?: boolean | number;
  created_at?: string | null;
  session_id?: string | null;
  channel_id?: string | null;
}

export interface SagaRecentResponse {
  atoms: SagaAtomSummary[];
  total: number;
  limit: number;
  channel_filter?: string | null;
  channels: string[];
  error?: string;
}

export interface SagaStatsResponse {
  ready: boolean;
  atom_count?: number;
  tombstoned_count?: number;
  session_count?: number;
  triple_count?: number;
  schema_version?: number | null;
  db_size_bytes?: number;
  db_path?: string;
  error?: string;
}

export interface SagaEmbedding {
  provider: string;
  model: string;
  dim: number;
  embedded_at?: string | null;
}

export interface SagaRelation {
  relation_type: string;
  target_id: string;
  confidence?: number | null;
  target_preview?: string | null;
}

export interface SagaAtomDetail {
  id: string;
  content?: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  topics?: string[];
  metadata?: Record<string, unknown>;
  arousal?: number | null;
  valence?: number | null;
  encoding_confidence?: number | null;
  is_pinned?: boolean | number;
  created_at?: string | null;
  session_id?: string | null;
  channel_id?: string | null;
  session_started_at?: string | null;
  access_count?: number;
  last_access_ts?: string | null;
  last_access_source?: string | null;
  embedding?: SagaEmbedding | null;
  relations_out?: SagaRelation[];
  tombstoned?: boolean | number;
  tombstoned_reason?: string | null;
  error?: string;
  [key: string]: unknown;
}

export interface SagaSearchResponse {
  atoms: SagaAtomSummary[];
  total_matched: number;
  query: string;
  channel_filter?: string | null;
  limit: number;
  error?: string;
}

export interface SagaActivationBucket {
  range_start: number;
  range_end: number;
  count: number;
}

export interface SagaActivationHistResponse {
  buckets: SagaActivationBucket[];
  total: number;
  never_accessed?: number;
  days?: number;
  error?: string;
}

export interface SagaCluster {
  cluster_id: string | null;
  size: number;
  sample_atoms: Array<{ id: string; content_preview: string }>;
}

export interface SagaClustersResponse {
  clusters: SagaCluster[];
  total_clusters?: number;
  total_atoms?: number;
  error?: string;
}

export type SagaSqlCell = string | number | boolean | null;

export interface SagaSqlResponse {
  columns?: string[];
  rows?: SagaSqlCell[][];
  row_count?: number;
  truncated?: boolean;
  error?: string;
  rejected?: boolean;
}

export function getSagaStats(
  options?: ApiClientOptions
): Promise<SagaStatsResponse> {
  return apiFetchJson<SagaStatsResponse>("/api/saga?view=stats", options);
}

export function listSagaAtoms(
  params: { channel?: string; limit?: number } = {},
  options?: ApiClientOptions
): Promise<SagaRecentResponse> {
  return apiFetchJson<SagaRecentResponse>(
    `/api/saga${buildQuery({ view: "recent", ...params })}`,
    options
  );
}

export function getSagaAtom(
  id: string,
  options?: ApiClientOptions
): Promise<SagaAtomDetail> {
  return apiFetchJson<SagaAtomDetail>(
    `/api/saga${buildQuery({ view: "atom", id })}`,
    options
  );
}

export function searchSagaAtoms(
  params: { q: string; channel?: string; limit?: number },
  options?: ApiClientOptions
): Promise<SagaSearchResponse> {
  return apiFetchJson<SagaSearchResponse>(
    `/api/saga${buildQuery({ view: "search", ...params })}`,
    options
  );
}

export function getSagaActivationHistogram(
  params: { days?: number } = {},
  options?: ApiClientOptions
): Promise<SagaActivationHistResponse> {
  return apiFetchJson<SagaActivationHistResponse>(
    `/api/saga${buildQuery({ view: "activation_hist", ...params })}`,
    options
  );
}

export function getSagaClusters(
  params: { sample_size?: number } = {},
  options?: ApiClientOptions
): Promise<SagaClustersResponse> {
  return apiFetchJson<SagaClustersResponse>(
    `/api/saga${buildQuery({ view: "clusters", ...params })}`,
    options
  );
}

export function runSagaSql(
  sql: string,
  options?: RequestInit & ApiClientOptions
): Promise<SagaSqlResponse> {
  const headers = new Headers(options?.headers);
  headers.set("Content-Type", "application/json");
  return apiFetchJson<SagaSqlResponse>("/api/saga/sql", {
    ...options,
    method: "POST",
    headers,
    body: JSON.stringify({ sql })
  });
}
