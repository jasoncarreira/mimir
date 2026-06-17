import type { ApiClient } from "./http";
import { withQuery } from "./http";

export interface SagaAtomListItem {
  id: string;
  content_preview: string;
  memory_type?: string;
  stream?: string;
  source_type?: string;
  topics?: unknown[];
  arousal?: number;
  valence?: number;
  encoding_confidence?: number;
  is_pinned?: number | boolean;
  created_at?: string;
  session_id?: string | null;
  channel_id?: string | null;
}

export interface SagaRecentResponse {
  atoms: SagaAtomListItem[];
  total: number;
  limit: number;
  channel_filter?: string | null;
  channels?: string[];
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

export interface SagaEmbeddingInfo {
  provider: string;
  model: string;
  dim: number;
  embedded_at: string;
}

export interface SagaRelation {
  relation_type: string;
  target_id: string;
  confidence?: number;
  target_preview?: string | null;
}

export interface SagaAtomDetails {
  id: string;
  content?: string;
  topics?: unknown[];
  metadata?: Record<string, unknown>;
  access_count?: number;
  last_access_ts?: string | null;
  last_access_source?: string | null;
  embedding?: SagaEmbeddingInfo | null;
  relations_out?: SagaRelation[];
  channel_id?: string | null;
  session_started_at?: string | null;
  [key: string]: unknown;
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

export interface SagaSqlRequest {
  sql: string;
}

export interface SagaSqlResponse {
  columns?: string[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  rejected?: boolean;
  error?: string;
}

export function createSagaClient(api: ApiClient) {
  return {
    recent(params: { channel?: string; limit?: number } = {}) {
      return api.requestJson<SagaRecentResponse>(
        withQuery("/api/saga", { view: "recent", ...params })
      );
    },

    atom(id: string) {
      return api.requestJson<SagaAtomDetails>(
        withQuery("/api/saga", { view: "atom", id })
      );
    },

    stats() {
      return api.requestJson<SagaStatsResponse>(
        withQuery("/api/saga", { view: "stats" })
      );
    },

    search(params: { q: string; channel?: string; limit?: number }) {
      return api.requestJson<SagaSearchResponse>(
        withQuery("/api/saga", { view: "search", ...params })
      );
    },

    activationHist(days?: number) {
      return api.requestJson<SagaActivationHistResponse>(
        withQuery("/api/saga", { view: "activation_hist", days })
      );
    },

    clusters(sample_size?: number) {
      return api.requestJson<SagaClustersResponse>(
        withQuery("/api/saga", { view: "clusters", sample_size })
      );
    },

    sql(request: SagaSqlRequest) {
      return api.requestJson<SagaSqlResponse>("/api/saga/sql", {
        method: "POST",
        body: JSON.stringify(request)
      });
    }
  };
}
