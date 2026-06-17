import { requestJson, type RequestJsonOptions } from "./http";

export interface SagaAtomListItem {
  id: string;
  content_preview: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  topics: string[];
  arousal?: number | null;
  valence?: number | null;
  encoding_confidence?: number | null;
  is_pinned?: boolean | number;
  created_at?: string;
  session_id?: string | null;
  channel_id?: string | null;
}

export interface SagaRecentResponse {
  atoms: SagaAtomListItem[];
  total: number;
  limit: number;
  channel_filter?: string | null;
  channels: string[];
  error?: string;
}

export interface SagaSearchResponse {
  atoms: SagaAtomListItem[];
  total_matched: number;
  query: string;
  channel_filter?: string | null;
  limit: number;
  error?: string;
}

export interface SagaEmbedding {
  provider: string;
  model: string;
  dim: number;
  embedded_at: string;
}

export interface SagaRelation {
  relation_type: string;
  target_id: string;
  confidence?: number | null;
  target_preview?: string | null;
}

export interface SagaAtomDetail extends Record<string, unknown> {
  id: string;
  content?: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  session_id?: string | null;
  channel_id?: string | null;
  arousal?: number | null;
  valence?: number | null;
  encoding_confidence?: number | null;
  is_pinned?: boolean | number;
  created_at?: string;
  access_count?: number;
  last_access_ts?: string | null;
  last_access_source?: string | null;
  embedding?: SagaEmbedding | null;
  relations_out?: SagaRelation[];
  topics: string[];
  metadata?: Record<string, unknown>;
  tombstoned?: boolean | number;
  tombstoned_reason?: string | null;
  error?: string;
}

export interface SagaStatsResponse {
  ready: boolean;
  atom_count?: number;
  tombstoned_count?: number;
  session_count?: number;
  triple_count?: number;
  schema_version?: number;
  db_size_bytes?: number;
  db_path?: string;
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
  never_accessed: number;
  days: number;
  error?: string;
}

export interface SagaCluster {
  cluster_id: string | null;
  size: number;
  sample_atoms: Array<{ id: string; content_preview: string }>;
}

export interface SagaClustersResponse {
  clusters: SagaCluster[];
  total_clusters: number;
  total_atoms: number;
  error?: string;
}

export interface SagaSqlResponse {
  columns?: string[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  error?: string;
  rejected?: boolean;
}

type SagaOptions = Pick<RequestJsonOptions, "apiKey" | "signal">;

export function getSagaStats(options: SagaOptions = {}): Promise<SagaStatsResponse> {
  return requestJson<SagaStatsResponse>("/api/saga", {
    ...options,
    query: { view: "stats" }
  });
}

export function listSagaAtoms(
  params: { channel?: string; limit?: number } = {},
  options: SagaOptions = {}
): Promise<SagaRecentResponse> {
  return requestJson<SagaRecentResponse>("/api/saga", {
    ...options,
    query: { view: "recent", ...params }
  });
}

export function getSagaAtom(id: string, options: SagaOptions = {}): Promise<SagaAtomDetail> {
  return requestJson<SagaAtomDetail>("/api/saga", {
    ...options,
    query: { view: "atom", id }
  });
}

export function searchSagaAtoms(
  params: { q: string; channel?: string; limit?: number },
  options: SagaOptions = {}
): Promise<SagaSearchResponse> {
  return requestJson<SagaSearchResponse>("/api/saga", {
    ...options,
    query: { view: "search", ...params }
  });
}

export function getSagaActivationHistogram(
  params: { days?: number } = {},
  options: SagaOptions = {}
): Promise<SagaActivationHistResponse> {
  return requestJson<SagaActivationHistResponse>("/api/saga", {
    ...options,
    query: { view: "activation_hist", ...params }
  });
}

export function getSagaClusters(
  params: { sample_size?: number } = {},
  options: SagaOptions = {}
): Promise<SagaClustersResponse> {
  return requestJson<SagaClustersResponse>("/api/saga", {
    ...options,
    query: { view: "clusters", ...params }
  });
}

export function runSagaSql(sql: string, options: SagaOptions = {}): Promise<SagaSqlResponse> {
  return requestJson<SagaSqlResponse>("/api/saga/sql", {
    ...options,
    method: "POST",
    body: JSON.stringify({ sql })
  });
}
