import React from "react";
import { type WikiIndexData, type WikiPageSummary } from "../api";
import { Badge, EmptyState } from "../ui";

type GraphNodeKind = "page" | "dangling";

interface GraphNode {
  id: string;
  title: string;
  slug: string;
  category: string;
  isOrphan: boolean;
  hasSlugCollision: boolean;
  kind: GraphNodeKind;
  x: number;
  y: number;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  isDangling: boolean;
}

const CATEGORY_COLORS = [
  "#4466a3",
  "#2f7d61",
  "#9a6a24",
  "#8b4c7a",
  "#5f6f31",
  "#7a5642"
];

function pageKey(page: WikiPageSummary): string {
  return page.path.endsWith(".md") ? page.path.slice(0, -3) : page.path || page.slug;
}

function linkSlugForPage(page: WikiPageSummary): string {
  return pageKey(page);
}

function pageForNode(nodeId: string, pages: WikiPageSummary[]): WikiPageSummary | undefined {
  return pages.find((page) => page.path === nodeId || pageKey(page) === nodeId || page.slug === nodeId);
}

function categoryColor(category: string, categories: string[]): string {
  const index = Math.max(0, categories.indexOf(category));
  return CATEGORY_COLORS[index % CATEGORY_COLORS.length];
}

function danglingNodeId(target: string): string {
  return `dangling:${target}`;
}

function labelForEdgeTarget(target: string): string {
  return target.startsWith("dangling:") ? target.slice("dangling:".length) : target;
}

function buildGraph(index: WikiIndexData): { nodes: GraphNode[]; edges: GraphEdge[]; categories: string[] } {
  const sourceNodes = index.graph.nodes.length
    ? index.graph.nodes
    : index.pages.map((page) => ({
        id: page.path,
        slug: page.slug,
        title: page.title,
        category: page.category,
        is_orphan: page.is_orphan,
        has_slug_collision: page.has_slug_collision
      }));
  const categories = Array.from(new Set(sourceNodes.map((node) => node.category))).sort();
  const radius = 42;
  const centerX = 50;
  const centerY = 48;
  const nodes: GraphNode[] = sourceNodes.map((node, indexInGraph) => {
    const categoryIndex = Math.max(0, categories.indexOf(node.category));
    const categoryCount = categories.length || 1;
    const categoryAngle = (Math.PI * 2 * categoryIndex) / categoryCount;
    const offsetAngle = categoryAngle + (indexInGraph % 7 - 3) * 0.12;
    const offsetRadius = radius - (indexInGraph % 4) * 5;
    return {
      id: node.id,
      title: node.title,
      slug: node.slug,
      category: node.category,
      isOrphan: node.is_orphan,
      hasSlugCollision: node.has_slug_collision,
      kind: "page",
      x: centerX + Math.cos(offsetAngle) * offsetRadius,
      y: centerY + Math.sin(offsetAngle) * Math.min(offsetRadius, 34)
    };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges: GraphEdge[] = [];

  index.graph.edges.forEach((edge, edgeIndex) => {
    const target = nodeIds.has(edge.target) ? edge.target : danglingNodeId(edge.target_slug || edge.target);
    edges.push({
      id: `edge:${edge.source}:${target}:${edgeIndex}`,
      source: edge.source,
      target,
      isDangling: !nodeIds.has(edge.target)
    });
  });

  index.dangling_links.forEach((link, linkIndex) => {
    const id = danglingNodeId(link.target);
    if (!nodeIds.has(id)) {
      const angle = Math.PI * 1.62 + linkIndex * 0.3;
      nodes.push({
        id,
        title: link.target,
        slug: link.target,
        category: "dangling",
        isOrphan: false,
        hasSlugCollision: false,
        kind: "dangling",
        x: centerX + Math.cos(angle) * 36,
        y: centerY + Math.sin(angle) * 30
      });
      nodeIds.add(id);
    }
    edges.push({
      id: `dangling:${link.source}:${link.target}:${link.line}:${linkIndex}`,
      source: link.source,
      target: id,
      isDangling: true
    });
  });

  return { nodes, edges, categories: [...categories, "dangling"] };
}

export default function WikiGraphView({
  index,
  selected,
  onOpenPage
}: {
  index: WikiIndexData;
  selected: string;
  onOpenPage: (slug: string) => void;
}) {
  const { nodes, edges, categories } = React.useMemo(() => buildGraph(index), [index]);
  const nodeById = React.useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const pagesByPath = React.useMemo(() => new Map(index.pages.map((page) => [page.path, page])), [index.pages]);

  if (!nodes.length) {
    return <EmptyState title="No graph data">The wiki API returned no graph nodes.</EmptyState>;
  }

  return (
    <section aria-label="Wiki graph view" className="wiki-graph">
      <div className="wiki-graph__summary">
        <div>
          <strong>{nodes.filter((node) => node.kind === "page").length}</strong>
          <span>pages</span>
        </div>
        <div>
          <strong>{edges.length}</strong>
          <span>wikilinks</span>
        </div>
        <div>
          <strong>{nodes.filter((node) => node.kind === "dangling").length}</strong>
          <span>dangling</span>
        </div>
      </div>
      <div className="wiki-graph__canvas">
        <svg aria-label="Wiki pages and wikilinks graph" role="img" viewBox="0 0 100 100">
          <g className="wiki-graph__edges">
            {edges.map((edge) => {
              const source = nodeById.get(edge.source);
              const target = nodeById.get(edge.target);
              if (!source || !target) return null;
              return (
                <line
                  className={edge.isDangling ? "wiki-graph__edge wiki-graph__edge--dangling" : "wiki-graph__edge"}
                  key={edge.id}
                  x1={source.x}
                  x2={target.x}
                  y1={source.y}
                  y2={target.y}
                >
                  <title>{`${source.title} -> ${labelForEdgeTarget(target.id)}`}</title>
                </line>
              );
            })}
          </g>
          <g className="wiki-graph__nodes">
            {nodes.map((node) => {
              const page = pageForNode(node.id, index.pages);
              const selectedNode = Boolean(page && (selected === page.slug || selected === page.path || selected === pageKey(page)));
              const color = node.kind === "dangling" ? "#8a6116" : categoryColor(node.category, categories);
              const classes = [
                "wiki-graph__node",
                node.kind === "dangling" ? "wiki-graph__node--dangling" : "",
                node.isOrphan ? "wiki-graph__node--orphan" : "",
                node.hasSlugCollision ? "wiki-graph__node--collision" : "",
                selectedNode ? "wiki-graph__node--selected" : ""
              ].filter(Boolean).join(" ");
              return (
                <g className={classes} key={node.id} transform={`translate(${node.x} ${node.y})`}>
                  <circle fill={color} r={node.kind === "dangling" ? 2.7 : 3.6} />
                  {node.hasSlugCollision ? <rect height="8.4" width="8.4" x="-4.2" y="-4.2" /> : null}
                  {node.kind === "page" && page ? (
                    <circle
                      aria-label={`Open ${node.title}`}
                      className="wiki-graph__hit"
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenPage(linkSlugForPage(page));
                        }
                      }}
                      onClick={() => onOpenPage(linkSlugForPage(page))}
                      r="6"
                      role="button"
                      tabIndex={0}
                    />
                  ) : null}
                  <text x="4.8" y="1.4">{node.title}</text>
                  <title>
                    {node.title} ({node.category})
                    {node.isOrphan ? " - orphan" : ""}
                    {node.hasSlugCollision ? " - slug collision" : ""}
                    {node.kind === "dangling" ? " - dangling target" : ""}
                  </title>
                </g>
              );
            })}
          </g>
        </svg>
      </div>
      <div aria-label="Wiki graph legend" className="wiki-graph__legend">
        {categories.map((category) => (
          <span key={category}>
            <i style={{ background: category === "dangling" ? "#8a6116" : categoryColor(category, categories) }} />
            {category}
          </span>
        ))}
        <Badge tone="warning">ring = orphan</Badge>
        <Badge tone="danger">square = collision</Badge>
        <Badge tone="warning">dashed = dangling</Badge>
      </div>
      <div className="wiki-graph__list" aria-label="Wiki graph node health">
        {nodes.map((node) => {
          const page = pagesByPath.get(node.id);
          const flags = [
            node.kind === "dangling" ? "dangling target" : "",
            node.isOrphan ? "orphan" : "",
            node.hasSlugCollision ? "slug collision" : ""
          ].filter(Boolean);
          return (
            <button
              aria-label={`${node.title} ${node.kind === "dangling" ? "dangling target" : node.id}${flags.length ? ` ${flags.join(" ")}` : ""}`}
              disabled={!page}
              key={node.id}
              onClick={() => page && onOpenPage(linkSlugForPage(page))}
              type="button"
            >
              <span>{node.title}</span>
              <small>{node.kind === "dangling" ? "dangling target" : node.id}</small>
              {node.kind === "dangling" ? <Badge tone="warning">dangling</Badge> : null}
              {node.isOrphan ? <Badge tone="warning">orphan</Badge> : null}
              {node.hasSlugCollision ? <Badge tone="danger">collision</Badge> : null}
            </button>
          );
        })}
      </div>
    </section>
  );
}
