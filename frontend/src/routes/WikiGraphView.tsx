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

interface LabelPlacement {
  id: string;
  text: string;
  x: number;
  y: number;
  width: number;
  active: boolean;
}

interface ViewTransform {
  scale: number;
  x: number;
  y: number;
}

interface DragState {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  startX: number;
  startY: number;
}

const CATEGORY_COLORS = [
  "#4466a3",
  "#2f7d61",
  "#9a6a24",
  "#8b4c7a",
  "#5f6f31",
  "#7a5642"
];

const VIEWPORT_SIZE = 100;
const GRAPH_MIN = 6;
const GRAPH_MAX = 94;
const LAYOUT_ITERATIONS = 80;
const LABEL_FONT_SIZE = 2.4;
const LABEL_CHAR_WIDTH = 1.18;
const MAX_DEFAULT_LABELS = 18;
const MAX_LABEL_CHARS = 20;

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

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function truncateLabel(label: string, maxChars: number): string {
  if (label.length <= maxChars) return label;
  if (maxChars <= 1) return "…";
  return `${label.slice(0, Math.max(1, maxChars - 1)).trimEnd()}…`;
}

function nodeRadius(node: GraphNode): number {
  return node.kind === "dangling" ? 2.7 : 3.6;
}

function nodePriority(node: GraphNode, degree: number): number {
  return degree * 10 + (node.hasSlugCollision ? 8 : 0) + (node.isOrphan ? 4 : 0) + (node.kind === "page" ? 2 : 0);
}

function layoutGraph(nodes: GraphNode[], edges: GraphEdge[], categories: string[]): GraphNode[] {
  const categoryCenters = new Map<string, { x: number; y: number }>();
  const layoutCategories = categories.filter((category) => category !== "dangling");
  const categoryCount = Math.max(1, layoutCategories.length);
  layoutCategories.forEach((category, categoryIndex) => {
    const angle = (Math.PI * 2 * categoryIndex) / categoryCount - Math.PI / 2;
    categoryCenters.set(category, {
      x: 50 + Math.cos(angle) * 26,
      y: 50 + Math.sin(angle) * 22
    });
  });
  categoryCenters.set("dangling", { x: 50, y: 82 });

  const groups = new Map<string, GraphNode[]>();
  nodes.forEach((node) => {
    const group = groups.get(node.category) ?? [];
    group.push(node);
    groups.set(node.category, group);
  });

  const laidOut = nodes.map((node) => ({ ...node }));
  const nodeById = new Map(laidOut.map((node) => [node.id, node]));
  const orderById = new Map(nodes.map((node, index) => [node.id, index]));

  for (const [category, group] of groups) {
    const center = categoryCenters.get(category) ?? { x: 50, y: 50 };
    group
      .map((node) => nodeById.get(node.id))
      .filter((node): node is GraphNode => Boolean(node))
      .forEach((node, indexInGroup) => {
        const goldenAngle = Math.PI * (3 - Math.sqrt(5));
        const radius = Math.sqrt(indexInGroup + 0.5) * 4.1;
        const angle = indexInGroup * goldenAngle;
        node.x = clamp(center.x + Math.cos(angle) * radius, GRAPH_MIN, GRAPH_MAX);
        node.y = clamp(center.y + Math.sin(angle) * radius, GRAPH_MIN, GRAPH_MAX);
      });
  }

  for (let iteration = 0; iteration < LAYOUT_ITERATIONS; iteration += 1) {
    for (const edge of edges) {
      const source = nodeById.get(edge.source);
      const target = nodeById.get(edge.target);
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(0.01, Math.hypot(dx, dy));
      const desired = edge.isDangling ? 12 : 15;
      const force = (distance - desired) * 0.012;
      const moveX = (dx / distance) * force;
      const moveY = (dy / distance) * force;
      source.x += moveX;
      source.y += moveY;
      target.x -= moveX;
      target.y -= moveY;
    }

    for (let a = 0; a < laidOut.length; a += 1) {
      for (let b = a + 1; b < laidOut.length; b += 1) {
        const first = laidOut[a];
        const second = laidOut[b];
        const dx = second.x - first.x;
        const dy = second.y - first.y;
        const distance = Math.max(0.01, Math.hypot(dx, dy));
        const sameCategory = first.category === second.category;
        const minimum = sameCategory ? nodeRadius(first) + nodeRadius(second) + 1.8 : 5.4;
        if (distance >= minimum) continue;
        const push = (minimum - distance) * 0.5;
        const jitter = ((orderById.get(first.id) ?? 0) - (orderById.get(second.id) ?? 0)) * 0.0007;
        const moveX = (dx / distance + jitter) * push;
        const moveY = (dy / distance - jitter) * push;
        first.x -= moveX;
        first.y -= moveY;
        second.x += moveX;
        second.y += moveY;
      }
    }

    for (const node of laidOut) {
      const center = categoryCenters.get(node.category) ?? { x: 50, y: 50 };
      node.x += (center.x - node.x) * 0.01;
      node.y += (center.y - node.y) * 0.01;
      node.x = clamp(node.x, GRAPH_MIN, GRAPH_MAX);
      node.y = clamp(node.y, GRAPH_MIN, GRAPH_MAX);
    }
  }

  return laidOut;
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
  const nodes: GraphNode[] = sourceNodes.map((node) => {
    return {
      id: node.id,
      title: node.title,
      slug: node.slug,
      category: node.category,
      isOrphan: node.is_orphan,
      hasSlugCollision: node.has_slug_collision,
      kind: "page",
      x: 50,
      y: 50
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
        x: 50,
        y: 82 + linkIndex
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

  const layoutCategories = [...categories, "dangling"];
  return { nodes: layoutGraph(nodes, edges, layoutCategories), edges, categories: layoutCategories };
}

function buildNeighborSets(edges: GraphEdge[]): { neighborsByNode: Map<string, Set<string>>; connectedEdgeIdsByNode: Map<string, Set<string>> } {
  const neighborsByNode = new Map<string, Set<string>>();
  const connectedEdgeIdsByNode = new Map<string, Set<string>>();
  edges.forEach((edge) => {
    const sourceNeighbors = neighborsByNode.get(edge.source) ?? new Set<string>();
    const targetNeighbors = neighborsByNode.get(edge.target) ?? new Set<string>();
    sourceNeighbors.add(edge.target);
    targetNeighbors.add(edge.source);
    neighborsByNode.set(edge.source, sourceNeighbors);
    neighborsByNode.set(edge.target, targetNeighbors);

    const sourceEdges = connectedEdgeIdsByNode.get(edge.source) ?? new Set<string>();
    const targetEdges = connectedEdgeIdsByNode.get(edge.target) ?? new Set<string>();
    sourceEdges.add(edge.id);
    targetEdges.add(edge.id);
    connectedEdgeIdsByNode.set(edge.source, sourceEdges);
    connectedEdgeIdsByNode.set(edge.target, targetEdges);
  });
  return { neighborsByNode, connectedEdgeIdsByNode };
}

function labelWidth(text: string): number {
  return text.length * LABEL_CHAR_WIDTH;
}

function labelBoxOverlaps(
  box: { x: number; y: number; width: number; height: number },
  boxes: Array<{ x: number; y: number; width: number; height: number }>
): boolean {
  return boxes.some((other) => (
    box.x < other.x + other.width
    && box.x + box.width > other.x
    && box.y < other.y + other.height
    && box.y + box.height > other.y
  ));
}

function labelPlacementForNode(node: GraphNode, text: string, active: boolean): LabelPlacement {
  const width = labelWidth(text);
  let x = node.x + nodeRadius(node) + 1.8;
  if (x + width > GRAPH_MAX) x = node.x - width - nodeRadius(node) - 1.8;
  x = clamp(x, 2, Math.max(2, VIEWPORT_SIZE - width - 2));
  const y = clamp(node.y + 0.9, 4, VIEWPORT_SIZE - 3);
  return { id: node.id, text, x, y, width, active };
}

function buildLabelPlacements(nodes: GraphNode[], edges: GraphEdge[], selectedId: string | null, activeId: string | null): LabelPlacement[] {
  const degreeByNode = new Map<string, number>();
  edges.forEach((edge) => {
    degreeByNode.set(edge.source, (degreeByNode.get(edge.source) ?? 0) + 1);
    degreeByNode.set(edge.target, (degreeByNode.get(edge.target) ?? 0) + 1);
  });

  const requiredIds = new Set([selectedId, activeId].filter((id): id is string => Boolean(id)));
  const candidates = [...nodes]
    .filter((node) => node.kind === "page" || requiredIds.has(node.id))
    .sort((a, b) => (
      nodePriority(b, degreeByNode.get(b.id) ?? 0) - nodePriority(a, degreeByNode.get(a.id) ?? 0)
      || a.title.localeCompare(b.title)
      || a.id.localeCompare(b.id)
    ));

  const boxes: Array<{ x: number; y: number; width: number; height: number }> = [];
  const labels: LabelPlacement[] = [];
  for (const node of candidates) {
    const active = node.id === activeId;
    const required = requiredIds.has(node.id);
    const text = truncateLabel(node.title, MAX_LABEL_CHARS);
    const placement = labelPlacementForNode(node, text, active);
    const box = { x: placement.x - 0.8, y: placement.y - LABEL_FONT_SIZE, width: placement.width + 1.6, height: LABEL_FONT_SIZE + 1.2 };
    if (!required && (labels.length >= MAX_DEFAULT_LABELS || labelBoxOverlaps(box, boxes))) continue;
    labels.push(placement);
    boxes.push(box);
  }

  return labels;
}

function clampTransform(transform: ViewTransform): ViewTransform {
  const scale = clamp(transform.scale, 1, 4);
  const minPan = VIEWPORT_SIZE - VIEWPORT_SIZE * scale;
  return {
    scale,
    x: clamp(transform.x, minPan, 0),
    y: clamp(transform.y, minPan, 0)
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
  const { nodes, edges, categories } = React.useMemo(() => buildGraph(index), [index]);
  const nodeById = React.useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const pagesByPath = React.useMemo(() => new Map(index.pages.map((page) => [page.path, page])), [index.pages]);
  const [activeNodeId, setActiveNodeId] = React.useState<string | null>(null);
  const [transform, setTransform] = React.useState<ViewTransform>({ scale: 1, x: 0, y: 0 });
  const [dragState, setDragState] = React.useState<DragState | null>(null);
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const { neighborsByNode, connectedEdgeIdsByNode } = React.useMemo(() => buildNeighborSets(edges), [edges]);
  const selectedNodeId = React.useMemo(() => {
    const selectedPage = index.pages.find((page) => selected === page.slug || selected === page.path || selected === pageKey(page));
    return selectedPage?.path ?? null;
  }, [index.pages, selected]);
  const activeNode = activeNodeId ? nodeById.get(activeNodeId) : undefined;
  const activeNeighborIds = activeNodeId ? neighborsByNode.get(activeNodeId) ?? new Set<string>() : new Set<string>();
  const activeEdgeIds = activeNodeId ? connectedEdgeIdsByNode.get(activeNodeId) ?? new Set<string>() : new Set<string>();
  const labelPlacements = React.useMemo(
    () => buildLabelPlacements(nodes, edges, selectedNodeId, activeNodeId),
    [activeNodeId, edges, nodes, selectedNodeId]
  );

  function zoomBy(delta: number) {
    setTransform((current) => clampTransform({ ...current, scale: current.scale * delta }));
  }

  function resetView() {
    setTransform({ scale: 1, x: 0, y: 0 });
  }

  function panBy(dx: number, dy: number) {
    setTransform((current) => clampTransform({ ...current, x: current.x + dx, y: current.y + dy }));
  }

  function handleWheel(event: React.WheelEvent<SVGSVGElement>) {
    event.preventDefault();
    const nextScale = clamp(transform.scale * (event.deltaY > 0 ? 0.88 : 1.14), 1, 4);
    const rect = svgRef.current?.getBoundingClientRect();
    const viewportX = rect && rect.width ? ((event.clientX - rect.left) / rect.width) * VIEWPORT_SIZE : VIEWPORT_SIZE / 2;
    const viewportY = rect && rect.height ? ((event.clientY - rect.top) / rect.height) * VIEWPORT_SIZE : VIEWPORT_SIZE / 2;
    const worldX = (viewportX - transform.x) / transform.scale;
    const worldY = (viewportY - transform.y) / transform.scale;
    setTransform(clampTransform({
      scale: nextScale,
      x: viewportX - worldX * nextScale,
      y: viewportY - worldY * nextScale
    }));
  }

  function handlePointerDown(event: React.PointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragState({
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startX: transform.x,
      startY: transform.y
    });
  }

  function handlePointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (!dragState || dragState.pointerId !== event.pointerId) return;
    const rect = svgRef.current?.getBoundingClientRect();
    const width = rect?.width || 1;
    const height = rect?.height || 1;
    const dx = ((event.clientX - dragState.startClientX) / width) * VIEWPORT_SIZE;
    const dy = ((event.clientY - dragState.startClientY) / height) * VIEWPORT_SIZE;
    setTransform(clampTransform({ scale: transform.scale, x: dragState.startX + dx, y: dragState.startY + dy }));
  }

  function handlePointerEnd(event: React.PointerEvent<SVGSVGElement>) {
    if (dragState?.pointerId === event.pointerId) setDragState(null);
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
          <strong>{nodes.filter((node) => node.kind === "dangling").length}</strong>
          <span>dangling</span>
        </div>
      </div>
      <div className="wiki-graph__toolbar" aria-label="Wiki graph controls">
        <button aria-label="Zoom in" onClick={() => zoomBy(1.25)} type="button">+</button>
        <button aria-label="Zoom out" onClick={() => zoomBy(0.8)} type="button">-</button>
        <button aria-label="Pan left" onClick={() => panBy(8, 0)} type="button">&lt;</button>
        <button aria-label="Pan right" onClick={() => panBy(-8, 0)} type="button">&gt;</button>
        <button aria-label="Pan up" onClick={() => panBy(0, 8)} type="button">^</button>
        <button aria-label="Pan down" onClick={() => panBy(0, -8)} type="button">v</button>
        <button aria-label="Reset graph view" onClick={resetView} type="button">Reset</button>
        <output aria-label="Graph zoom level">{Math.round(transform.scale * 100)}%</output>
      </div>
      <div className="wiki-graph__canvas">
        <svg
          aria-label="Wiki pages and wikilinks graph"
          className={dragState ? "wiki-graph__svg wiki-graph__svg--dragging" : "wiki-graph__svg"}
          onPointerCancel={handlePointerEnd}
          onPointerDown={handlePointerDown}
          onPointerLeave={handlePointerEnd}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerEnd}
          onWheel={handleWheel}
          ref={svgRef}
          role="img"
          viewBox="0 0 100 100"
        >
          <clipPath id="wiki-graph-viewport">
            <rect height="100" width="100" x="0" y="0" />
          </clipPath>
          <g clipPath="url(#wiki-graph-viewport)">
            <g transform={`translate(${transform.x} ${transform.y}) scale(${transform.scale})`}>
              <g className="wiki-graph__edges">
                {edges.map((edge) => {
                  const source = nodeById.get(edge.source);
                  const target = nodeById.get(edge.target);
                  if (!source || !target) return null;
                  const related = !activeNodeId || activeEdgeIds.has(edge.id);
                  return (
                    <line
                      className={[
                        "wiki-graph__edge",
                        edge.isDangling ? "wiki-graph__edge--dangling" : "",
                        related ? "wiki-graph__edge--related" : "wiki-graph__edge--dimmed"
                      ].filter(Boolean).join(" ")}
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
                  const selectedNode = node.id === selectedNodeId;
                  const related = !activeNodeId || node.id === activeNodeId || activeNeighborIds.has(node.id);
                  const color = node.kind === "dangling" ? "#8a6116" : categoryColor(node.category, categories);
                  const classes = [
                    "wiki-graph__node",
                    node.kind === "dangling" ? "wiki-graph__node--dangling" : "",
                    node.isOrphan ? "wiki-graph__node--orphan" : "",
                    node.hasSlugCollision ? "wiki-graph__node--collision" : "",
                    selectedNode ? "wiki-graph__node--selected" : "",
                    node.id === activeNodeId ? "wiki-graph__node--active" : "",
                    related ? "wiki-graph__node--related" : "wiki-graph__node--dimmed"
                  ].filter(Boolean).join(" ");
                  return (
                    <g
                      className={classes}
                      key={node.id}
                      onMouseEnter={() => setActiveNodeId(node.id)}
                      onMouseLeave={() => setActiveNodeId((current) => (current === node.id ? null : current))}
                      transform={`translate(${node.x} ${node.y})`}
                    >
                      <circle fill={color} r={nodeRadius(node)} />
                      {node.hasSlugCollision ? <rect height="8.4" width="8.4" x="-4.2" y="-4.2" /> : null}
                      {node.kind === "page" && page ? (
                        <circle
                          aria-label={`Open ${node.title}`}
                          className="wiki-graph__hit"
                          onBlur={() => setActiveNodeId((current) => (current === node.id ? null : current))}
                          onFocus={() => setActiveNodeId(node.id)}
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
              <g className="wiki-graph__labels" aria-hidden="true">
                {labelPlacements.map((label) => (
                  <text
                    className={label.active ? "wiki-graph__label wiki-graph__label--active" : "wiki-graph__label"}
                    key={label.id}
                    x={label.x}
                    y={label.y}
                  >
                    {label.text}
                  </text>
                ))}
              </g>
            </g>
          </g>
        </svg>
      </div>
      <div className="wiki-graph__focus-label" aria-live="polite">
        {activeNode ? (
          <>
            <strong>{activeNode.title}</strong>
            <span>{activeNode.kind === "dangling" ? "dangling target" : activeNode.category}</span>
          </>
        ) : (
          <span>Hover or focus a node to isolate its links.</span>
        )}
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
