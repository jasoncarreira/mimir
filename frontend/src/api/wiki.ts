import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  WikiDanglingLink,
  WikiIndexData,
  WikiPageData,
  WikiPageSummary
} from "./generated/contracts";

export type {
  WikiDanglingLink,
  WikiIndexData,
  WikiPageData,
  WikiPageSummary
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function booleanValue(value: unknown): boolean {
  return value === true;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function normalizeWikiPageSummary(value: unknown): WikiPageSummary {
  const source = asRecord(value);
  const slug = stringValue(source.slug);
  const path = stringValue(source.path, slug ? `${slug}.md` : "");
  const title = stringValue(source.title, slug || path || "Untitled");
  return {
    slug,
    title,
    category: stringValue(source.category, "_root"),
    path,
    mtime: nullableString(source.mtime),
    outbound: stringList(source.outbound),
    inbound: stringList(source.inbound),
    is_orphan: booleanValue(source.is_orphan),
    has_slug_collision: booleanValue(source.has_slug_collision)
  };
}

function normalizeDanglingLink(value: unknown): WikiDanglingLink {
  const source = asRecord(value);
  return {
    target: stringValue(source.target),
    source: stringValue(source.source),
    line: numberValue(source.line)
  };
}

function normalizeSlugCollisions(value: unknown): Record<string, string[]> {
  const source = asRecord(value);
  return Object.fromEntries(
    Object.entries(source).map(([slug, paths]) => [slug, stringList(paths)])
  );
}

export function normalizeWikiIndexPayload(value: unknown): WikiIndexData {
  const source = asRecord(value);
  const pages = Array.isArray(source.pages)
    ? source.pages.map(normalizeWikiPageSummary)
    : [];
  const graph = asRecord(source.graph);
  const health = asRecord(source.health);
  return {
    page_count: numberValue(source.page_count, pages.length),
    pages,
    graph: {
      nodes: Array.isArray(graph.nodes)
        ? graph.nodes.map((node) => {
            const normalized = normalizeWikiPageSummary(node);
            return {
              id: stringValue(asRecord(node).id, normalized.path),
              slug: normalized.slug,
              title: normalized.title,
              category: normalized.category,
              is_orphan: normalized.is_orphan,
              has_slug_collision: normalized.has_slug_collision
            };
          })
        : [],
      edges: Array.isArray(graph.edges)
        ? graph.edges.map((edge) => {
            const item = asRecord(edge);
            return {
              source: stringValue(item.source),
              target: stringValue(item.target),
              target_slug: stringValue(item.target_slug)
            };
          })
        : []
    },
    orphans: stringList(source.orphans),
    dangling_links: Array.isArray(source.dangling_links)
      ? source.dangling_links.map(normalizeDanglingLink)
      : [],
    slug_collisions: normalizeSlugCollisions(source.slug_collisions),
    health: source.health && typeof source.health === "object"
      ? {
          has_orphans: booleanValue(health.has_orphans),
          has_dangling_links: booleanValue(health.has_dangling_links),
          has_slug_collisions: booleanValue(health.has_slug_collisions)
        }
      : undefined
  };
}

export function normalizeWikiPagePayload(value: unknown): WikiPageData {
  const source = asRecord(value);
  return {
    ...normalizeWikiPageSummary(source),
    markdown: stringValue(source.markdown)
  };
}

export async function getWikiIndex(
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<WikiIndexData>> {
  const envelope = await apiFetchEnvelope<unknown>("/api/v1/wiki", options);
  return { ...envelope, data: normalizeWikiIndexPayload(envelope.data) };
}

export async function getWikiPage(
  slug: string,
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<WikiPageData>> {
  const encoded = encodeURIComponent(slug.trim()).replace(/%2F/g, "/");
  const envelope = await apiFetchEnvelope<unknown>(`/api/v1/wiki/${encoded}`, options);
  return { ...envelope, data: normalizeWikiPagePayload(envelope.data) };
}
