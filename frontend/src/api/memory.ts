import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ListMeta,
  MemoryChannelsData,
  MemoryFileData,
  MemorySearchData,
  MemorySearchHit,
  MemoryTreeDir,
  MemoryTreeFile,
  MemoryTreeNode
} from "./generated/contracts";

export type {
  MemorySearchHit,
  MemoryTreeDir,
  MemoryTreeFile,
  MemoryTreeNode
};

export type MemoryFileResponse = MemoryFileData & { error?: string };
export type MemorySearchResponse = MemorySearchData & {
  total: number;
  truncated: boolean;
};
export type MemoryChannelsResponse = MemoryChannelsData;

export function getMemoryTree(
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<MemoryTreeDir>> {
  return apiFetchEnvelope<MemoryTreeDir>("/api/v1/memory?view=tree", options);
}

export function getMemoryFile(
  path: string,
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<MemoryFileData>> {
  return apiFetchEnvelope<MemoryFileData>(
    `/api/v1/memory${buildQuery({ view: "file", path })}`,
    options
  );
}

export function searchMemoryFiles(
  q: string,
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<MemorySearchData, ListMeta>> {
  return apiFetchEnvelope<MemorySearchData, ListMeta>(
    `/api/v1/memory${buildQuery({ view: "search", q })}`,
    options
  );
}

export function getMemoryChannels(
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<MemoryChannelsData, ListMeta>> {
  return apiFetchEnvelope<MemoryChannelsData, ListMeta>(
    "/api/v1/memory?view=channels",
    options
  );
}
