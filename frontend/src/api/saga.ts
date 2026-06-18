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

const SQL_ALLOWED_FIRST_WORDS = new Set(["SELECT", "EXPLAIN", "WITH"]);
const SQL_WRITE_KEYWORDS_RE = /\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA)\b/i;

function positiveInteger(value: number | undefined, fallback: number, max: number) {
  if (value === undefined || !Number.isFinite(value)) return fallback;
  return Math.max(1, Math.min(Math.floor(value), max));
}

export function validateSagaAtomId(id: string): string {
  const trimmed = id.trim();
  if (!trimmed) {
    throw new Error("Atom ID is required.");
  }
  return trimmed;
}

export function validateSagaSearchQuery(query: string): string {
  const trimmed = query.trim();
  if (!trimmed) {
    throw new Error("Search query is required.");
  }
  return trimmed;
}

export function validateSagaSql(sql: string): string {
  const trimmed = sql.trim();
  if (!trimmed) {
    throw new Error("SQL statement is required.");
  }
  const firstWord = trimmed.split(/\s+/, 1)[0]?.toUpperCase();
  if (!SQL_ALLOWED_FIRST_WORDS.has(firstWord)) {
    throw new Error("Only SELECT, EXPLAIN, and WITH queries are allowed.");
  }
  const writeKeyword = SQL_WRITE_KEYWORDS_RE.exec(trimmed);
  if (writeKeyword) {
    throw new Error(`Write keyword ${writeKeyword[1].toUpperCase()} is not allowed.`);
  }
  return trimmed;
}

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
    `/api/v1/saga${buildQuery({
      view: "recent",
      channel: params.channel?.trim(),
      limit: positiveInteger(params.limit, 50, 200)
    })}`,
    options
  );
}

export function getSagaAtom(
  id: string,
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaAtomDetailData>> {
  return apiFetchEnvelope<SagaAtomDetailData>(
    `/api/v1/saga${buildQuery({ view: "atom", id: validateSagaAtomId(id) })}`,
    options
  );
}

export function searchSagaAtoms(
  params: { q: string; channel?: string; limit?: number },
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaSearchData, ListMeta>> {
  return apiFetchEnvelope<SagaSearchData, ListMeta>(
    `/api/v1/saga${buildQuery({
      view: "search",
      q: validateSagaSearchQuery(params.q),
      channel: params.channel?.trim(),
      limit: positiveInteger(params.limit, 100, 100)
    })}`,
    options
  );
}

export function getSagaActivationHistogram(
  params: { days?: number } = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaActivationHistData, ListMeta>> {
  return apiFetchEnvelope<SagaActivationHistData, ListMeta>(
    `/api/v1/saga${buildQuery({
      view: "activation_hist",
      days: positiveInteger(params.days, 7, 365)
    })}`,
    options
  );
}

export function getSagaClusters(
  params: { sample_size?: number } = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<SagaClustersData, ListMeta>> {
  return apiFetchEnvelope<SagaClustersData, ListMeta>(
    `/api/v1/saga${buildQuery({
      view: "clusters",
      sample_size: positiveInteger(params.sample_size, 3, 20)
    })}`,
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
    body: JSON.stringify({ sql: validateSagaSql(sql) })
  });
}
