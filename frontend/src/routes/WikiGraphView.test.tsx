// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WikiIndexData } from "../api";
import WikiGraphView from "./WikiGraphView";

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

function largeGraphIndex(): WikiIndexData {
  const pages = Array.from({ length: 42 }, (_, index) => {
    const category = index < 21 ? "concepts" : "topics";
    const slug = `page-${index}`;
    return {
      slug,
      title: index === 0 ? "Hub Page With A Very Long Title That Needs Truncation" : `Node ${index}`,
      category,
      path: `${category}/${slug}.md`,
      mtime: null,
      outbound: [],
      inbound: [],
      is_orphan: index % 13 === 0,
      has_slug_collision: false
    };
  });
  const edges = pages.slice(1).map((page, index) => ({
    source: pages[0].path,
    target: page.path,
    target_slug: page.slug
  }));
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
    health: { has_orphans: true, has_dangling_links: false, has_slug_collisions: false }
  };
}

afterEach(() => {
  cleanup();
});

describe("WikiGraphView", () => {
  it("renders page nodes, wikilink edges, and category legend", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="concepts/alpha" />);

    expect(screen.getByLabelText("Wiki graph view")).toBeTruthy();
    expect(screen.getByLabelText("Wiki pages and wikilinks graph")).toBeTruthy();
    expect(screen.getByText("pages")).toBeTruthy();
    expect(screen.getByText("wikilinks")).toBeTruthy();
    expect(screen.getByText("concepts")).toBeTruthy();
    expect(screen.getByText("topics")).toBeTruthy();
    expect(screen.getAllByText("dangling").length).toBeGreaterThan(0);
  });

  it("opens page nodes through the reader callback", () => {
    const onOpenPage = vi.fn();
    render(<WikiGraphView index={graphIndex()} onOpenPage={onOpenPage} selected="" />);

    fireEvent.click(screen.getByRole("button", { name: "Open Beta" }));

    expect(onOpenPage).toHaveBeenCalledWith("topics/beta");
  });

  it("distinguishes orphans, dangling targets, and slug collisions", () => {
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

  it("declutters labels and truncates long default labels", () => {
    render(<WikiGraphView index={largeGraphIndex()} onOpenPage={vi.fn()} selected="" />);

    const graph = screen.getByLabelText("Wiki pages and wikilinks graph");
    const labels = graph.querySelectorAll(".wiki-graph__label");

    expect(labels.length).toBeGreaterThan(0);
    expect(labels.length).toBeLessThan(largeGraphIndex().pages.length);
    expect([...labels].some((label) => label.textContent?.endsWith("…"))).toBe(true);
    labels.forEach((label) => {
      expect(Number(label.getAttribute("x"))).toBeGreaterThanOrEqual(2);
      expect(Number(label.getAttribute("x"))).toBeLessThanOrEqual(98);
      expect(Number(label.getAttribute("y"))).toBeGreaterThanOrEqual(4);
      expect(Number(label.getAttribute("y"))).toBeLessThanOrEqual(97);
    });
  });

  it("zooms with controls and resets the graph transform", () => {
    render(<WikiGraphView index={graphIndex()} onOpenPage={vi.fn()} selected="" />);

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));

    expect(screen.getByLabelText("Graph zoom level").textContent).toBe("125%");

    fireEvent.click(screen.getByRole("button", { name: "Reset graph view" }));

    expect(screen.getByLabelText("Graph zoom level").textContent).toBe("100%");
  });

  it("isolates hovered node links and shows the full focused label", () => {
    render(<WikiGraphView index={largeGraphIndex()} onOpenPage={vi.fn()} selected="" />);

    fireEvent.mouseEnter(screen.getByRole("button", { name: /Open Hub Page With A Very Long Title/ }));

    expect(document.querySelector(".wiki-graph__focus-label")?.textContent).toContain("Hub Page With A Very Long Title That Needs Truncation");
    expect(document.querySelectorAll(".wiki-graph__edge--dimmed").length).toBe(0);

    fireEvent.mouseEnter(screen.getByRole("button", { name: "Open Node 10" }));

    expect(document.querySelector(".wiki-graph__focus-label")?.textContent).toContain("Node 10");
    expect(document.querySelectorAll(".wiki-graph__edge--dimmed").length).toBeGreaterThan(0);
    expect(document.querySelectorAll(".wiki-graph__node--dimmed").length).toBeGreaterThan(0);
  });
});
