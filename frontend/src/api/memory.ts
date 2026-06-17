import type { ApiClient } from "./http";
import { withQuery } from "./http";

export interface MemoryDirNode {
  name: string;
  type: "dir";
  path: string;
  desc: string | null;
  children: MemoryTreeNode[];
  error?: string;
}

export interface MemoryFileNode {
  name: string;
  type: "file";
  path: string;
  size: number;
  modified: string;
  desc: string | null;
}

export type MemoryTreeNode = MemoryDirNode | MemoryFileNode;

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

export function createMemoryClient(api: ApiClient) {
  return {
    tree() {
      return api.requestJson<MemoryDirNode>(
        withQuery("/api/memory", { view: "tree" })
      );
    },

    file(path: string) {
      return api.requestJson<MemoryFileResponse>(
        withQuery("/api/memory", { view: "file", path })
      );
    },

    search(q: string) {
      return api.requestJson<MemorySearchResponse>(
        withQuery("/api/memory", { view: "search", q })
      );
    },

    channels() {
      return api.requestJson<MemoryChannelsResponse>(
        withQuery("/api/memory", { view: "channels" })
      );
    }
  };
}
