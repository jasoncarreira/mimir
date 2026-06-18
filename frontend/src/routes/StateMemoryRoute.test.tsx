// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
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

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("StateMemoryRoute", () => {
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
