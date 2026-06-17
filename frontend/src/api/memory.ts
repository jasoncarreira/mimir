import { requestJson, type RequestJsonOptions } from "./http";

export interface MemoryTreeNodeBase {
  name: string;
  path: string;
  desc: string | null;
}

export interface MemoryDirNode extends MemoryTreeNodeBase {
  type: "dir";
  children: MemoryTreeNode[];
}

export interface MemoryFileNode extends MemoryTreeNodeBase {
  type: "file";
  size: number;
  modified: string;
}

export type MemoryTreeNode = MemoryDirNode | MemoryFileNode;

export interface MemoryTreeResponse extends MemoryDirNode {
  error?: string;
}

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
  error?: string;
}

export interface MemoryChannelsResponse {
  channels: string[];
}

type MemoryOptions = Pick<RequestJsonOptions, "apiKey" | "signal">;

export function getMemoryTree(options: MemoryOptions = {}): Promise<MemoryTreeResponse> {
  return requestJson<MemoryTreeResponse>("/api/memory", {
    ...options,
    query: { view: "tree" }
  });
}

export function getMemoryFile(
  path: string,
  options: MemoryOptions = {}
): Promise<MemoryFileResponse> {
  return requestJson<MemoryFileResponse>("/api/memory", {
    ...options,
    query: { view: "file", path }
  });
}

export function searchMemory(
  q: string,
  options: MemoryOptions = {}
): Promise<MemorySearchResponse> {
  return requestJson<MemorySearchResponse>("/api/memory", {
    ...options,
    query: { view: "search", q }
  });
}

export function listMemoryChannels(
  options: MemoryOptions = {}
): Promise<MemoryChannelsResponse> {
  return requestJson<MemoryChannelsResponse>("/api/memory", {
    ...options,
    query: { view: "channels" }
  });
}
