// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WikiIndexData } from "../api";
import WikiGraphView from "./WikiGraphView";

const graphCanvasMock = vi.hoisted(() => ({
  fitNodesInView: vi.fn(),
  props: [] as Array<Record<string, any>>,
  zoomIn: vi.fn(),
  zoomOut: vi.fn()
}));

vi.mock("reagraph", () => {
  const lightTheme = {
    canvas: {},
    node: {
      fill: "",
      activeFill: "",
      opacity: 1,
      selectedOpacity: 1,
      inactiveOpacity: 0.2,
      label: {},
      subLabel: {}
    },
    ring: { fill: "", activeFill: "" },
    edge: {
      fill: "",
      activeFill: "",
      opacity: 1,
      selectedOpacity: 1,
      inactiveOpacity: 0.1,
      label: {},
      subLabel: {}
    },
    arrow: { fill: "", activeFill: "" },
    lasso: { background: "", border: "" },
    cluster: { label: {} }
  };
  return {
    lightTheme,
    GraphCanvas: React.forwardRef((props: Record<string, any>, ref) => {
      graphCanvasMock.props.push(props);
      React.useImperativeHandle(ref, () => ({
        fitNodesInView: graphCanvasMock.fitNodesInView,
        zoomIn: graphCanvasMock.zoomIn,
        zoomOut: graphCanvasMock.zoomOut
      }));
      return React.createElement(
        "div",
        {
          "aria-label": "Mock reagraph canvas",
          "data-edge-count": props.edges.length,
          "data-node-count": props.nodes.length,
          "data-testid": "reagraph-canvas"
        },
        props.nodes
          .filter((node: { data?: { kind?: string } }) => node.data?.kind === "page")
          .map((node: { id: string; data?: { title?: string } }) => (
            React.createElement(
              "button",
              {
                key: node.id,
                onClick: () => props.onNodeClick({ id: node.id, data: {} }),
                onPointerOut: () => props.onNodePointerOut({ id: node.id, data: {} }),
                onPointerOver: () => props.onNodePointerOver({ id: node.id, data: {} }),
                type: "button"
              },
              `Mock open ${node.data?.title?.split(" - ")[0] ?? node.id}`
            )
          ))
      );
    })
  };
});

function graphIndex(): WikiIndexData {
  return {
    page_count: 3,
    pages: [
      {
        slug: "alpha",
        title: "Alpha",
        category: "concepts",
        path: "concepts/alpha.md",
        mtime: null,
        outbound: ["beta", "ghost"],
        inbound: [],
        is_orphan: true,
        has_slug_collision: false
      },
      {
        slug: "beta",
        title: "Beta",
        category: "topics",
        path: "topics/beta.md",
        mtime: null,
        outbound: ["alpha"],
        inbound: ["concepts/alpha.md"],
        is_orphan: false,
        has_slug_collision: false
      },
      {
        slug: "alpha",
        title: "Alpha Topic",
        category: "topics",
        path: "topics/alpha.md",
        mtime: null,
        outbound: [],
        inbound: [],
        is_orphan: true,
        has_slug_collision: true
      }
    ],
    graph: {
      nodes: [
        { id: "concepts/alpha.md", slug: "alpha", title: "Alpha", category: "concepts", is_orphan: true, has_slug_collision: false },
        { id: "topics/beta.md", slug: "beta", title: "Beta", category: "topics", is_orphan: false, has_slug_collision: false },
        { id: "topics/alpha.md", slug: "alpha", title: "Alpha Topic", category: "topics", is_orphan: true, has_slug_collision: true }
      ],
      edges: [
        { source: "concepts/alpha.md", target: "topics/beta.md", target_slug: "beta" },
        { source: "topics/beta.md", target: "concepts/alpha.md", target_slug: "alpha" }
      ]
    },
    orphans: ["concepts/alpha.md", "topics/alpha.md"],
    dangling_links: [{ target: "ghost", source: "concepts/alpha.md", line: 2 }],
    slug_collisions: { alpha: ["concepts/alpha.md", "topics/alpha.md"] },
    health: { has_orphans: true, has_dangling_links: true, has_slug_collisions: true }
  };
}

function denseGraphIndex(): WikiIndexData {
  const pages = Array.from({ length: 142 }, (_, index) => {
    const slug = `page-${index}`;
    const category = index < 101 ? "concepts" : index < 122 ? "topics" : "entities";
    return {
      slug,
      title: `Node ${index}`,
      category,
      path: `${category}/${slug}.md`,
      mtime: null,
      outbound: [],
      inbound: [],
      is_orphan: false,
      has_slug_collision: false
    };
  });
  const edges: Array<{ source: string; target: string; target_slug: string }> = [];
  for (const [start, end] of [[0, 71], [71, 142]]) {
    for (let sourceIndex = start; sourceIndex < end && edges.length < 924; sourceIndex += 1) {
      for (let offset = 1; offset < end - start && edges.length < 924; offset += 1) {
        const target = pages[start + ((sourceIndex - start + offset) % (end - start))];
        edges.push({
          source: pages[sourceIndex].path,
          target: target.path,
          target_slug: target.slug
        });
      }
    }
  }
  return {
    page_count: pages.length,
    pages,
    graph: {
      nodes: pages.map((page) => ({
        id: page.path,
        slug: page.slug,
        title: page.title,
        category: page.category,
        is_orphan: page.is_orphan,
        has_slug_collision: page.has_slug_collision
      })),
      edges
    },
    orphans: [],
    dangling_links: [],
    slug_collisions: {},
    health: { has_orphans: false, has_dangling_links: false, has_slug_collisions: false }
  };
}

function latestGraphProps() {
  return graphCanvasMock.props.at(-1) as Record<string, any>;
}

afterEach(() => {
  cleanup();
  [
    "--mimir-color-panel-background-muted",
    "--mimir-color-text",
    "--mimir-color-text-muted",
    "--mimir-color-panel-border",
    "--mimir-color-chrome-accent",
    "--mimir-color-status-success",
    "--mimir-color-status-warning",
    "--mimir-color-status-danger"
  ].forEach((token) => document.documentElement.style.removeProperty(token));
  graphCanvasMock.fitNodesInView.mockClear();
  graphCanvasMock.props = [];
  graphCanvasMock.zoomIn.mockClear();
  graphCanvasMock.zoomOut.mockClear();
});

describe("WikiGraphView", () => {
  it("passes wiki nodes, wikilink edges, and bundled rendering configuration to reagraph", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="concepts/alpha" />);

    expect(screen.getByLabelText("Wiki graph view")).toBeTruthy();
    expect(screen.getByTestId("reagraph-canvas").getAttribute("data-node-count")).toBe("4");
    expect(screen.getByTestId("reagraph-canvas").getAttribute("data-edge-count")).toBe("3");
    expect(screen.getByText("pages")).toBeTruthy();
    expect(screen.getByText("wikilinks")).toBeTruthy();
    expect(screen.getByText("communities")).toBeTruthy();

    const props = latestGraphProps();
    expect(props.layoutType).toBe("forceDirected2d");
    expect(props.clusterAttribute).toBe("community");
    expect(props.aggregateEdges).toBe(false);
    expect(props.edgeArrowPosition).toBe("none");
    expect(props.edgeInterpolation).toBe("curved");
    expect(props.labelType).toBe("nodes");
    expect(props.labelFontUrl).toContain("kenpixel.ttf");
    expect(props.labelFontUrl).not.toMatch(/^https?:\/\//);
    expect(props.nodes.map((node: { id: string }) => node.id)).toEqual([
      "concepts/alpha.md",
      "topics/beta.md",
      "topics/alpha.md",
      "dangling:ghost"
    ]);
    expect(props.edges.map((edge: { source: string; target: string }) => [edge.source, edge.target])).toEqual([
      ["concepts/alpha.md", "topics/beta.md"],
      ["topics/beta.md", "concepts/alpha.md"],
      ["concepts/alpha.md", "dangling:ghost"]
    ]);
    expect(props.nodes.every((node: { data?: { community?: string } }) => node.data?.community)).toBe(true);
  });

  it("derives reagraph and cluster colors from the active mimir CSS tokens", () => {
    document.documentElement.style.setProperty("--mimir-color-panel-background-muted", "#edf6ee");
    document.documentElement.style.setProperty("--mimir-color-text", "#123456");
    document.documentElement.style.setProperty("--mimir-color-text-muted", "#52665b");
    document.documentElement.style.setProperty("--mimir-color-panel-border", "#c8d8ce");
    document.documentElement.style.setProperty("--mimir-color-chrome-accent", "#4d7056");
    document.documentElement.style.setProperty("--mimir-color-status-success", "#2f6b44");
    document.documentElement.style.setProperty("--mimir-color-status-warning", "#80621f");
    document.documentElement.style.setProperty("--mimir-color-status-danger", "#8a3d35");

    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="concepts/alpha" />);

    const props = latestGraphProps();
    expect(props.theme.canvas.background).toBe("#edf6ee");
    expect(props.theme.node.fill).toBe("#4d7056");
    expect(props.theme.node.activeFill).toBe("#123456");
    expect(props.theme.node.label.backgroundColor).toBe("#edf6ee");
    expect(props.theme.edge.fill).toBe("rgba(18, 52, 86, 0.46)");
    expect(props.theme.cluster.fill).toBe("rgba(200, 216, 206, 0.22)");

    expect(props.nodes.find((node: { id: string }) => node.id === "dangling:ghost")?.fill).toBe("#80621f");
    expect(props.edges.find((edge: { dashed?: boolean }) => edge.dashed)?.fill).toBe("#80621f");
    expect(props.edges.find((edge: { dashed?: boolean }) => !edge.dashed)?.fill).toBe("rgba(18, 52, 86, 0.48)");
    expect(props.nodes.map((node: { fill: string }) => node.fill)).not.toContain("#4466a3");

    const legendSwatches = Array.from(document.querySelectorAll<HTMLElement>(".wiki-graph__legend i")).map((swatch) => swatch.style.background);
    expect(legendSwatches).toContain("rgb(128, 98, 31)");
    expect(legendSwatches).not.toContain("rgb(68, 102, 163)");
  });

  it("declutters labels to selected and hovered nodes instead of zoom-driven auto labels", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="alpha" />);

    let props = latestGraphProps();
    expect(props.labelType).toBe("nodes");
    expect(props.nodes.filter((node: { label?: string }) => node.label)).toEqual([
      expect.objectContaining({ id: "concepts/alpha.md", label: "Alpha" })
    ]);
    expect(screen.getAllByText("Alpha").length).toBeGreaterThan(0);

    fireEvent.pointerOver(screen.getByRole("button", { name: "Mock open Beta" }));

    props = latestGraphProps();
    expect(props.nodes.filter((node: { label?: string }) => node.label)).toEqual([
      expect.objectContaining({ id: "concepts/alpha.md", label: "Alpha" }),
      expect.objectContaining({ id: "topics/beta.md", label: "Beta" })
    ]);
    expect(screen.getAllByText("Beta").length).toBeGreaterThan(0);
  });

  it("exposes fit and zoom controls through the reagraph camera ref", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="" />);

    fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));

    expect(graphCanvasMock.fitNodesInView).toHaveBeenCalledTimes(1);
    expect(graphCanvasMock.zoomIn).toHaveBeenCalledTimes(1);
    expect(graphCanvasMock.zoomOut).toHaveBeenCalledTimes(1);
  });

  it("opens page nodes through the reader callback", () => {
    const onOpenPage = vi.fn();
    render(<WikiGraphView index={graphIndex()} onOpenPage={onOpenPage} selected="" />);

    fireEvent.click(screen.getByRole("button", { name: "Mock open Beta" }));

    expect(onOpenPage).toHaveBeenCalledWith("topics/beta");
  });

  it("distinguishes orphans, dangling targets, and slug collisions in the health list", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="" />);

    const healthList = screen.getByLabelText("Wiki graph node health");
    const alpha = within(healthList).getByRole("button", { name: /Alpha concepts\/alpha\.md orphan/ });
    const collision = within(healthList).getByRole("button", { name: /Alpha Topic topics\/alpha\.md orphan slug collision/ });
    const dangling = within(healthList).getByRole("button", { name: /ghost dangling target/ });

    expect(within(alpha).getByText("orphan")).toBeTruthy();
    expect(within(collision).getByText("collision")).toBeTruthy();
    expect(within(dangling).getAllByText("dangling").length).toBeGreaterThan(0);
    expect(dangling).toHaveProperty("disabled", true);
  });

  it("passes all live-scale resolved edges to the WebGL graph without aggregation", () => {
    render(<WikiGraphView index={denseGraphIndex()} onOpenPage={vi.fn()} selected="" />);

    const props = latestGraphProps();
    expect(props.nodes).toHaveLength(142);
    expect(props.edges).toHaveLength(924);
    expect(props.aggregateEdges).toBe(false);
    expect(props.edges.every((edge: { arrowPlacement?: string; interpolation?: string }) => (
      edge.arrowPlacement === "none" && edge.interpolation === "curved"
    ))).toBe(true);
  });

  it("uses link-derived communities instead of file categories for cluster layout", () => {
    render(<WikiGraphView index={denseGraphIndex()} onOpenPage={vi.fn()} selected="" />);

    const props = latestGraphProps();
    const categoryCount = new Set(props.nodes.map((node: { data: { category: string } }) => node.data.category)).size;
    const communityCount = new Set(props.nodes.map((node: { data: { community: string } }) => node.data.community)).size;

    expect(props.clusterAttribute).toBe("community");
    expect(categoryCount).toBe(3);
    expect(communityCount).toBeGreaterThan(1);
  });
});
