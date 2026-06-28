// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeWikiIndexPayload } from "../api/wiki";
import type { DashboardSurface } from "../dashboardExtensions";
import { WikiRoute } from "./WikiRoute";

vi.mock("reagraph", async () => {
  const ReactModule = await import("react");
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
    GraphCanvas: (props: Record<string, any>) => ReactModule.createElement(
      "div",
      { "aria-label": "Mock reagraph canvas" },
      props.nodes
        .filter((node: { data?: { kind?: string } }) => node.data?.kind === "page")
        .map((node: { id: string; label: string }) => ReactModule.createElement(
          "button",
          {
            key: node.id,
            onClick: () => props.onNodeClick({ id: node.id, data: {} }),
            type: "button"
          },
          `Open ${node.label}`
        ))
    )
  };
});

const surface: DashboardSurface = {
  id: "wiki",
  label: "Wiki",
  title: "Wiki",
  detail: "Browse read-only wiki pages",
  icon: null,
  route_path: "/wiki",
  nav_position: 55,
  enabled: true,
  trusted_first_party: true,
  bundle: null,
  css: [],
  api_namespace: "wiki",
  path: "/wiki",
  tabs: ["pages"],
  filterLabel: "category"
};

function envelope(data: unknown) {
  return { ok: true, version: "v1", data };
}

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body)
  } as Response;
}

function indexPayload() {
  return {
    page_count: 3,
    pages: [
      {
        slug: "alpha",
        title: "Alpha",
        category: "concepts",
        path: "concepts/alpha.md",
        mtime: "2026-06-18T14:00:00Z",
        outbound: ["beta", "missing-page"],
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
        title: "Alpha Collision",
        category: "topics",
        path: "topics/alpha.md",
        mtime: null,
        outbound: [],
        inbound: [],
        is_orphan: true,
        has_slug_collision: true
      }
    ],
    graph: { nodes: [], edges: [] },
    orphans: ["concepts/alpha.md", "topics/alpha.md"],
    dangling_links: [{ target: "missing-page", source: "concepts/alpha.md", line: 3 }],
    slug_collisions: { alpha: ["concepts/alpha.md", "topics/alpha.md"] },
    health: { has_orphans: true, has_dangling_links: true, has_slug_collisions: true }
  };
}

function graphPayload() {
  const payload = indexPayload();
  return {
    ...payload,
    graph: {
      nodes: payload.pages.map((page) => ({
        id: page.path,
        slug: page.slug,
        title: page.title,
        category: page.category,
        is_orphan: page.is_orphan,
        has_slug_collision: page.has_slug_collision
      })),
      edges: [
        { source: "concepts/alpha.md", target: "topics/beta.md", target_slug: "beta" },
        { source: "topics/beta.md", target: "concepts/alpha.md", target_slug: "alpha" }
      ]
    }
  };
}

function pagePayload(slugOrPath: string) {
  if (slugOrPath.includes("topics%2Fbeta") || slugOrPath.includes("topics/beta") || slugOrPath.endsWith("/beta")) {
    return {
      slug: "beta",
      title: "Beta",
      category: "topics",
      path: "topics/beta.md",
      mtime: null,
      outbound: ["alpha"],
      inbound: ["concepts/alpha.md"],
      is_orphan: false,
      has_slug_collision: false,
      markdown: "# Beta\nBack to [[alpha]]."
    };
  }
  return {
    slug: "alpha",
    title: "Alpha",
    category: "concepts",
    path: "concepts/alpha.md",
    mtime: "2026-06-18T14:00:00Z",
    outbound: ["beta", "missing-page"],
    inbound: [],
    is_orphan: true,
    has_slug_collision: false,
    markdown: "# Alpha\nSee [[beta]] and [[missing-page]].\n\n<script>alert('xss')</script>"
  };
}

function renderRoute(initialEntry = "/wiki?slug=concepts/alpha") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<WikiRoute surface={surface} />} path="/wiki" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("wiki API normalization", () => {
  it("normalizes partial wiki index payloads", () => {
    const normalized = normalizeWikiIndexPayload({
      pages: [{ slug: "demo", title: 42, outbound: ["ok", 3], is_orphan: true }],
      dangling_links: [{ target: "ghost", source: "demo.md", line: "bad" }],
      slug_collisions: { demo: ["a.md", 1] }
    });

    expect(normalized.page_count).toBe(1);
    expect(normalized.pages[0]).toMatchObject({
      slug: "demo",
      title: "demo",
      category: "_root",
      path: "demo.md",
      outbound: ["ok"],
      is_orphan: true
    });
    expect(normalized.dangling_links[0]).toEqual({ target: "ghost", source: "demo.md", line: 0 });
    expect(normalized.slug_collisions).toEqual({ demo: ["a.md"] });
  });
});

describe("WikiRoute", () => {
  it("renders read-only markdown, escapes raw HTML, and navigates wikilinks in-app", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/v1/wiki")) return jsonResponse(envelope(indexPayload()));
      if (url.includes("/api/v1/wiki/")) return jsonResponse(envelope(pagePayload(url)));
      return jsonResponse(envelope({}));
    }));

    renderRoute();

    await waitFor(() => expect(screen.getAllByRole("heading", { name: "Alpha" }).length).toBeGreaterThan(0));
    const reader = screen.getByText("concepts/alpha.md").closest("article") as HTMLElement;
    expect(within(reader).getByRole("link", { name: "beta" }).getAttribute("href")).toBe("/wiki?slug=topics%2Fbeta");
    expect(within(reader).getAllByText("missing-page")[0].className).toContain("wiki-wikilink--dangling");
    expect(reader.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>alert('xss')</script>")).toBeTruthy();
    expect(screen.getByText("Backlinks")).toBeTruthy();
    expect(screen.getByText("Outlinks")).toBeTruthy();
    expect(screen.getByText("dangling links")).toBeTruthy();

    fireEvent.click(within(reader).getByRole("link", { name: "beta" }));
    await waitFor(() => expect(screen.getAllByRole("heading", { name: "Beta" }).length).toBeGreaterThan(0));
  });

  it("browses by title, slug, and category filters", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/v1/wiki")) return jsonResponse(envelope(indexPayload()));
      if (url.includes("/api/v1/wiki/")) return jsonResponse(envelope(pagePayload(url)));
      return jsonResponse(envelope({}));
    }));

    renderRoute("/wiki");

    await waitFor(() => expect(screen.getAllByRole("button", { name: /Alpha/ }).length).toBeGreaterThan(0));
    fireEvent.change(screen.getByLabelText("Search wiki pages"), { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByRole("button", { name: /Beta/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Alpha Collision/ })).toBeNull();

    fireEvent.change(screen.getByLabelText("Filter wiki category"), { target: { value: "topics" } });
    fireEvent.change(screen.getByLabelText("Search wiki pages"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByRole("button", { name: /Beta/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Alpha Collision/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Alpha alpha/ })).toBeNull();
  });

  it("lazy-loads graph mode and opens clicked nodes in the reader", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/v1/wiki")) return jsonResponse(envelope(graphPayload()));
      if (url.includes("/api/v1/wiki/")) return jsonResponse(envelope(pagePayload(url)));
      return jsonResponse(envelope({}));
    }));

    renderRoute("/wiki?view=graph&slug=concepts/alpha");

    fireEvent.click(await screen.findByRole("button", { name: "Graph" }));
    expect(await screen.findByLabelText("Wiki graph view")).toBeTruthy();
    expect(screen.getByLabelText("Wiki graph legend")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Open Beta" }));
    await waitFor(() => expect(screen.getAllByRole("heading", { name: "Beta" }).length).toBeGreaterThan(0));
    expect(screen.getByText("topics/beta.md")).toBeTruthy();
  });

  it("renders empty and error states", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/v1/wiki")) {
        return jsonResponse(envelope({
          page_count: 0,
          pages: [],
          graph: { nodes: [], edges: [] },
          orphans: [],
          dangling_links: [],
          slug_collisions: {}
        }));
      }
      return jsonResponse(envelope({}));
    }));

    const first = renderRoute("/wiki");
    expect(await screen.findByText("No matching pages")).toBeTruthy();
    expect(screen.getByText("The wiki API returned an empty page list.")).toBeTruthy();
    first.unmount();

    vi.restoreAllMocks();
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse({ ok: false, version: "v1", error: { code: "wiki_not_found", message: "wiki directory not found" } }, false, 404)
    ));
    renderRoute("/wiki");
    await waitFor(() => expect(screen.getByText("Wiki index failed")).toBeTruthy());
    expect(screen.getByText("wiki_not_found: wiki directory not found")).toBeTruthy();
  });
});
