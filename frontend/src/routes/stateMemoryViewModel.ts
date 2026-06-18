import type { MemoryFileData, MemorySearchHit, MemoryTreeFile, MemoryTreeNode } from "../api";

export function sourceLayerForPath(path: string) {
  if (path.startsWith("state/")) return "state";
  if (path.startsWith("memory/core/")) return "core memory";
  if (path.startsWith("memory/")) return "non-core memory";
  return "unknown";
}

export function flattenFiles(node: MemoryTreeNode): MemoryTreeFile[] {
  if (node.type === "file") return [node];
  return node.children.flatMap(flattenFiles);
}

export function fmtBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtTimestamp(value?: string) {
  if (!value) return "unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function descriptionFromContent(content?: string) {
  const firstLine = content?.split(/\r?\n/, 1)[0] ?? "";
  const match = firstLine.match(/^<!--\s*desc:\s*(.*?)\s*-->$/i);
  return match?.[1] ?? "";
}

export function defaultMemoryPath(files: MemoryTreeFile[]) {
  return files.find((file) => file.path === "memory/INDEX.md")?.path ?? files[0]?.path ?? "";
}

export function countByLayer(files: MemoryTreeFile[]) {
  return {
    state: files.filter((file) => file.path.startsWith("state/")).length,
    memory: files.filter((file) => file.path.startsWith("memory/")).length
  };
}

export function searchResultsCaption(hits: MemorySearchHit[], total?: number | null, truncated?: boolean) {
  return `${total ?? hits.length} result(s)${truncated ? " (truncated)" : ""}`;
}

export type { MemoryFileData, MemorySearchHit, MemoryTreeFile, MemoryTreeNode };
