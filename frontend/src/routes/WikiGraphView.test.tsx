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
});
