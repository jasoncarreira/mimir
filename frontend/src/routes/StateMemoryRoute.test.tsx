// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StateMemoryRoute } from "./StateMemoryRoute";
import type { DashboardSurface } from "../dashboardExtensions";

const surface: DashboardSurface = {
  id: "state-memory",
  label: "State & Memory",
  title: "State and memory dashboard",
  detail: "Browse files",
  icon: null,
  route_path: "/state-memory",
  nav_position: 50,
  enabled: true,
  trusted_first_party: true,
  bundle: null,
  css: [],
  api_namespace: null,
  path: "/state-memory",
  tabs: ["files"],
  filterLabel: "tier"
};

function envelope(data: unknown, meta?: unknown) {
  return { ok: true, version: "v1", data, meta };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body
  } as Response;
}

function renderRoute(initialEntry = "/state-memory") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<StateMemoryRoute surface={surface} />} path="/state-memory" />
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

describe("StateMemoryRoute", () => {
  it.each([[390, 844], [652, 800], [1280, 800]])(
    "preserves a large expanded tree and search origin through history at %ix%i",
    async (width, height) => {
      vi.stubGlobal("innerWidth", width);
      vi.stubGlobal("innerHeight", height);
      vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query === "(max-width: 720px)" && width <= 720 })));
      const scroll = vi.fn();
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
      const files = Array.from({ length: 144 }, (_, i) => ({
        name: `note-${i}.md`, type: "file", path: `memory/archive/nested/note-${i}.md`, size: 10, modified: null, desc: null
      }));
      const index = { ...files[0], name: "INDEX.md", path: "memory/INDEX.md" };
      vi.stubGlobal("fetch", vi.fn(async (url: string) => {
        const params = new URL(url, "http://localhost").searchParams;
        if (params.get("view") === "tree") return jsonResponse(envelope({
          name: "", type: "dir", path: "", children: [{
            name: "memory", type: "dir", path: "memory", children: [index, {
              name: "archive", type: "dir", path: "memory/archive", children: [{
                name: "nested", type: "dir", path: "memory/archive/nested", children: files
              }]
            }]
          }]
        }));
        if (params.get("view") === "search") return jsonResponse(envelope({
          hits: [7, 19].map((line_no) => ({ path: files[143].path, line_no, snippet: `needle at ${line_no}` }))
        }, { total: 2 }));
        return jsonResponse(envelope({ path: params.get("path"), content: "File content", size: 10, modified: null }));
      }));
      const router = createMemoryRouter([{ path: "/state-memory", element: <StateMemoryRoute surface={surface} /> }], {
        initialEntries: ["/state-memory"]
      });
      const initialKey = router.state.location.key;
      render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>);
      await screen.findByRole("heading", { name: "memory/INDEX.md" });
      expect(router.state.location.search).toBe("");
      expect(router.state.location.key).toBe(initialKey);
      expect(router.state.historyAction).toBe("POP");
      const heading = screen.getByRole("heading", { name: "Detail", level: 2 });
      const detailFocus = vi.spyOn(heading, "focus");
      const detail = heading.closest<HTMLElement>("[data-browser-detail]")!;
      expect(document.activeElement).not.toBe(heading);
      expect(document.activeElement).toBe(document.body);
      expect(scroll.mock.contexts).not.toContain(detail);
      const nav = screen.getByRole("navigation", { name: "State and memory file tree" });
      const sidebar = nav.closest<HTMLElement>("[data-browser-list]")!;
      const archive = within(nav).getByRole("button", { name: "archive" });
      fireEvent.click(archive);
      const nested = within(nav).getByRole("button", { name: "nested" });
      fireEvent.click(nested);
      expect(within(nav).getAllByRole("button", { name: /note-\d+\.md/ })).toHaveLength(144);

      async function journey(origin: HTMLElement, query: string) {
        sidebar.scrollTop = 3100;
        origin.focus();
        const focus = vi.spyOn(origin, "focus");
        scroll.mockClear();
        // Flush RouterProvider's asynchronous navigation before observing query content.
        await act(async () => { fireEvent.click(origin.querySelector("span")!); });
        await screen.findByRole("heading", { name: files[143].path });
        const detailSearch = router.state.location.search;
        expect(new URLSearchParams(detailSearch).get("path")).toBe(files[143].path);
        expect(new URLSearchParams(detailSearch).has("pane")).toBe(false);
        function assertPosition(inDetail: boolean) {
          expect(sidebar.isConnected).toBe(true);
          expect(origin.isConnected).toBe(true);
          expect(sidebar.scrollTop).toBe(3100);
          expect((screen.getByLabelText("Search state and memory files") as HTMLInputElement).value).toBe(query);
          expect(new URLSearchParams(router.state.location.search).get("q") ?? "").toBe(query);
          expect(new URLSearchParams(router.state.location.search).get("path")).toBe(files[143].path);
          expect(document.activeElement).toBe(width <= 720 && inDetail ? heading : origin);
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
          if (!query) {
            expect(screen.getByRole("navigation", { name: "State and memory file tree" })).toBe(nav);
            expect(archive.getAttribute("aria-expanded")).toBe("true");
            expect(nested.getAttribute("aria-expanded")).toBe("true");
            expect(origin.getAttribute("aria-current")).toBe("true");
          }
        }
        assertPosition(true);
        if (width <= 720) {
          expect(heading.tabIndex).toBe(-1);
          expect(fireEvent.keyDown(heading, { key: "Tab" })).toBe(true);
          const returnButton = screen.getByRole("button", { name: "Back to files" });
          returnButton.focus();
          expect(document.activeElement).toBe(returnButton);
        }
        await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Back to files" })); });
        expect(new URLSearchParams(router.state.location.search).get("pane")).toBe("list");
        assertPosition(false);
        await act(async () => { await router.navigate(-1); });
        expect(router.state.location.search).toBe(detailSearch);
        assertPosition(true);
        await act(async () => { await router.navigate(1); });
        assertPosition(false);
      }
      await journey(within(nav).getByRole("button", { name: "note-143.md" }), "");
      fireEvent.change(screen.getByLabelText("Search state and memory files"), { target: { value: "needle" } });
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Search" })); });
      // Two hits for the same file ensure return remembers the clicked hit, not just its path.
      const hit = await screen.findByRole("button", { name: /:19\s*needle at 19/ });
      await journey(hit, "needle");
    }
  );

  it("renders file list counts and auto-selects INDEX.md with parsed desc", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("view=tree")) {
        return jsonResponse(envelope({
          name: "",
          type: "dir",
          path: "",
          desc: null,
          children: [
            {
              name: "memory",
              type: "dir",
              path: "memory",
              desc: null,
              children: [
                { name: "INDEX.md", type: "file", path: "memory/INDEX.md", size: 512, modified: "2026-06-18T14:00:00Z", desc: "Index" }
              ]
            },
            {
              name: "state",
              type: "dir",
              path: "state",
              desc: null,
              children: [
                { name: "notes.md", type: "file", path: "state/notes.md", size: 1024, modified: "2026-06-18T14:01:00Z", desc: null }
              ]
            }
          ]
        }));
      }
      if (url.includes("view=file") && url.includes("memory%2FINDEX.md")) {
        return jsonResponse(envelope({
          path: "memory/INDEX.md",
          content: "<!-- desc: Memory index -->\n# Memory Index",
          size: 512,
          modified: "2026-06-18T14:00:00Z"
        }));
      }
      return jsonResponse(envelope({ hits: [] }, { cursor: null, limit: null, total: 0, truncated: false }));
    }));

    renderRoute();

    expect(await screen.findByRole("heading", { name: "memory/INDEX.md" })).toBeTruthy();
    expect(screen.getByText("Memory index")).toBeTruthy();
    expect(screen.getByText("512 B")).toBeTruthy();
    expect(screen.getByText("2026-06-18 14:00:00Z")).toBeTruthy();
    const counts = screen.getByText("State").closest("dl") as HTMLElement;
    expect(within(counts).getAllByText("1")).toHaveLength(2);
  });

  it("renders search hits and endpoint errors without blanking the detail pane", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("view=tree")) {
        return jsonResponse(envelope({
          name: "",
          type: "dir",
          path: "",
          desc: null,
          children: [
            { name: "memory", type: "dir", path: "memory", desc: null, children: [
              { name: "INDEX.md", type: "file", path: "memory/INDEX.md", size: 10, modified: "2026-06-18T14:00:00Z", desc: null }
            ] }
          ]
        }));
      }
      if (url.includes("view=search")) {
        return jsonResponse(envelope({
          query: "needle",
          hits: [{ path: "state/wiki/topics/demo.md", line_no: 7, snippet: "needle in haystack" }]
        }, { cursor: null, limit: null, total: 1, truncated: false }));
      }
      if (url.includes("view=file")) {
        return jsonResponse(envelope({
          path: "memory/INDEX.md",
          content: "# Memory Index",
          size: 10,
          modified: "bad-date"
        }));
      }
      return jsonResponse(envelope({}));
    }));

    renderRoute("/state-memory?q=needle&path=memory/INDEX.md");

    expect(await screen.findByText("1 result(s)")).toBeTruthy();
    expect(screen.getByText("state/wiki/topics/demo.md:7")).toBeTruthy();
    expect(screen.getByText("needle in haystack")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "memory/INDEX.md" })).toBeTruthy();
    fireEvent.click(screen.getByText("Clear"));
    expect(screen.queryByText("needle in haystack")).toBeNull();
  });
});
