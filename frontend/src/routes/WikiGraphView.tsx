import React from "react";
import {
  GraphCanvas,
  lightTheme,
  type GraphCanvasRef,
  type GraphEdge as ReagraphEdge,
  type GraphNode as ReagraphNode,
  type InternalGraphNode,
  type Theme
} from "reagraph";
import wikiLabelFontUrl from "../assets/wiki/kenpixel.ttf";
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
  community: string;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  isDangling: boolean;
}

interface GraphNodeData {
  category: string;
  community: string;
  hasSlugCollision: boolean;
  isOrphan: boolean;
  kind: GraphNodeKind;
  pageSlug: string | null;
  title: string;
}

const CATEGORY_COLORS = [
  "#4466a3",
  "#2f7d61",
  "#9a6a24",
  "#8b4c7a",
  "#5f6f31",
  "#7a5642"
];

const COMMUNITY_COLORS = [
  "#2f6f9f",
  "#3f7d55",
  "#9a6a24",
  "#8d4d6f",
  "#6a6f2f",
  "#7a5642",
  "#4f668f",
  "#a64f3c"
];

const GRAPH_THEME: Theme = {
  ...lightTheme,
  canvas: { background: "#f1f5f2", fog: null },
  node: {
    ...lightTheme.node,
    fill: "#4466a3",
    activeFill: "#16201b",
    inactiveOpacity: 0.22,
    label: {
      ...lightTheme.node.label,
      color: "#16201b",
      stroke: "#f1f5f2",
      activeColor: "#16201b",
      backgroundColor: "#f1f5f2",
      backgroundOpacity: 0.8,
      padding: 1.5,
      strokeColor: "#f1f5f2",
      strokeWidth: 0.35
    }
  },
  edge: {
    ...lightTheme.edge,
    fill: "rgba(22, 32, 27, 0.46)",
    activeFill: "#16201b",
    inactiveOpacity: 0.12,
    opacity: 0.72,
    selectedOpacity: 0.95,
    label: {
      ...lightTheme.edge.label,
      color: "#16201b",
      stroke: "#f1f5f2",
      activeColor: "#16201b",
      fontSize: 5
    }
  },
  arrow: {
    fill: "rgba(22, 32, 27, 0.46)",
    activeFill: "#16201b"
  },
  cluster: {
    stroke: "rgba(22, 32, 27, 0.18)",
    fill: "rgba(255, 255, 255, 0.18)",
    opacity: 0.5,
    selectedOpacity: 0.75,
    inactiveOpacity: 0.12,
    label: {
      color: "#58685f",
      stroke: "#f1f5f2",
      fontSize: 8
    }
  },
  ring: {
    fill: "#f1f5f2",
    activeFill: "#16201b"
  },
  lasso: lightTheme.lasso
};

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

function communityColor(community: string): string {
  const index = Number(community.replace("community-", ""));
  if (Number.isFinite(index)) return COMMUNITY_COLORS[index % COMMUNITY_COLORS.length];
  return COMMUNITY_COLORS[0];
}

function danglingNodeId(target: string): string {
  return `dangling:${target}`;
}

function nodeSize(node: GraphNode, degree: number): number {
  if (node.kind === "dangling") return 5;
  return Math.min(14, 7 + Math.sqrt(degree) * 1.2 + (node.hasSlugCollision ? 1.5 : 0));
}

function graphLabel(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/[^\x20-\x7e]/g, "")
    .trim() || "untitled";
}

function buildCommunityIds(nodes: GraphNode[], edges: GraphEdge[]): Map<string, string> {
  const neighborsByNode = new Map<string, Set<string>>();
  nodes.forEach((node) => neighborsByNode.set(node.id, new Set()));
  edges.forEach((edge) => {
    neighborsByNode.get(edge.source)?.add(edge.target);
    neighborsByNode.get(edge.target)?.add(edge.source);
  });

  let labels = new Map(nodes.map((node) => [node.id, node.id]));
  for (let iteration = 0; iteration < 24; iteration += 1) {
    let changed = false;
    const nextLabels = new Map(labels);
    const orderedNodes = [...nodes].sort((a, b) => {
      const degreeDelta = (neighborsByNode.get(b.id)?.size ?? 0) - (neighborsByNode.get(a.id)?.size ?? 0);
      return degreeDelta || a.id.localeCompare(b.id);
    });

    orderedNodes.forEach((node) => {
      const counts = new Map<string, number>();
      neighborsByNode.get(node.id)?.forEach((neighborId) => {
        const label = labels.get(neighborId);
        if (label) counts.set(label, (counts.get(label) ?? 0) + 1);
      });
      if (!counts.size) return;
      const current = labels.get(node.id) ?? node.id;
      const best = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
      if (best !== current) {
        nextLabels.set(node.id, best);
        changed = true;
      }
    });

    labels = nextLabels;
    if (!changed) break;
  }

  const groups = new Map<string, string[]>();
  nodes.forEach((node) => {
    const label = labels.get(node.id) ?? node.id;
    const group = groups.get(label) ?? [];
    group.push(node.id);
    groups.set(label, group);
  });

  const communityByLabel = new Map<string, string>();
  [...groups.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .forEach(([label], index) => communityByLabel.set(label, `community-${index + 1}`));

  return new Map(nodes.map((node) => [node.id, communityByLabel.get(labels.get(node.id) ?? node.id) ?? "community-1"]));
}

function buildGraph(index: WikiIndexData): { nodes: GraphNode[]; edges: GraphEdge[]; categories: string[]; communities: string[] } {
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
  const nodes: GraphNode[] = sourceNodes.map((node) => {
    return {
      id: node.id,
      title: node.title,
      slug: node.slug,
      category: node.category,
      isOrphan: node.is_orphan,
      hasSlugCollision: node.has_slug_collision,
      kind: "page",
      community: "community-1"
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
      nodes.push({
        id,
        title: link.target,
        slug: link.target,
        category: "dangling",
        isOrphan: false,
        hasSlugCollision: false,
        kind: "dangling",
        community: "community-1"
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

  const communityByNode = buildCommunityIds(nodes, edges);
  nodes.forEach((node) => {
    node.community = communityByNode.get(node.id) ?? "community-1";
  });

  const communities = Array.from(new Set(nodes.map((node) => node.community))).sort((a, b) => {
    const aIndex = Number(a.replace("community-", ""));
    const bIndex = Number(b.replace("community-", ""));
    return aIndex - bIndex;
  });

  return {
    nodes,
    edges: edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target)),
    categories: categories.includes("dangling") || !nodes.some((node) => node.kind === "dangling") ? categories : [...categories, "dangling"],
    communities
  };
}

function buildDegreeMap(edges: GraphEdge[]): Map<string, number> {
  const degreeByNode = new Map<string, number>();
  edges.forEach((edge) => {
    degreeByNode.set(edge.source, (degreeByNode.get(edge.source) ?? 0) + 1);
    degreeByNode.set(edge.target, (degreeByNode.get(edge.target) ?? 0) + 1);
  });
  return degreeByNode;
}

function toReagraphNode(node: GraphNode, degree: number, labeledNodeIds: Set<string>): ReagraphNode {
  const showLabel = labeledNodeIds.has(node.id);
  const labelParts = [
    node.title,
    node.kind === "dangling" ? "dangling target" : node.category,
    node.isOrphan ? "orphan" : "",
    node.hasSlugCollision ? "slug collision" : ""
  ].filter(Boolean);
  return {
    id: node.id,
    label: showLabel ? graphLabel(node.title) : "",
    subLabel: showLabel ? graphLabel(node.kind === "dangling" ? "dangling target" : node.category) : "",
    fill: node.kind === "dangling" ? "#8a6116" : communityColor(node.community),
    cluster: node.community,
    labelVisible: showLabel,
    size: nodeSize(node, degree),
    data: {
      category: node.category,
      community: node.community,
      hasSlugCollision: node.hasSlugCollision,
      isOrphan: node.isOrphan,
      kind: node.kind,
      pageSlug: node.kind === "page" ? node.slug : null,
      title: labelParts.join(" - ")
    } satisfies GraphNodeData
  };
}

function toReagraphEdge(edge: GraphEdge): ReagraphEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    fill: edge.isDangling ? "#8a6116" : "rgba(22, 32, 27, 0.48)",
    dashed: edge.isDangling,
    dashArray: [4, 2],
    arrowPlacement: "none",
    interpolation: "curved"
  };
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
  const { nodes, edges, categories, communities } = React.useMemo(() => buildGraph(index), [index]);
  const nodeById = React.useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const pagesByPath = React.useMemo(() => new Map(index.pages.map((page) => [page.path, page])), [index.pages]);
  const graphRef = React.useRef<GraphCanvasRef | null>(null);
  const [activeNodeId, setActiveNodeId] = React.useState<string | null>(null);
  const selectedNodeId = React.useMemo(() => {
    const selectedPage = index.pages.find((page) => selected === page.slug || selected === page.path || selected === pageKey(page));
    return selectedPage?.path ?? null;
  }, [index.pages, selected]);
  const degreeByNode = React.useMemo(() => buildDegreeMap(edges), [edges]);
  const labeledNodeIds = React.useMemo(() => {
    return new Set([activeNodeId, selectedNodeId].filter((id): id is string => Boolean(id)));
  }, [activeNodeId, selectedNodeId]);
  const reagraphNodes = React.useMemo(
    () => nodes.map((node) => toReagraphNode(node, degreeByNode.get(node.id) ?? 0, labeledNodeIds)),
    [degreeByNode, labeledNodeIds, nodes]
  );
  const reagraphEdges = React.useMemo(() => edges.map(toReagraphEdge), [edges]);
  const selections = React.useMemo(() => (selectedNodeId ? [selectedNodeId] : []), [selectedNodeId]);
  const actives = React.useMemo(() => (activeNodeId ? [activeNodeId] : []), [activeNodeId]);
  const focusedNode = (activeNodeId ? nodeById.get(activeNodeId) : undefined) ?? (selectedNodeId ? nodeById.get(selectedNodeId) : undefined);

  function openGraphNode(node: InternalGraphNode) {
    const graphNode = nodeById.get(node.id);
    if (!graphNode || graphNode.kind !== "page") return;
    const page = pageForNode(graphNode.id, index.pages);
    if (page) onOpenPage(linkSlugForPage(page));
  }

  function fitGraph() {
    graphRef.current?.fitNodesInView();
  }

  function zoomIn() {
    graphRef.current?.zoomIn();
  }

  function zoomOut() {
    graphRef.current?.zoomOut();
  }

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
          <strong>{communities.length}</strong>
          <span>communities</span>
        </div>
      </div>
      <div aria-label="Wiki graph controls" className="wiki-graph__toolbar">
        <button onClick={fitGraph} type="button">Fit</button>
        <button aria-label="Zoom in" onClick={zoomIn} type="button">+</button>
        <button aria-label="Zoom out" onClick={zoomOut} type="button">-</button>
      </div>
      <div aria-label="Wiki pages and wikilinks graph" className="wiki-graph__canvas" role="img">
        <GraphCanvas
          ref={graphRef}
          actives={actives}
          aggregateEdges={false}
          animated
          cameraMode="pan"
          clusterAttribute="community"
          defaultNodeSize={7}
          draggable
          edgeArrowPosition="none"
          edgeInterpolation="curved"
          edges={reagraphEdges}
          labelFontUrl={wikiLabelFontUrl}
          labelType="nodes"
          layoutOverrides={{
            clusterStrength: 0.72,
            clusterType: "force",
            forceCharge: -1050,
            forceLinkDistance: 110,
            forceLinkStrength: 0.14,
            linkStrengthInterCluster: 0.02,
            linkStrengthIntraCluster: 0.52,
            nodeStrength: -420
          }}
          layoutType="forceDirected2d"
          maxNodeSize={15}
          maxZoom={140}
          minNodeSize={5}
          minZoom={0.25}
          nodes={reagraphNodes}
          onNodeClick={openGraphNode}
          onNodePointerOut={(node) => setActiveNodeId((current) => (current === node.id ? null : current))}
          onNodePointerOver={(node) => setActiveNodeId(node.id)}
          selections={selections}
          sizingType="default"
          theme={GRAPH_THEME}
        />
      </div>
      <div className="wiki-graph__focus-label" aria-live="polite">
        {focusedNode ? (
          <>
            <strong>{focusedNode.title}</strong>
            <span>{focusedNode.kind === "dangling" ? "dangling target" : `${focusedNode.category} / ${focusedNode.community}`}</span>
          </>
        ) : (
          <span>Zoom, pan, drag, or hover nodes to inspect link communities.</span>
        )}
      </div>
      <div aria-label="Wiki graph legend" className="wiki-graph__legend">
        {communities.map((community) => (
          <span key={community}>
            <i style={{ background: communityColor(community) }} />
            {community}
          </span>
        ))}
        {categories.map((category) => (
          <span key={category}>
            <i style={{ background: category === "dangling" ? "#8a6116" : categoryColor(category, categories) }} />
            {category}
          </span>
        ))}
        <Badge tone="warning">orphan</Badge>
        <Badge tone="danger">collision</Badge>
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
              <small>{node.community}</small>
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
