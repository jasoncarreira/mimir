// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import React from "react";
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from "react-router-dom";
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
        .map((node: { id: string; data?: { title?: string } }) => ReactModule.createElement(
          "button",
          {
            key: node.id,
            onClick: () => props.onNodeClick({ id: node.id, data: {} }),
            type: "button"
          },
          `Open ${node.data?.title?.split(" - ")[0] ?? node.id}`
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

const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
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
  it("bounds both narrow sidebars at the same breakpoint used for focus navigation", () => {
    // jsdom has no layout: this checks the CSS contract, not rendered dimensions.
    const css = readFileSync("frontend/src/styles.css", "utf8");
    expect(css).toMatch(/@media \(max-width: 720px\)\s*\{[^@]*?\.memory-browser__sidebar,\s*\.wiki-browser__sidebar\s*\{[^}]*max-height: 65vh;\s*max-height: 65dvh;\s*overflow: auto;/);
  });

  it.each([[390, 844], [652, 800], [1280, 800]])(
    "preserves a 144-page sidebar through selection, return and history at %ix%i",
    async (width, height) => {
      vi.stubGlobal("innerWidth", width);
      vi.stubGlobal("innerHeight", height);
      const matchMedia = vi.fn((query: string) => ({ matches: query === "(max-width: 720px)" && width <= 720 }));
      vi.stubGlobal("matchMedia", matchMedia);
      const scroll = vi.fn();
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
      const pages = Array.from({ length: 144 }, (_, i) => ({
        ...indexPayload().pages[0],
        slug: `page-${String(i).padStart(3, "0")}`,
        title: `Page ${String(i).padStart(3, "0")}`,
        path: `concepts/page-${String(i).padStart(3, "0")}.md`
      }));
      vi.stubGlobal("fetch", vi.fn(async (url: string) => {
        if (url.endsWith("/api/v1/wiki")) return jsonResponse(envelope({ ...indexPayload(), page_count: pages.length, pages }));
        const page = pages.find((item) => decodeURIComponent(url).endsWith(item.path.slice(0, -3))) ?? pages[0];
        return jsonResponse(envelope({ ...page, markdown: `Content for ${page.title}` }));
      }));
      const router = createMemoryRouter([{ path: "/wiki", element: <WikiRoute surface={surface} /> }], {
        initialEntries: ["/wiki?q=page&category=concepts&pane=list"]
      });
      render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>);
      const nav = await screen.findByRole("navigation", { name: "Wiki pages" });
      expect(document.activeElement).toBe(document.body);
      expect(within(nav).getAllByRole("button")).toHaveLength(144);
      const sidebar = nav.closest<HTMLElement>("[data-browser-list]")!;
      const heading = screen.getByRole("heading", { name: "Reader", level: 2 });
      const detailFocus = vi.spyOn(heading, "focus");
      const detail = heading.closest<HTMLElement>("[data-browser-detail]")!;
      const selected = within(nav).getByRole("button", { name: /^Page 143/ });
      sidebar.scrollTop = 4200;
      selected.focus();
      const focus = vi.spyOn(selected, "focus");
      scroll.mockClear();
      fireEvent.click(selected.querySelector("span")!);
      await screen.findByText("Content for Page 143");
      const detailSearch = router.state.location.search;
      expect(new URLSearchParams(detailSearch).get("slug")).toBe("concepts/page-143");
      expect(new URLSearchParams(detailSearch).has("pane")).toBe(false);

      function assertPosition(inDetail: boolean) {
        expect(screen.getByRole("navigation", { name: "Wiki pages" })).toBe(nav);
        expect(sidebar.scrollTop).toBe(4200);
        expect(selected.getAttribute("aria-current")).toBe("true");
        expect((screen.getByLabelText("Search wiki pages") as HTMLInputElement).value).toBe("page");
        expect((screen.getByLabelText("Filter wiki category") as HTMLSelectElement).value).toBe("concepts");
        expect(new URLSearchParams(router.state.location.search).get("q")).toBe("page");
        expect(new URLSearchParams(router.state.location.search).get("category")).toBe("concepts");
        expect(new URLSearchParams(router.state.location.search).get("slug")).toBe("concepts/page-143");
        expect(document.activeElement).toBe(width <= 720 && inDetail ? heading : selected);
        if (width <= 720) {
          expect(scroll.mock.contexts.at(-1)).toBe(inDetail ? detail : sidebar);
          expect(scroll).toHaveBeenLastCalledWith({ block: "start" });
          if (inDetail) expect(detailFocus).toHaveBeenLastCalledWith({ preventScroll: true });
          if (!inDetail) expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
        } else {
          expect(scroll).not.toHaveBeenCalled();
          expect(focus).not.toHaveBeenCalled();
          expect(detailFocus).not.toHaveBeenCalled();
        }
      }
      assertPosition(true);
      if (width <= 720) {
        expect(heading.tabIndex).toBe(-1);
        expect(fireEvent.keyDown(heading, { key: "Tab" })).toBe(true);
        const returnButton = screen.getByRole("button", { name: "Back to pages" });
        returnButton.focus();
        expect(document.activeElement).toBe(returnButton);
      }
      fireEvent.click(screen.getByRole("button", { name: "Back to pages" }));
      expect(new URLSearchParams(router.state.location.search).get("pane")).toBe("list");
      assertPosition(false);
      await act(async () => { await router.navigate(-1); });
      expect(router.state.location.search).toBe(detailSearch);
      assertPosition(true);
      await act(async () => { await router.navigate(1); });
      assertPosition(false);
      expect(matchMedia).toHaveBeenCalledWith("(max-width: 720px)");
    }
  );

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

  it("renders a bare fence as code without interpreting inline markdown", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/api/v1/wiki")) return jsonResponse(envelope(indexPayload()));
      if (url.includes("/api/v1/wiki/")) {
        return jsonResponse(envelope({
          ...pagePayload(url),
          markdown: "# Alpha\n```\n**bold** [[beta]]\n```"
        }));
      }
      return jsonResponse(envelope({}));
    }));

    renderRoute();

    await waitFor(() => expect(screen.getAllByRole("heading", { name: "Alpha" }).length).toBeGreaterThan(0));
    const reader = screen.getByText("concepts/alpha.md").closest("article") as HTMLElement;
    const code = reader.querySelector("pre > code");
    expect(code?.textContent).toBe("**bold** [[beta]]");
    expect(code?.querySelector("strong")).toBeNull();
    expect(code?.querySelector("a")).toBeNull();
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
