import React from "react";
import {
  GraphCanvas,
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

interface GraphThemeTokens {
  background: string;
  text: string;
  textMuted: string;
  panelBorder: string;
  accent: string;
  success: string;
  warning: string;
  danger: string;
}

interface WikiGraphTheme {
  categoryColors: string[];
  communityColors: string[];
  danglingColor: string;
  edgeColor: string;
  reagraphTheme: Theme;
  signature: string;
}

const FALLBACK_GRAPH_TOKENS: GraphThemeTokens = {
  background: "#f1f5f2",
  text: "#16201b",
  textMuted: "#58685f",
  panelBorder: "#d6ded9",
  accent: "#60786a",
  success: "#326b48",
  warning: "#7a5b1d",
  danger: "#8b3a3a"
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

function readCssToken(style: CSSStyleDeclaration, name: string, fallback: string): string {
  const value = style.getPropertyValue(name).trim();
  return value && !value.startsWith("var(") ? value : fallback;
}

function parseColor(color: string): [number, number, number] | null {
  const hex = color.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const value = hex[1].length === 3
      ? hex[1].split("").map((character) => character + character).join("")
      : hex[1];
    return [
      Number.parseInt(value.slice(0, 2), 16),
      Number.parseInt(value.slice(2, 4), 16),
      Number.parseInt(value.slice(4, 6), 16)
    ];
  }

  const rgb = color.trim().match(/^rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)/i);
  if (rgb) {
    return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])].map((channel) => Math.max(0, Math.min(255, Math.round(channel)))) as [
      number,
      number,
      number
    ];
  }

  return null;
}

function toHex([red, green, blue]: [number, number, number]): string {
  return `#${[red, green, blue].map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
}

function mixColors(color: string, target: string, targetWeight: number): string {
  const sourceRgb = parseColor(color);
  const targetRgb = parseColor(target);
  if (!sourceRgb || !targetRgb) return color;
  const sourceWeight = 1 - targetWeight;
  return toHex([
    Math.round(sourceRgb[0] * sourceWeight + targetRgb[0] * targetWeight),
    Math.round(sourceRgb[1] * sourceWeight + targetRgb[1] * targetWeight),
    Math.round(sourceRgb[2] * sourceWeight + targetRgb[2] * targetWeight)
  ]);
}

function withAlpha(color: string, alpha: number): string {
  const rgb = parseColor(color);
  if (!rgb) return color;
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

function readGraphThemeTokens(element: HTMLElement | null): GraphThemeTokens {
  if (typeof window === "undefined") return FALLBACK_GRAPH_TOKENS;
  const style = window.getComputedStyle(element ?? document.documentElement);
  return {
    background: readCssToken(style, "--mimir-color-panel-background-muted", FALLBACK_GRAPH_TOKENS.background),
    text: readCssToken(style, "--mimir-color-text", FALLBACK_GRAPH_TOKENS.text),
    textMuted: readCssToken(style, "--mimir-color-text-muted", FALLBACK_GRAPH_TOKENS.textMuted),
    panelBorder: readCssToken(style, "--mimir-color-panel-border", FALLBACK_GRAPH_TOKENS.panelBorder),
    accent: readCssToken(style, "--mimir-color-chrome-accent", FALLBACK_GRAPH_TOKENS.accent),
    success: readCssToken(style, "--mimir-color-status-success", FALLBACK_GRAPH_TOKENS.success),
    warning: readCssToken(style, "--mimir-color-status-warning", FALLBACK_GRAPH_TOKENS.warning),
    danger: readCssToken(style, "--mimir-color-status-danger", FALLBACK_GRAPH_TOKENS.danger)
  };
}

function createWikiGraphTheme(tokens: GraphThemeTokens): WikiGraphTheme {
  const edgeColor = withAlpha(tokens.text, 0.48);
  const clusterColors = [
    tokens.accent,
    tokens.success,
    tokens.warning,
    mixColors(tokens.textMuted, tokens.accent, 0.35),
    mixColors(tokens.warning, tokens.success, 0.35),
    tokens.danger,
    mixColors(tokens.accent, tokens.warning, 0.45),
    mixColors(tokens.text, tokens.success, 0.28)
  ];
  const categoryColors = [
    tokens.success,
    tokens.accent,
    tokens.warning,
    mixColors(tokens.textMuted, tokens.success, 0.3),
    mixColors(tokens.warning, tokens.accent, 0.42),
    tokens.danger
  ];
  const reagraphTheme = {
    canvas: { background: tokens.background, fog: null },
    node: {
      fill: tokens.accent,
      activeFill: tokens.text,
      opacity: 1,
      selectedOpacity: 1,
      inactiveOpacity: 0.22,
      label: {
        color: tokens.text,
        stroke: tokens.background,
        activeColor: tokens.text,
        backgroundColor: tokens.background,
        backgroundOpacity: 0.82,
        padding: 1.5,
        strokeColor: tokens.background,
        strokeWidth: 0.35
      },
      subLabel: {
        color: tokens.textMuted,
        stroke: tokens.background,
        activeColor: tokens.text,
        backgroundColor: tokens.background,
        backgroundOpacity: 0.82,
        padding: 1.5,
        strokeColor: tokens.background,
        strokeWidth: 0.35
      }
    },
    edge: {
      fill: withAlpha(tokens.text, 0.46),
      activeFill: tokens.text,
      opacity: 0.72,
      selectedOpacity: 0.95,
      inactiveOpacity: 0.12,
      label: {
        color: tokens.text,
        stroke: tokens.background,
        activeColor: tokens.text,
        fontSize: 5
      },
      subLabel: {
        color: tokens.textMuted,
        stroke: tokens.background,
        activeColor: tokens.text,
        fontSize: 5
      }
    },
    arrow: {
      fill: withAlpha(tokens.text, 0.46),
      activeFill: tokens.text
    },
    cluster: {
      stroke: withAlpha(tokens.text, 0.18),
      fill: withAlpha(tokens.panelBorder, 0.22),
      opacity: 0.5,
      selectedOpacity: 0.75,
      inactiveOpacity: 0.12,
      label: {
        color: tokens.textMuted,
        stroke: tokens.background,
        fontSize: 8
      }
    },
    ring: {
      fill: tokens.background,
      activeFill: tokens.text
    },
    lasso: {
      background: withAlpha(tokens.accent, 0.08),
      border: tokens.accent
    }
  } as Theme;
  return {
    categoryColors,
    communityColors: clusterColors,
    danglingColor: tokens.warning,
    edgeColor,
    reagraphTheme,
    signature: Object.values(tokens).join("|")
  };
}

function useWikiGraphTheme(rootRef: React.RefObject<HTMLElement | null>): WikiGraphTheme {
  const [theme, setTheme] = React.useState(() => createWikiGraphTheme(FALLBACK_GRAPH_TOKENS));

  React.useLayoutEffect(() => {
    const nextTheme = createWikiGraphTheme(readGraphThemeTokens(rootRef.current));
    setTheme((current) => (current.signature === nextTheme.signature ? current : nextTheme));
  });

  return theme;
}

function categoryColor(category: string, categories: string[], palette: string[]): string {
  const index = Math.max(0, categories.indexOf(category));
  return palette[index % palette.length];
}

function communityColor(community: string, palette: string[]): string {
  const index = Number(community.replace("community-", ""));
  if (Number.isFinite(index)) return palette[index % palette.length];
  return palette[0];
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

function toReagraphNode(node: GraphNode, degree: number, labeledNodeIds: Set<string>, graphTheme: WikiGraphTheme): ReagraphNode {
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
    fill: node.kind === "dangling" ? graphTheme.danglingColor : communityColor(node.community, graphTheme.communityColors),
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

function toReagraphEdge(edge: GraphEdge, graphTheme: WikiGraphTheme): ReagraphEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    fill: edge.isDangling ? graphTheme.danglingColor : graphTheme.edgeColor,
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
  const rootRef = React.useRef<HTMLElement | null>(null);
  const { nodes, edges, categories, communities } = React.useMemo(() => buildGraph(index), [index]);
  const graphTheme = useWikiGraphTheme(rootRef);
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
    () => nodes.map((node) => toReagraphNode(node, degreeByNode.get(node.id) ?? 0, labeledNodeIds, graphTheme)),
    [degreeByNode, graphTheme, labeledNodeIds, nodes]
  );
  const reagraphEdges = React.useMemo(() => edges.map((edge) => toReagraphEdge(edge, graphTheme)), [edges, graphTheme]);
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
    <section aria-label="Wiki graph view" className="wiki-graph" ref={rootRef}>
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
          theme={graphTheme.reagraphTheme}
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
            <i style={{ background: communityColor(community, graphTheme.communityColors) }} />
            {community}
          </span>
        ))}
        {categories.map((category) => (
          <span key={category}>
            <i style={{ background: category === "dangling" ? graphTheme.danglingColor : categoryColor(category, categories, graphTheme.categoryColors) }} />
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
