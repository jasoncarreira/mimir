import { apiFetchJson, buildQuery, type ApiClientOptions } from "./http";

export interface MemoryTreeDir {
  name: string;
  type: "dir";
  path: string;
  desc: string | null;
  children: MemoryTreeNode[];
  error?: string;
}

export interface MemoryTreeFile {
  name: string;
  type: "file";
  path: string;
  size: number;
  modified: string;
  desc: string | null;
}

export type MemoryTreeNode = MemoryTreeDir | MemoryTreeFile;

export interface MemoryFileResponse {
  path: string;
  content: string;
  size: number;
  modified: string;
  error?: string;
}

export interface MemorySearchHit {
  path: string;
  line_no: number;
  snippet: string;
}

export interface MemorySearchResponse {
  query: string;
  hits: MemorySearchHit[];
  total: number;
  truncated: boolean;
}

export interface MemoryChannelsResponse {
  channels: string[];
}

export function getMemoryTree(
  options?: ApiClientOptions
): Promise<MemoryTreeDir> {
  return apiFetchJson<MemoryTreeDir>("/api/memory?view=tree", options);
}

export function getMemoryFile(
  path: string,
  options?: ApiClientOptions
): Promise<MemoryFileResponse> {
  return apiFetchJson<MemoryFileResponse>(
    `/api/memory${buildQuery({ view: "file", path })}`,
    options
  );
}

export function searchMemoryFiles(
  q: string,
  options?: ApiClientOptions
): Promise<MemorySearchResponse> {
  return apiFetchJson<MemorySearchResponse>(
    `/api/memory${buildQuery({ view: "search", q })}`,
    options
  );
}

export function getMemoryChannels(
  options?: ApiClientOptions
): Promise<MemoryChannelsResponse> {
  return apiFetchJson<MemoryChannelsResponse>(
    "/api/memory?view=channels",
    options
  );
}
